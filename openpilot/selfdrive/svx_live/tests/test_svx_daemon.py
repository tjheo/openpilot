"""LiveDaemon lifecycle: default OFF, toggle OFF teardown, selection/fail-closed, invalid source, cadence, signals."""
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from openpilot.selfdrive.svx_live import svx_lived as D
from openpilot.selfdrive.svx_live import wire as W
from openpilot.selfdrive.svx_live.netpolicy import AddressPolicy
from openpilot.selfdrive.svx_live.profile import HYUNDAI_FLAG_CANFD, CarSelection
from openpilot.selfdrive.svx_live.rpm_state import INVALID
from openpilot.selfdrive.svx_live.server import LiveServer
from openpilot.selfdrive.svx_live.tests.helpers import Client

SELTOS = CarSelection("hyundai", "KIA_SELTOS", 0)
HERE = os.path.dirname(os.path.abspath(__file__))


class Sel:
  def __init__(self, sel):
    self.sel = sel

  def read(self):
    return self.sel


class Src:
  def __init__(self):
    self.next = None
    self.closed = False

  def poll(self):
    f, self.next = self.next, None
    return f

  def close(self):
    self.closed = True


class Rig:
  def __init__(self, sel=SELTOS, enabled=True):
    self.enabled = enabled
    self.sel = Sel(sel)
    self.srcs = []
    self.server = LiveServer(0, AddressPolicy(allow_loopback=True), host="127.0.0.1")
    self.d = D.LiveDaemon(self.server, lambda: self.enabled, self.sel, self._factory, logger=lambda m: None)

  def _factory(self, prof):
    assert prof.address == 0x316 and prof.fallback_address == 0x366 and prof.allowed_buses == frozenset({0})
    s = Src()
    self.srcs.append(s)
    return s

  def steps(self, n=1):
    for _ in range(n):
      if not self.d.step():
        return False
    return True

  def connect(self):
    self.steps(1)
    c = Client(self.server.bound_port)
    c.send(W.hello(42))
    frames, _ = c.recv_frames(lambda: self.steps(1), until=lambda fs: len(fs) >= 2)
    assert frames[0].type == W.T_WELCOME
    return c, frames[0].fields[0], frames[1:]


@pytest.fixture
def rig():
  rigs = []

  def make(**kw):
    r = Rig(**kw)
    rigs.append(r)
    return r
  yield make
  for r in rigs:
    r.d.close()


def last_rpm(frames):
  rpms = [f for f in frames if f.type == W.T_RPM]
  return rpms[-1].fields if rpms else None


def test_off_never_listens_or_subscribes(rig):
  r = rig(enabled=False)
  assert r.d.step() is False
  assert not r.server.listening and r.srcs == [] and r.server.sel is None


def test_unsupported_car_fails_closed_without_can_subscription(rig):
  for sel, status in ((CarSelection("hyundai", "HYUNDAI_SONATA", 0), W.STATUS_UNSUPPORTED_CAR),
                      (CarSelection("hyundai", "KIA_SELTOS", HYUNDAI_FLAG_CANFD), W.STATUS_UNSUPPORTED_CANFD),
                      (None, W.STATUS_WAIT_CAR)):
    r = rig(sel=sel)
    c, sid, frames = r.connect()
    frames, _ = c.recv_frames(lambda r=r: r.steps(1), until=lambda fs: len(fs) >= 2)
    f = last_rpm(frames)
    assert f[5] == status and not f[6] & W.FLAG_RPM_VALID and r.srcs == []
    c.close()


def test_supported_car_streams_real_rpm_and_invalid_source_silences(rig):
  r = rig()
  c, sid, frames = r.connect()
  assert len(r.srcs) == 1
  src = r.srcs[0]
  src.next = (D.clock_boottime_ns(), bytes([0, 0, 0x00, 0x32, 0, 0, 0, 0]))
  frames, _ = c.recv_frames(lambda: r.steps(1), until=lambda fs: any(f.type == W.T_RPM and f.fields[5] == W.STATUS_OK for f in fs))
  ok = [f for f in frames if f.type == W.T_RPM and f.fields[5] == W.STATUS_OK][-1].fields
  assert ok[3] == 0x3200 and ok[6] == W.FLAG_RPM_VALID | W.FLAG_FRAME_SEEN
  src.next = (INVALID, "invalid_event")
  frames, _ = c.recv_frames(lambda: r.steps(1), until=lambda fs: any(f.type == W.T_RPM for f in fs))
  assert last_rpm(frames)[5] == W.STATUS_INVALID and not last_rpm(frames)[6] & W.FLAG_RPM_VALID
  src.next = lambda: None
  src.poll = lambda: (_ for _ in ()).throw(RuntimeError("source broke"))  # an exception is invalid evidence too
  frames, _ = c.recv_frames(lambda: r.steps(1), until=lambda fs: any(f.type == W.T_RPM for f in fs))
  assert last_rpm(frames)[5] == W.STATUS_INVALID
  c.close()


def test_missing_frames_go_stale_then_no_valid(rig):
  r = rig()
  c, sid, _ = r.connect()
  r.srcs[0].next = (D.clock_boottime_ns(), bytes([0, 0, 0x00, 0x32, 0, 0, 0, 0]))
  frames, _ = c.recv_frames(lambda: r.steps(1), until=lambda fs: any(f.type == W.T_RPM and f.fields[5] == W.STATUS_STALE for f in fs),
                            timeout=1.5)
  assert any(f.type == W.T_RPM and f.fields[5] == W.STATUS_STALE and f.fields[6] == W.FLAG_FRAME_SEEN for f in frames)
  c.close()


def test_selection_loss_ends_sessions_and_drops_data(rig):
  r = rig()
  c, sid, _ = r.connect()
  r.srcs[0].next = (D.clock_boottime_ns(), bytes([0, 0, 0x00, 0x32, 0, 0, 0, 0]))
  r.steps(2)
  assert r.d.state.frame_counter == 1
  r.sel.sel = None
  r.d._next_sel = 0.0
  frames, eof = c.recv_frames(lambda: r.steps(1), until=lambda fs: any(f.type == W.T_BYE for f in fs))
  assert any(f.type == W.T_BYE and f.fields[:2] == (sid, W.BYE_CAR_CHANGED) for f in frames)
  assert r.srcs[0].closed and r.d.src is None and r.d.state.frame_counter == 0 and r.d.state.last_sample is None
  c.close()


def test_toggle_off_closes_sessions_promptly(rig):
  r = rig()
  c, sid, _ = r.connect()
  r.enabled = False
  t0 = time.monotonic()
  frames, eof = c.recv_frames(lambda: r.d.step() or r.d.close(), until=lambda fs: False, timeout=2.0)
  assert eof and any(f.type == W.T_BYE and f.fields[:2] == (sid, W.BYE_STOPPING) for f in frames)
  assert time.monotonic() - t0 < D.ENABLED_RECHECK_S + 0.3
  assert not r.server.listening and r.srcs[0].closed
  c.close()


def test_cadence_holds_under_flood(rig):
  r = rig()
  c, sid, _ = r.connect()
  stop = threading.Event()

  def flood():
    while not stop.is_set():
      try:
        c.sock.send(b"\x00")  # never a complete header: always-readable socket until the buffer cap trips
      except OSError:
        return
      time.sleep(0.0005)
  t = threading.Thread(target=flood, daemon=True)
  t.start()
  t0, cpu0 = time.monotonic(), time.process_time()
  r.steps(25)
  wall, cpu = time.monotonic() - t0, time.process_time() - cpu0
  stop.set()
  t.join(1)
  assert wall >= 25 * D.TICK_S * 0.95, wall  # 50 Hz wall-clock cadence enforced, not select()-driven
  assert cpu < wall  # sleeps, does not spin
  c.close()


def test_invalid_port_idles_without_socket():
  slept = []
  d = D.LiveDaemon(None, lambda: True, Sel(SELTOS), lambda p: pytest.fail("no source without a listener"), logger=lambda m: None,
                   sleep=slept.append)
  assert d.step() and slept[0] == D.IDLE_S and d.src is None
  d.close()


def test_real_child_process_stream_off_teardown_and_sigint(tmp_path):
  toggle, portfile = tmp_path / "toggle", tmp_path / "port"
  toggle.write_text("1")
  env = {"PATH": os.environ.get("PATH", "")}
  p = subprocess.Popen([sys.executable, "-I", os.path.join(HERE, "daemon_harness.py"), str(toggle), str(portfile)],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
  try:
    for _ in range(200):
      if portfile.exists():
        break
      time.sleep(0.02)
    port = int(portfile.read_text())
    c = Client(port)
    c.send(W.hello(7))
    frames, _ = c.recv_frames(lambda: time.sleep(0.005), until=lambda fs: any(f.type == W.T_RPM and f.fields[5] == W.STATUS_OK for f in fs))
    sid = frames[0].fields[0]
    assert any(f.type == W.T_RPM and f.fields[3] == 0x3200 and f.fields[5] == W.STATUS_OK for f in frames)
    # toggle OFF -> BYE STOPPING and process exit, without any signal
    toggle.write_text("0")
    t0 = time.monotonic()
    frames, eof = c.recv_frames(lambda: time.sleep(0.005), until=lambda fs: False, timeout=3.0)
    assert eof and any(f.type == W.T_BYE and f.fields[:2] == (sid, W.BYE_STOPPING) for f in frames)
    assert p.wait(timeout=3) == 0 and time.monotonic() - t0 < 1.5
    c.close()
    out = p.stdout.read()
    assert "source closed" in out and "disabled" in out
  finally:
    if p.poll() is None:
      p.kill()
      p.wait()
    p.stdout.close()

  # manager-style stop: SIGINT while a session is open
  toggle.write_text("1")
  portfile.unlink()
  p = subprocess.Popen([sys.executable, "-I", os.path.join(HERE, "daemon_harness.py"), str(toggle), str(portfile)],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
  try:
    for _ in range(200):
      if portfile.exists():
        break
      time.sleep(0.02)
    c = Client(int(portfile.read_text()))
    c.send(W.hello(8))
    frames, _ = c.recv_frames(lambda: time.sleep(0.005), until=lambda fs: len(fs) >= 2)
    sid = frames[0].fields[0]
    t0 = time.monotonic()
    p.send_signal(signal.SIGINT)
    frames, eof = c.recv_frames(lambda: time.sleep(0.005), until=lambda fs: False, timeout=3.0)
    assert eof and any(f.type == W.T_BYE and f.fields[:2] == (sid, W.BYE_STOPPING) for f in frames)
    assert p.wait(timeout=3) == 0 and time.monotonic() - t0 < 1.0
    c.close()
  finally:
    if p.poll() is None:
      p.kill()
      p.wait()
    p.stdout.close()


def test_unexpected_tick_error_is_contained(rig):
  r = rig()
  logs = []
  r.d._logger = logs.append
  r.d._work = lambda now, deadline: (_ for _ in ()).throw(RuntimeError("boom"))
  t0 = time.monotonic()
  assert r.steps(5)  # keeps running instead of crashing (the manager would restart it every second)
  assert r.d.errors == 5 and len([m for m in logs if "tick failed" in m]) == 1
  assert time.monotonic() - t0 >= 5 * D.TICK_S * 0.95


def test_ems366_fallback_frame_streams_through_the_daemon(rig):
  from openpilot.selfdrive.svx_live.rpm_state import Frame
  r = rig()
  c, sid, frames = r.connect()
  r.srcs[0].next = Frame(D.clock_boottime_ns(), bytes([0x10, 0xF0, 0x0A, 0x10, 0, 0, 0]), 0x366)  # 7 bytes, N raw 2800
  frames, _ = c.recv_frames(lambda: r.steps(1), until=lambda fs: any(f.type == W.T_RPM and f.fields[5] == W.STATUS_OK for f in fs))
  ok = [f for f in frames if f.type == W.T_RPM and f.fields[5] == W.STATUS_OK][-1].fields
  assert ok[3] == 2800 and ok[6] == W.FLAG_RPM_VALID | W.FLAG_FRAME_SEEN and r.d.state.last_sample.rpm == 700.0
  c.close()

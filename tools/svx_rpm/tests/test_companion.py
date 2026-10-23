import errno
import os
import signal
import socket
import subprocess
import sys
import threading
import time

from svxrpm import protocol as P
from svxrpm.cereal_source import KEEP_PER_TICK, MAX_DRAIN_PER_TICK, drain_bounded
from svxrpm.companion import EXIT_UNSUPPORTED, Companion
from svxrpm.config import Config
from svxrpm.core import NS_PER_MS
from svxrpm.profile import HYUNDAI_FLAG_CANFD, CarSelection
from helpers import KEY, ems

SELTOS = CarSelection("hyundai", "KIA_SELTOS", 0)
CFG = Config("kia_seltos_2021_ems11", 0, "192.168.43.1", 47816, KEY)
HERE = os.path.dirname(os.path.abspath(__file__))


class Clock:
  def __init__(self):
    self.ns = 10_000_000_000

  def __call__(self):
    return self.ns

  def mono(self):
    return self.ns / 1e9

  def adv(self, ms):
    self.ns += ms * NS_PER_MS


class FakeSrc:
  def __init__(self):
    self.next = None
    self.polls = self.discards = 0
    self.closed = False
    self.rejects = {}

  def poll(self):
    self.polls += 1
    f, self.next = self.next, None
    return f

  def discard(self):
    self.discards += 1
    self.next = None

  def close(self):
    self.closed = True


class FakeSel:
  def __init__(self, sel):
    self.sel = sel

  def read(self):
    return self.sel


class FakeOut:
  def __init__(self, fail=None):
    self.pkts = []
    self.fail = fail
    self.closed = False
    self.attempts = 0

  def send(self, data):
    self.attempts += 1
    if self.fail:
      raise OSError(self.fail, os.strerror(self.fail))
    self.pkts.append(P.decode(data, KEY))

  def close(self):
    self.closed = True


def make(sel=SELTOS, fail=None):
  clk, logs = Clock(), []
  c = Companion(CFG, FakeSrc(), FakeSel(sel), FakeOut(fail), clock_ns=clk, logger=logs.append, mono_s=clk.mono)
  return c, clk, logs


def test_valid_rpm_flow():
  c, clk, logs = make()
  c.src.next = (clk.ns, ems(3200))
  assert c.step()
  p = c.out.pkts[-1]
  assert p.flags & P.FLAG_RPM_VALID and p.rpm_raw == 3200 and p.frame_counter == 1
  assert all(KEY.decode() not in m for m in logs)


def test_unknown_selection_never_uses_frames():
  c, clk, _ = make(sel=None)
  for _ in range(20):
    c.src.next = (clk.ns, ems(3200))
    assert c.step()
    clk.adv(20)
  assert c.src.polls == 0 and c.src.discards == 20
  assert c.out.pkts and all(not p.flags & (P.FLAG_RPM_VALID | P.FLAG_FRAME_SEEN) and p.frame_counter == 0 for p in c.out.pkts)


def test_unsupported_car_and_canfd_exit_with_final_invalid_heartbeat():
  for sel in (CarSelection("hyundai", "KIA_SPORTAGE_5TH_GEN", 0), CarSelection("toyota", "KIA_SELTOS", 0),
              CarSelection("hyundai", "KIA_SELTOS", HYUNDAI_FLAG_CANFD)):
    c, clk, _ = make(sel=sel)
    assert not c.step() and c.exit_code == EXIT_UNSUPPORTED
    assert len(c.out.pkts) == 1 and not c.out.pkts[0].flags & P.FLAG_RPM_VALID
    assert c.src.polls == 0


def test_selection_loss_starts_new_session_and_drops_data():
  c, clk, logs = make()
  c.src.next = (clk.ns, ems(3200))
  c.step()
  old = c.out.pkts[-1]
  assert old.flags & P.FLAG_RPM_VALID
  c.sel_reader.sel = None
  clk.adv(2000)
  c.src.next = (clk.ns, ems(3300))
  c.step()
  p = c.out.pkts[-1]
  assert p.session_id != old.session_id and p.seq == 1
  assert p.frame_counter == 0 and p.rpm_raw == 0 and not p.flags & (P.FLAG_RPM_VALID | P.FLAG_FRAME_SEEN)
  # selection regained: only frames ingested after that point count
  c.sel_reader.sel = SELTOS
  clk.adv(2000)
  c.src.next = (clk.ns, ems(3400))
  c.step()
  p2 = c.out.pkts[-1]
  assert p2.session_id == p.session_id and p2.frame_counter == 1 and p2.rpm_raw == 3400


def test_stale_without_new_frames_goes_invalid():
  c, clk, _ = make()
  c.src.next = (clk.ns, ems(3200))
  c.step()
  for _ in range(20):
    clk.adv(20)
    c.step()
  last = c.out.pkts[-1]
  assert not last.flags & P.FLAG_RPM_VALID and last.frame_age_ms >= 300 and last.frame_counter == 1


def test_network_failure_keeps_cadence_and_rate_limits_log():
  c, clk, logs = make(fail=errno.ENETUNREACH)
  for _ in range(1000):  # 20 s simulated
    clk.adv(20)
    assert c.step()
  assert c.send_errors == c.out.attempts and 90 <= c.out.attempts <= 110  # heartbeat cadence, no retry storm
  assert 2 <= sum("send failed" in m for m in logs) <= 3


def test_run_stops_promptly_and_closes():
  c, _, _ = make()
  c.clock_ns = time.monotonic_ns
  c.mono_s = time.monotonic
  stop = threading.Event()
  t = threading.Thread(target=c.run, args=(stop,))
  t.start()
  time.sleep(0.1)
  t0 = time.monotonic()
  stop.set()
  t.join(1.0)
  assert not t.is_alive() and time.monotonic() - t0 < 0.2
  assert c.out.closed and c.src.closed


def test_drain_is_bounded():
  class Infinite:
    n = 0

    def receive(self, non_blocking=False):
      assert non_blocking
      self.n += 1
      return b"m%d" % self.n
  s = Infinite()
  kept, n = drain_bounded(s)
  assert n == MAX_DRAIN_PER_TICK and s.n == MAX_DRAIN_PER_TICK and len(kept) == KEEP_PER_TICK
  assert kept[-1] == b"m%d" % MAX_DRAIN_PER_TICK


CHILD = r'''
import sys, threading, signal
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[2])
from svxrpm.companion import Companion, UdpOut
from svxrpm.config import Config
from svxrpm.profile import CarSelection
from helpers import KEY, ems
import time
class Src:
  rejects = {}
  def poll(self): return (time.clock_gettime_ns(time.CLOCK_BOOTTIME), ems(3200))
  def discard(self): pass
  def close(self): print("SRC_CLOSED", flush=True)
class Sel:
  def read(self): return CarSelection("hyundai", "KIA_SELTOS", 0)
cfg = Config("kia_seltos_2021_ems11", 0, "127.0.0.1", int(sys.argv[3]), KEY)  # loopback only in this test
stop = threading.Event()
signal.signal(signal.SIGTERM, lambda *a: stop.set())
c = Companion(cfg, Src(), Sel(), UdpOut(cfg.target_ip, cfg.port))
print("READY", flush=True)
sys.exit(c.run(stop))
'''


def test_sigterm_real_udp_child():
  rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  rx.bind(("127.0.0.1", 0))
  rx.settimeout(2.0)
  port = rx.getsockname()[1]
  proc = subprocess.Popen([sys.executable, "-I", "-c", CHILD, os.path.dirname(HERE), HERE, str(port)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
  try:
    assert proc.stdout.readline().strip() == "READY"
    pkts = [P.decode(rx.recv(64), KEY) for _ in range(10)]
    assert all(p.flags & P.FLAG_RPM_VALID and p.rpm_raw == 3200 for p in pkts)
    assert [p.seq for p in pkts] == sorted(p.seq for p in pkts)
    t0 = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=2.0)
    assert proc.returncode == 0 and time.monotonic() - t0 < 1.0
    assert "SRC_CLOSED" in out and "stopped:" in err and KEY.decode() not in out + err
  finally:
    if proc.poll() is None:
      proc.kill()
    rx.close()

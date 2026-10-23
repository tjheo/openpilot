"""LiveServer over REAL loopback TCP sockets (loopback allowed only through explicit test injection)."""
import socket
import time

import pytest

from openpilot.selfdrive.svx_live import server as S
from openpilot.selfdrive.svx_live import wire as W
from openpilot.selfdrive.svx_live.netpolicy import AddressPolicy
from openpilot.selfdrive.svx_live.tests.helpers import Client, FakeClock

OK = {"frame_counter": 5, "rpm_raw": 12800, "frame_age_ms": 10, "status": W.STATUS_OK, "flags": 3}
NO_CAR = {"frame_counter": 0, "rpm_raw": 0, "frame_age_ms": 0xFFFF, "status": W.STATUS_WAIT_CAR, "flags": 0}


@pytest.fixture
def srv():
  servers = []

  def make(policy=None, **kw):
    s = S.LiveServer(0, policy or AddressPolicy(allow_loopback=True), host="127.0.0.1", **kw)
    assert s.open()
    servers.append(s)
    return s
  yield make
  for s in servers:
    s.close()


def pump_of(s, fields=None):
  def pump():
    if fields is not None:
      s.publish(fields)
    s.poll(0.002)
  return pump


def handshake(s, c, nonce=0x1234, fields=OK):
  c.send(W.hello(nonce))
  frames, eof = c.recv_frames(pump_of(s, fields), until=lambda fs: len(fs) >= 2)
  assert not eof and frames[0].type == W.T_WELCOME, frames
  sid, echo, hb, to = frames[0].fields
  assert sid != 0 and echo == nonce and hb == S.HEARTBEAT_MS and to == S.CLIENT_TIMEOUT_MS
  assert frames[1].type == W.T_RPM and frames[1].fields[0] == sid and frames[1].fields[1] == 1  # first telemetry right away
  return sid, frames


def test_handshake_ping_pong_and_telemetry(srv):
  s = srv()
  c = Client(s.bound_port)
  sid, frames = handshake(s, c)
  assert frames[1].fields[2:7] == (5, 12800, 10, W.STATUS_OK, 3)
  c.send(W.ping(sid, 1))
  frames, _ = c.recv_frames(pump_of(s, OK), until=lambda fs: any(f.type == W.T_PONG for f in fs))
  assert any(f.type == W.T_PONG and f.fields == (sid, 1) for f in frames)
  c.close()


def test_heartbeat_cadence_and_status_change_is_immediate(srv):
  s = srv()
  c = Client(s.bound_port)
  sid, _ = handshake(s, c)
  t0 = time.monotonic()
  frames, _ = c.recv_frames(pump_of(s, OK), until=lambda fs: len(fs) >= 3, timeout=1.5)
  dt = time.monotonic() - t0
  assert len(frames) == 3 and 0.45 <= dt < 1.2  # unchanged data: heartbeats every 200 ms only
  seqs = [f.fields[1] for f in frames]
  assert seqs == sorted(seqs) and len(set(seqs)) == 3
  t0 = time.monotonic()
  frames, _ = c.recv_frames(pump_of(s, dict(OK, status=W.STATUS_STALE, flags=2)), until=lambda fs: bool(fs))
  assert frames[0].fields[5] == W.STATUS_STALE and time.monotonic() - t0 < 0.15
  c.close()


def test_new_frames_rate_limited_to_50hz(srv):
  s = srv()
  c = Client(s.bound_port)
  handshake(s, c)
  n = [5]

  def pump():
    n[0] += 1
    s.publish(dict(OK, frame_counter=n[0]))
    s.poll(0.0005)
  t0 = time.monotonic()
  frames, _ = c.recv_frames(pump, until=lambda fs: time.monotonic() - t0 > 1.0, timeout=1.2)
  assert 30 <= len(frames) <= 52
  c.close()


def test_partial_hello_byte_by_byte(srv):
  s = srv()
  c = Client(s.bound_port)
  for b in W.hello(77):
    c.send(bytes([b]))
    s.poll(0.002)
  frames, _ = c.recv_frames(pump_of(s, OK), until=lambda fs: len(fs) >= 2)
  assert frames[0].type == W.T_WELCOME and frames[0].fields[1] == 77
  c.close()


@pytest.mark.parametrize("first,reason", [
  (W.ping(1, 1), W.REJECT_BAD_HELLO),                          # not a HELLO
  (b"GET / HTTP/1.1\r\n\r\n", W.REJECT_BAD_HELLO),             # garbage
  (W._encode_raw(W.T_HELLO, W.HELLO.pack(5, 0, 0), version=2), W.REJECT_VERSION),
  (W._encode_raw(W.T_HELLO, W.HELLO.pack(0, 0, 0)), W.REJECT_BAD_HELLO),  # zero nonce
])
def test_malformed_handshake_rejected_and_closed(srv, first, reason):
  s = srv()
  c = Client(s.bound_port)
  c.send(first)
  frames, eof = c.recv_frames(pump_of(s, OK), until=lambda fs: False, timeout=1.0)
  assert eof and [f.type for f in frames] == [W.T_REJECT] and frames[0].fields[0] == reason
  assert s.conns == {}
  c.close()


def test_handshake_timeout_closes_silently(srv):
  clk = FakeClock()
  s = srv(mono=clk)
  c = Client(s.bound_port)
  s.poll(0.05)
  assert len(s.conns) == 1
  clk.adv(S.HANDSHAKE_TIMEOUT_S + 0.1)
  frames, eof = c.recv_frames(pump_of(s), until=lambda fs: False, timeout=0.5)
  assert eof and frames == [] and s.stats["handshake_timeout"] == 1
  c.close()


def test_session_protocol_errors(srv):
  s = srv()
  for bad in (lambda sid: W.hello(9), lambda sid: W.ping(sid ^ 1, 1), lambda sid: W.ping(sid, 5) + W.ping(sid, 5),
              lambda sid: b"\x00" * 12):
    c = Client(s.bound_port)
    sid, _ = handshake(s, c)
    c.send(bad(sid))
    frames, eof = c.recv_frames(pump_of(s, OK), until=lambda fs: any(f.type == W.T_BYE for f in fs), timeout=1.0)
    byes = [f for f in frames if f.type == W.T_BYE]
    assert byes and byes[-1].fields[:2] == (sid, W.BYE_PROTOCOL_ERROR)
    c.close()
  assert s.sessions == []


def test_flood_is_rate_limited(srv):
  s = srv()
  c = Client(s.bound_port)
  sid, _ = handshake(s, c)
  c.send(b"".join(W.ping(sid, i) for i in range(1, 3)))  # 2 frames per recv chunk
  for i in range(3, 40, 2):
    try:
      c.send(W.ping(sid, i) + W.ping(sid, i + 1))
    except OSError:
      break
    s.poll(0.001)
  frames, eof = c.recv_frames(pump_of(s, OK), until=lambda fs: any(f.type == W.T_BYE for f in fs), timeout=1.0)
  assert any(f.type == W.T_BYE and f.fields[1] == W.BYE_RATE_LIMIT for f in frames)
  assert s.sessions == [] and s.stats["rate_limit"] == 1
  c.close()


def test_max_sessions_busy_and_same_address_replaces(srv):
  s = srv()
  a = Client(s.bound_port, src="127.0.0.1")
  b = Client(s.bound_port, src="127.0.0.2")
  sid_a, _ = handshake(s, a)
  handshake(s, b)
  c = Client(s.bound_port, src="127.0.0.3")
  c.send(W.hello(3))
  frames, eof = c.recv_frames(pump_of(s, OK), until=lambda fs: False, timeout=0.6)
  assert eof and frames[0].type == W.T_REJECT and frames[0].fields[0] == W.REJECT_BUSY
  # the same phone reconnecting replaces its own old session instead of being locked out
  a2 = Client(s.bound_port, src="127.0.0.1")
  sid_a2, _ = handshake(s, a2)
  frames, eof = a.recv_frames(pump_of(s, OK), until=lambda fs: any(f.type == W.T_BYE for f in fs), timeout=1.0)
  assert any(f.type == W.T_BYE and f.fields[:2] == (sid_a, W.BYE_REPLACED) for f in frames)
  assert sid_a2 != sid_a and len(s.sessions) == 2
  for x in (a, b, c, a2):
    x.close()


def test_pending_handshakes_are_capped(srv):
  s = srv()
  idle = [Client(s.bound_port, src=f"127.0.0.{10 + i}") for i in range(S.MAX_PENDING)]
  s.poll(0.05)
  extra = Client(s.bound_port, src="127.0.0.20")
  frames, eof = extra.recv_frames(pump_of(s), until=lambda fs: False, timeout=0.5)
  assert eof and frames and frames[0].fields[0] == W.REJECT_BUSY
  assert sum(1 for x in s.conns.values() if not x.session_id) == S.MAX_PENDING
  for x in idle + [extra]:
    x.close()


def test_accept_rate_limited(srv):
  s = srv()
  clients = []
  for _ in range(S.ACCEPT_BURST + 6):
    clients.append(Client(s.bound_port))
    for x in list(s.conns.values()):  # keep pending slots free so only the accept bucket limits
      s._drop(x, None, None)
    s.poll(0.001)
  for _ in range(10):
    s.poll(0.001)
  assert s.stats.get("accept_rate", 0) >= 4
  for x in clients:
    x.close()


def test_non_lan_peer_gets_zero_bytes(srv):
  s = srv(policy=AddressPolicy(allow_loopback=False))  # production policy: loopback is not a LAN peer
  c = Client(s.bound_port)
  c.send(W.hello(1))
  frames, eof = c.recv_frames(pump_of(s, OK), until=lambda fs: False, timeout=0.5)
  assert eof and frames == [] and s.stats["address_rejected"] == 1 and s.conns == {}
  c.close()


def test_client_timeout(srv):
  clk = FakeClock()
  s = srv(mono=clk)
  c = Client(s.bound_port)
  sid, _ = handshake(s, c)
  clk.adv(S.CLIENT_TIMEOUT_S - 0.1)
  s.poll(0.01)
  assert len(s.sessions) == 1
  clk.adv(0.2)
  frames, eof = c.recv_frames(pump_of(s, OK), until=lambda fs: any(f.type == W.T_BYE for f in fs))
  assert any(f.type == W.T_BYE and f.fields == (sid, W.BYE_CLIENT_TIMEOUT, 0, 0) for f in frames) and s.sessions == []
  c.close()


def test_kernel_queue_backlog_skips_telemetry_then_drops_slow_reader(srv):
  clk = FakeClock()
  outq = {"v": 0}
  s = srv(mono=clk, outq_fn=lambda sock: outq["v"])
  c = Client(s.bound_port)
  sid, _ = handshake(s, c)
  outq["v"] = S.OUTQ_SKIP + 1  # the peer stopped acknowledging; nothing is in the user-space buffer
  sent_before = s.sessions[0].seq
  for i in range(20):
    clk.adv(0.05)
    c.send(W.ping(sid, i + 1)) if i % 8 == 0 else None
    s.publish(dict(OK, frame_counter=100 + i))
    s.poll(0.001)
  assert s.sessions[0].seq == sent_before and s.stats["telemetry_skipped"] >= 19  # dropped, never queued
  clk.adv(S.SLOW_S + 0.1)
  s.poll(0.001)
  assert s.sessions == [] and s.stats["slow_reader"] == 1
  c.close()


def test_user_space_buffer_is_bounded(srv):
  s = srv()
  c = Client(s.bound_port)
  handshake(s, c)
  conn = s.sessions[0]
  conn.out += b"x" * (S.OUT_MAX - 10)  # simulate a partially written backlog
  assert not s._send(conn, W.pong(conn.session_id, 1), droppable=False)  # would exceed OUT_MAX
  assert s.sessions == [] and s.stats["slow_reader"] == 1
  c.close()


def test_peer_disconnect_and_reset_are_handled(srv):
  s = srv()
  c = Client(s.bound_port)
  handshake(s, c)
  c.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")  # RST on close
  c.close()
  for _ in range(20):
    s.publish(OK)
    s.poll(0.005)
  assert s.sessions == []


def test_end_sessions_and_close_send_bye(srv):
  s = srv()
  c = Client(s.bound_port)
  sid, _ = handshake(s, c, fields=NO_CAR)
  s.end_sessions(W.BYE_CAR_CHANGED)
  frames, eof = c.recv_frames(pump_of(s), until=lambda fs: False, timeout=0.5)
  assert eof and frames[-1].fields == (sid, W.BYE_CAR_CHANGED, 0, 0)
  assert s.listening  # listener stays: the phone reconnects for a new session
  c.close()
  c = Client(s.bound_port)
  sid, _ = handshake(s, c, fields=NO_CAR)
  s.close()
  frames, eof = c.recv_frames(lambda: None, until=lambda fs: False, timeout=0.5)
  assert eof and frames[-1].fields == (sid, W.BYE_STOPPING, 0, 0)
  assert not s.listening and s.conns == {}
  with pytest.raises(OSError):
    Client(s.bound_port or 1)
  c.close()


def test_bind_failure_is_bounded_not_raised():
  hold = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  hold.bind(("127.0.0.1", 0))
  hold.listen(1)
  logs = []
  s = S.LiveServer(hold.getsockname()[1], AddressPolicy(allow_loopback=True), host="127.0.0.1", logger=logs.append)
  try:
    assert not s.open() and not s.open()
    assert s.stats["bind_error"] == 2 and len(logs) == 1  # rate-limited log
    s.poll(0.0)  # no listener: returns immediately, never raises
  finally:
    s.close()
    hold.close()


def test_selector_failure_closes_and_reports(srv):
  s = srv()
  c = Client(s.bound_port)
  handshake(s, c)

  def boom(timeout=None):
    raise OSError("selector broke")
  s.sel.select = boom
  s.poll(0.01)
  assert not s.listening and s.conns == {} and s.stats["select_error"] == 1
  assert s.open()  # recoverable by a later open()
  c.close()


def test_failing_logger_never_breaks_the_server():
  def bad_log(msg):
    raise RuntimeError("log sink down")
  s = S.LiveServer(0, AddressPolicy(allow_loopback=True), host="127.0.0.1", logger=bad_log)
  try:
    assert s.open()
    c = Client(s.bound_port)
    handshake(s, c)
    c.close()
  finally:
    s.close()


def test_poll_is_one_round_even_when_flooded(srv):
  s = srv()
  c = Client(s.bound_port)
  sid, _ = handshake(s, c)
  c.send(b"\x00" * 4)  # always-readable socket (partial garbage that never completes a header)
  t0 = time.monotonic()
  s.poll(0.0)
  assert time.monotonic() - t0 < 0.05
  c.close()

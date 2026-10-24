"""svxLiveDiag: bounded, rate-limited diagnostic summaries. REAL log.capnp events through CerealCanSource (socket
double) and the real LiveDaemon with a fake clock/server, so cadence and freshness can be compared exactly."""
import json

import pytest

pytest.importorskip("capnp")
cereal = pytest.importorskip("openpilot.cereal")
log = cereal.log

from openpilot.selfdrive.svx_live import diagnostics as G
from openpilot.selfdrive.svx_live import svx_lived as D
from openpilot.selfdrive.svx_live import wire as W
from openpilot.selfdrive.svx_live.cereal_source import KEEP_PER_TICK, MAX_DRAIN_PER_TICK, CerealCanSource
from openpilot.selfdrive.svx_live.profile import CarSelection
from openpilot.selfdrive.svx_live.rpm_state import INVALID
from openpilot.selfdrive.svx_live.tests.helpers import FakeClock, ems

SELTOS = CarSelection("hyundai", "KIA_SELTOS", 0)
COMMIT = "f63ab7cefb7fa39bd1c71802f512cb704569d38b"
SECRET = "SECRET-PAYLOAD-1234"


def can_bytes(t_ns, frames, valid=True):
  e = log.Event.new_message(logMonoTime=t_ns, valid=valid)
  c = e.init("can", len(frames))
  for i, (addr, src, dat) in enumerate(frames):
    c[i].address, c[i].src, c[i].dat = addr, src, dat
  return e.to_bytes()


def log_from_bytes(dat):
  with log.Event.from_bytes(dat, traversal_limit_in_words=2**64 - 1) as msg:
    return msg


class Sock:
  def __init__(self, msgs=()):
    self.msgs = list(msgs)
    self.error = None

  limit = None  # optional: messages returned per poll before the queue looks empty

  def receive(self, non_blocking=False):
    if self.error is not None:
      raise self.error
    if self.limit is not None:
      if self.limit <= 0:
        return None
      self.limit -= 1
    return self.msgs.pop(0) if self.msgs else None


def source(msgs=(), stats=None):
  s = CerealCanSource.__new__(CerealCanSource)  # bypass msgq socket creation only
  s._log_from_bytes, s._sock, s.address, s.bus, s.rejects, s.drain_caps = log_from_bytes, Sock(msgs), 0x316, 0, {}, 0
  s.stats = stats
  return s


class Emit:
  def __init__(self, fail=False):
    self.records, self.fail = [], fail

  def __call__(self, event, **kw):
    if self.fail:
      raise RuntimeError(SECRET)
    assert event == G.EVENT
    json.dumps(kw, allow_nan=False)  # what swaglog will serialize
    self.records.append(kw)

  def kinds(self, kind):
    return [r for r in self.records if r["kind"] == kind]


class FakeServer:
  listening = True

  def __init__(self):
    self.stats, self.sessions, self.published = {}, [], []

  def open(self):
    return True

  def end_sessions(self, reason):
    pass

  def publish(self, fields):
    self.published.append(dict(fields))

  def poll(self, timeout):
    pass

  def close(self, reason=W.BYE_STOPPING):
    pass


class Rig:
  """Real LiveDaemon on a fake clock: each step advances exactly TICK_S of fake time."""

  def __init__(self, diag=True, enabled=True, emit=None, factory=None, revision="ok"):
    self.clock = FakeClock(100.0)
    self.emit = emit or Emit()
    self.revision_calls = 0
    self.enabled = enabled
    self.diag = G.LiveDiagnostics(self.emit, revision_fn=self._revision) if diag else None
    self.revision = COMMIT if revision == "ok" else revision
    self.sock = Sock()
    self.sources = []
    self.server = FakeServer()
    self.d = D.LiveDaemon(self.server, lambda: self.enabled, type("S", (), {"read": lambda s: SELTOS})(),
                          factory or self._factory, clock_ns=lambda: int(self.clock.t * 1e9), mono=self.clock,
                          logger=lambda m: None, sleep=self.clock.adv, diag=self.diag)

  def _revision(self):
    self.revision_calls += 1
    return self.revision

  def _factory(self, prof):
    s = source(stats=self.diag.source_stats if self.diag else None)
    s._sock = self.sock
    self.sources.append(s)
    return s

  def now_ns(self):
    return int(self.clock.t * 1e9)

  def steps(self, n, feed=None):
    for i in range(n):
      if feed is not None:
        feed(self, i)
      assert self.d.step()


def summaries(rig):
  return rig.emit.kinds("summary")


def test_disabled_emits_nothing_reads_nothing_subscribes_nothing():
  r = Rig(enabled=False)
  assert r.d.step() is False
  r.d.close()
  assert r.emit.records == [] and r.revision_calls == 0 and r.sources == [] and not r.diag.active


def test_without_diag_the_daemon_behaves_as_before():
  r = Rig(diag=False)
  r.steps(5)
  assert r.d.diag is None and r.sources[0].stats is None


def test_start_and_summary_cadence_for_true_no_event():
  r = Rig()
  r.steps(int(25.5 / D.TICK_S))  # 25.5 s of fake time with an empty `can` queue
  start = r.emit.kinds("start")
  assert len(start) == 1 and start[0]["commit"] == COMMIT and r.revision_calls == 1
  assert start[0]["address"] == 0x316 and start[0]["fallback_address"] == 0x366 and start[0]["bus"] == [0] and start[0]["max_drain"] == MAX_DRAIN_PER_TICK
  assert start[0]["keep"] == KEEP_PER_TICK and start[0]["protocol"] == W.VERSION
  s = summaries(r)
  assert [round(x["t"]) for x in s] == [3, 13, 23]  # first after FIRST_SUMMARY_S, then every SUMMARY_PERIOD_S
  last = s[-1]
  assert last["status"] == "no_frame" and last["sel"] == "ok" and last["src"] is True
  assert last["can"]["polls"] == last["can"]["empty_polls"] == last["ticks"] and abs(last["ticks"] - 500) <= 1
  assert last["can"]["drained"] == 0 and last["can"]["events_valid"] == 0 and last["can"]["rx_frames"] == {}
  assert last["total_events"] == 0 and last["since_match_ms"] is None and last["event_age_ms"] is None
  assert last["status_ticks"] == {"no_frame": last["ticks"]} and last["late_ticks"] == 0
  assert last["tick_gap_ms"]["max"] == pytest.approx(D.TICK_S * 1000)
  r.d.close()
  r.d.close()  # run()'s finally plus an explicit close: still one stop record
  assert r.emit.records[-1]["kind"] == "stop" and len(r.emit.kinds("stop")) == 1


def test_events_without_target_and_other_bus_target_count_only():
  r = Rig()

  def feed(rig, i):  # EMS11 only on bus 2 and as TX echo; other traffic on bus 0
    rig.sock.msgs.append(can_bytes(rig.now_ns(), [(0x2B0, 0, b"\x00" * 8), (0x316, 2, ems(3200)), (0x316, 128, ems(3200)),
                                                  (0x260, 1, b"\x00" * 8)]))
  r.steps(int(3.1 / D.TICK_S), feed)
  s = summaries(r)[0]
  c, n = s["can"], s["ticks"]
  assert c["rx_frames"] == {"0": n, "1": n, "2": n} and c["rx_target"] == {"2": n}
  assert c["tx_frames"] == n and c["tx_target"] == n and c["matches"] == 0 and c["poll_none"] == n
  assert s["accepted"] == 0 and s["status"] == "no_frame" and r.d.state.frame_counter == 0  # never accepted
  assert s["event_age_ms"]["max"] == 0.0 and s["total_target_rx"] == n


def test_match_accept_and_reject_reasons():
  r = Rig()

  def feed(rig, i):
    t = rig.now_ns() - (400_000_000 if i % 2 else 5_000_000)  # every other frame is older than the 300 ms limit
    rig.sock.msgs.append(can_bytes(t, [(0x316, 0, ems(3200))]))
  r.steps(int(3.1 / D.TICK_S), feed)
  s = summaries(r)[0]
  n = s["ticks"]
  assert s["can"]["matches"] == n and s["can"]["rx_target"] == {"0": n}
  assert s["accepted"] + s["state_rejects"]["too_old"] == n and abs(s["accepted"] - n / 2) <= 1
  assert s["sample_reasons"] == {"ok": s["accepted"]}
  assert s["since_accept_ms"] is not None and s["since_match_ms"] == 0
  assert "rpm" not in json.dumps(r.emit.records)  # no RPM field or value names in any record


def test_drain_saturation_and_discard_accounting():
  r = Rig()
  burst = [can_bytes(1, [(0x316, 0, ems(1))])] * (MAX_DRAIN_PER_TICK + 100)

  def feed(rig, i):
    if i == 10:
      rig.sock.msgs.extend(burst)
  r.steps(int(3.1 / D.TICK_S), feed)
  c = summaries(r)[0]["can"]
  assert c["drained"] == MAX_DRAIN_PER_TICK + 100 and c["drain_saturated"] == 1
  assert c["parsed"] == 2 * KEEP_PER_TICK and c["drain_discarded"] == MAX_DRAIN_PER_TICK + 100 - 2 * KEEP_PER_TICK
  assert c["events_valid"] == 2 * KEEP_PER_TICK  # census covers parsed messages only
  assert r.sources[0].drain_caps == 1


def test_subscriber_error_is_categorized_and_message_never_logged():
  def factory(prof):
    raise OSError(SECRET)
  r = Rig(factory=factory)
  r.steps(int(3.1 / D.TICK_S))
  s = summaries(r)[0]
  assert s["subscriber_errors"] == 2 and s["subscriber_error_types"] == {"os": 2} and s["src"] is False  # 2 s recheck
  assert s["can"]["polls"] == 0 and SECRET not in json.dumps(r.emit.records)


def test_read_error_parse_error_and_no_event_are_distinct():
  st = G.SourceStats()
  s = source([can_bytes(10, [(0x316, 0, ems(5))])], stats=st)
  s.poll()
  assert st.newest_event_ns == 10
  s._sock.error = OSError(SECRET)
  with pytest.raises(OSError):  # unchanged: the daemon still turns this into source_error / INVALID
    s.poll()
  assert st.w["read_errors"] == 1 and st.read_error_types == {"os": 1} and st.newest_event_ns is None
  s._sock.error = None
  s._sock.msgs = [b"\x01\x02garbage"]
  assert s.poll() == (INVALID, "invalid_event")  # unchanged INVALID semantics for unparseable newest
  assert st.w["parse_errors"] == 1 and st.w["events_valid"] == 1 and st.w["events_invalid"] == 0
  assert s.poll() is None
  assert st.w["empty_polls"] == 1 and st.w["poll_none"] == 1 and st.w["read_errors"] == 1
  s._sock.msgs = [can_bytes(20, [(0x316, 0, ems(5))], valid=False)]
  assert s.poll() == (INVALID, "invalid_event") and st.w["events_invalid"] == 1 and st.w["poll_invalid"] == 2


def test_daemon_read_error_is_source_error():
  r = Rig()
  r.steps(1)
  r.sock.error = ConnectionError(SECRET)
  r.steps(int(3.1 / D.TICK_S))
  s = summaries(r)[0]
  assert s["source_errors"] == s["can"]["read_errors"] > 0 and s["can"]["read_error_types"] == {"connection": s["source_errors"]}
  assert s["status"] == "invalid" and s["state_rejects"] == {"source_error": s["source_errors"]}
  assert SECRET not in json.dumps(r.emit.records)


def test_census_budget_counts_actual_scanned_frames_across_events():
  st = G.SourceStats()
  evts = [log_from_bytes(can_bytes(t, [(0x316, 0, ems(1))] * 1500)) for t in (1, 2, 3)]
  st.census(evts, 0x316)
  assert st.w["census_frames"] == G.CENSUS_MAX_FRAMES_PER_TICK and st.w["census_skipped_frames"] == 3 * 1500 - 2048
  assert st.rx_frames == {0: 2048} and st.w["events_valid"] == 3 and st.newest_event_ns == 3


def test_census_never_changes_poll_results():
  import random
  rnd = random.Random(7)
  msgs = []
  for i in range(400):
    k = rnd.random()
    if k < 0.05:
      msgs.append(b"\x00garbage")
    else:
      frames = [(rnd.choice((0x316, 0x2B0)), rnd.choice((0, 1, 2, 128)), ems(rnd.randrange(0, 5000)) if rnd.random() > .1 else b"\x00")
                for _ in range(rnd.randrange(0, 6))]
      msgs.append(can_bytes(1000 + i, frames, valid=k > 0.1))
  plain, counted = source(list(msgs)), source(list(msgs), stats=G.SourceStats())
  for _ in range(450):  # default drain: up to 512 per poll, so feed in irregular slices
    n = rnd.randrange(0, 4)
    plain._sock.limit = counted._sock.limit = n
    assert plain.poll() == counted.poll()
  assert plain.rejects == counted.rejects


def test_cadence_and_freshness_identical_with_and_without_diag():
  def feed(rig, i):
    if i % 3:
      rig.sock.msgs.append(can_bytes(rig.now_ns() - 1_000_000, [(0x316, 0, ems(3200 + i)), (0x316, 1, ems(1))]))
    if i % 37 == 0:
      rig.sock.msgs.append(can_bytes(rig.now_ns(), [], valid=False))
  a, b = Rig(diag=False), Rig()
  a.steps(1200, feed)
  b.steps(1200, feed)
  assert a.server.published == b.server.published and a.clock.t == b.clock.t
  assert a.d.state.frame_counter == b.d.state.frame_counter > 0 and a.d.state.rejects == b.d.state.rejects


def test_transition_logs_are_rate_limited():
  r = Rig()

  def feed(rig, i):  # flapping newest evidence: OK / INVALID every tick (50 Hz transitions)
    rig.sock.msgs.append(can_bytes(rig.now_ns(), [(0x316, 0, ems(3200))], valid=bool(i % 2)))
  r.steps(int(25.5 / D.TICK_S), feed)
  tr = r.emit.kinds("transition")
  ts = [x["t"] for x in tr]
  assert all(b - a >= G.TRANSITION_MIN_GAP_S for a, b in zip(ts, ts[1:], strict=False))
  per_window = [sum(1 for t in ts if lo <= t < hi) for lo, hi in ((0, 3), (3, 13), (13, 23))]
  assert all(n <= G.TRANSITION_LOGS_PER_WINDOW for n in per_window) and len(tr) <= 4 * G.TRANSITION_LOGS_PER_WINDOW
  s = summaries(r)[1]
  assert s["status_changes"] >= 400 and s["transition_logs_suppressed"] == s["status_changes"] - G.TRANSITION_LOGS_PER_WINDOW
  assert len(r.emit.records) < 20  # 25 s of 50 Hz flapping: start + 3 summaries + a few transitions


def test_logger_failure_never_affects_the_daemon():
  def feed(rig, i):
    rig.sock.msgs.append(can_bytes(rig.now_ns(), [(0x316, 0, ems(3200))]))
  ok, bad = Rig(diag=False), Rig(emit=Emit(fail=True))
  ok.steps(800, feed)
  bad.steps(800, feed)
  bad.d.close()
  assert ok.server.published == bad.server.published and bad.d.errors == 0
  assert bad.diag.emit_errors >= 3 and bad.diag.records == 0


def test_hook_failure_is_contained():
  r = Rig()
  r.diag.observe = lambda *a: (_ for _ in ()).throw(RuntimeError(SECRET))
  r.steps(int(3.1 / D.TICK_S))
  assert r.d.errors == 0 and r.d.state is not None and r.server.published[-1]["status"] == W.STATUS_NO_FRAME


def test_bounded_keys_counters_bytes_and_privacy_at_saturation():
  st = G.SourceStats()
  evts = [log_from_bytes(can_bytes(5, [(0x316, b, bytes(range(8))) for b in range(256)]))]
  st.census(evts, 0x316)
  assert len(st.rx_frames) == len(st.rx_target) == G.MAX_BUS_KEYS + 1 and st.tx_frames == 128
  for k in st.w:
    st.w[k] = 10 ** 30
  dg = G.LiveDiagnostics(Emit())
  dg.source_stats = st
  dg.start(0.0, {})
  for i in range(10_000):
    dg.observe(float(i), i % 300, "x" * 100 if i % 2 else "ok", bool(i % 3))
    dg.accepted(float(i), "weird" * 50)
    dg.subscriber_error(type("Secreté" + "X" * 100, (Exception,), {})(SECRET))
    dg.event_age(float("inf") if i % 2 else -10 ** 20)
  snap = {"state_rejects": {f"r{i}é": 10 ** 40 for i in range(100)}, "server": {f"k{i}": i for i in range(100)} | {"Bad Key\n": 1},
          "sessions": 10 ** 40, "tick_errors": -5}
  dg.summary(20_000.0, snap)
  rec = dg._emit.records[-1]
  text = json.dumps(dg._emit.records, ensure_ascii=False, allow_nan=False)
  assert rec["kind"] == "summary" and "truncated" not in rec
  assert all(G.record_bytes(x) <= G.MAX_RECORD_BYTES for x in dg._emit.records)
  assert SECRET not in text and "Secret" not in text and "weird" not in text and "xxxx" not in text and "é" not in text
  assert rec["subscriber_error_types"] == {"other": 10_000} and rec["sample_reasons"] == {"other": 10_000}
  assert rec["state_rejects"] == {"other": G.COUNTER_MAX} and len(rec["server"]) <= G.MAX_SERVER_KEYS
  assert rec["sessions"] == G.COUNTER_MAX and rec["tick_errors"] == 0 and rec["can"]["drained"] == G.COUNTER_MAX
  assert rec["event_age_ms"]["max"] == G.FLOAT_MAX and rec["event_age_ms"]["min"] == -G.FLOAT_MAX
  assert set(rec["status_ticks"]) <= set(G.STATUS_NAMES.values()) | {"unknown"}
  assert not any(k in r for r in dg._emit.records for k in ("error", "debug"))  # would change the cloudlog level
  # an oversized record is replaced, never emitted
  dg._send("summary", {"x": "y" * (G.MAX_RECORD_BYTES + 1)})
  assert dg._emit.records[-1] == {"kind": "summary", "v": G.DIAG_VERSION, "truncated": True}


def test_commit_validation():
  assert G.valid_commit(COMMIT) == COMMIT and G.valid_commit(COMMIT.upper().encode()) == COMMIT
  for bad in (None, "", "main", COMMIT[:-1], COMMIT + "0", "g" * 40, b"\xff" * 40, 123, f"{COMMIT[:39]}é"):
    assert G.valid_commit(bad) == "unknown"
  r = Rig(revision="not-a-commit SECRET")
  r.steps(1)
  assert r.emit.kinds("start")[0]["commit"] == "unknown"
  r = Rig(revision=None)
  r.diag._revision_fn = lambda: (_ for _ in ()).throw(RuntimeError(SECRET))
  r.steps(1)
  assert r.emit.kinds("start")[0]["commit"] == "unknown" and SECRET not in json.dumps(r.emit.records)


def test_server_session_flood_does_not_amplify_logs():
  r = Rig()
  r.steps(1)
  for _ in range(int(25.5 / D.TICK_S)):
    r.server.stats["accepted"] = r.server.stats.get("accepted", 0) + 5  # 250 reconnects/s
    r.server.stats["peer_closed"] = r.server.stats.get("peer_closed", 0) + 5
    r.d.step()
  s = summaries(r)
  assert len(r.emit.records) == 1 + len(s) == 4
  n = s[1]["ticks"]
  assert s[1]["server"] == {"accepted": 5 * n, "peer_closed": 5 * n}


def test_snapshot_uses_the_real_server_api():
  from openpilot.selfdrive.svx_live.netpolicy import AddressPolicy
  from openpilot.selfdrive.svx_live.server import LiveServer
  from openpilot.selfdrive.svx_live.tests.helpers import Client
  server = LiveServer(0, AddressPolicy(allow_loopback=True), host="127.0.0.1")
  emit = Emit()
  d = D.LiveDaemon(server, lambda: True, type("S", (), {"read": lambda s: SELTOS})(), lambda p: source(),
                   logger=lambda m: None, diag=G.LiveDiagnostics(emit))
  try:
    d.step()
    snap = d._diag_snapshot()
    assert snap["sessions"] == 0 and snap["listening"] is True and snap["server"] is server.stats
    c = Client(server.bound_port)
    c.send(W.hello(42))
    c.recv_frames(lambda: d.step(), until=lambda fs: len(fs) >= 2)
    assert d._diag_snapshot()["sessions"] == 1
    c.close()
  finally:
    d.close()
  stop = emit.kinds("stop")[-1]
  assert stop["listening"] is True and stop["server"]["sessions"] == 1  # pre-close snapshot by design


def test_existing_text_logs_carry_category_only_and_subscriber_log_is_rate_limited():
  logs = []

  def factory(prof):
    raise OSError(SECRET)
  r = Rig(factory=factory)
  r.d._logger = logs.append
  r.steps(int(25.5 / D.TICK_S))  # subscriber retried every 2 s, logged at most every 10 s
  sub = [m for m in logs if "can subscriber unavailable" in m]
  assert len(sub) == 3 and all("(os)" in m for m in sub)
  r.d._work = lambda now, deadline: (_ for _ in ()).throw(RuntimeError(SECRET))
  r.steps(3)
  assert any("tick failed (runtime)" in m for m in logs) and not any(SECRET in m for m in logs)
  assert summaries(r)[-1]["subscriber_errors"] == 5


def test_broken_diagnostics_never_skip_invalidation_or_change_poll():
  r = Rig()
  r.steps(1)
  r.diag.source_error = lambda: (_ for _ in ()).throw(RuntimeError("diag broke"))
  r.sources[0].poll = lambda: (_ for _ in ()).throw(RuntimeError("source broke"))
  r.steps(2)
  assert r.d.state.invalid_reason == "source_error" and r.server.published[-1]["status"] == W.STATUS_INVALID
  assert r.d.errors == 0 and r.diag.w["diag_errors"] >= 2

  class Broken:
    def __getattr__(self, name):
      raise RuntimeError("stats broke")
  msgs = [can_bytes(5, [(0x316, 0, ems(3200))]), b"\x00garbage", can_bytes(9, [(0x316, 0, ems(3300))]),
          can_bytes(11, [(0x316, 0, ems(1))], valid=False)]
  for split in ([0, 1], [1, 2], [2, 3], [3, 4]):
    plain, broken = source(msgs[split[0]:split[1]]), source(msgs[split[0]:split[1]], stats=Broken())
    assert plain.poll() == broken.poll() and plain.rejects == broken.rejects
  plain, broken = source(), source(stats=Broken())
  plain._sock.error = broken._sock.error = OSError("x")
  for s in (plain, broken):
    with pytest.raises(OSError):
      s.poll()

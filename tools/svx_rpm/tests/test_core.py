from svxrpm import protocol as P
from svxrpm.core import NS_PER_MS, RpmState, Sender, latest_frame
from helpers import KEY, ems, event, frame

A = 0x316
T0 = 5_000_000_000


def test_latest_frame_correct_bus_only():
  evs = [event(T0, [frame(A, 0, ems(100)), frame(A, 1, ems(200)), frame(A, 128, ems(300)), frame(0x317, 0, ems(400))])]
  assert latest_frame(evs, A, 0) == (T0, ems(100))
  assert latest_frame(evs, A, 1) == (T0, ems(200))
  assert latest_frame(evs, A, 2) is None
  # TX echo of bus 0 is src 128: never matched by a receive bus
  assert latest_frame([event(T0, [frame(A, 128, ems(1))])], A, 0) is None


def test_latest_frame_newest_wins_and_bad_length_skipped():
  rej = {}
  evs = [event(T0, [frame(A, 0, ems(100))]), event(T0 + 10, [frame(A, 0, ems(200)), frame(A, 0, b"\x00" * 7)])]
  assert latest_frame(evs, A, 0, rej) == (T0 + 10, ems(200))
  assert rej == {"len": 1}


def test_newer_invalid_event_blocks_older_healthy_data():
  rej = {}
  evs = [event(T0, [frame(A, 0, ems(100))]), event(T0 + 10, [frame(A, 0, ems(200))], valid=False)]
  assert latest_frame(evs, A, 0, rej) is None and rej == {"invalid_event": 1}
  evs = [event(T0, [frame(A, 0, ems(100))]), event(T0 + 10, [], valid=False)]
  assert latest_frame(evs, A, 0) is None
  # an older invalid event below a newer healthy match does not matter
  evs = [event(T0, [], valid=False), event(T0 + 10, [frame(A, 0, ems(200))])]
  assert latest_frame(evs, A, 0) == (T0 + 10, ems(200))


def test_ingest_rejects_future_bad_time_old_and_reordered():
  st = RpmState()
  assert not st.ingest(T0 + 1, ems(100), T0)  # any future time
  assert not st.ingest(0, ems(100), T0)
  assert not st.ingest(-5, ems(100), T0)
  assert not st.ingest(T0 - 301 * NS_PER_MS, ems(100), T0)  # backlog older than stale limit
  assert st.frame_counter == 0
  assert st.ingest(T0, ems(100), T0)
  assert not st.ingest(T0, ems(100), T0 + 1)  # duplicate
  assert not st.ingest(T0 - 1, ems(100), T0 + 1)  # out of order
  assert st.rejects == {"future": 1, "bad_time": 2, "too_old": 1, "out_of_order": 2}
  assert st.frame_counter == 1


def test_stale_and_no_frame_are_invalid():
  st = RpmState()
  s = Sender(st, KEY, session_id=7)
  p = P.decode(s.build(T0, True), KEY)
  assert p.flags == P.FLAG_AUTH and p.frame_age_ms == 0xFFFF and p.frame_counter == 0 and p.rpm_raw == 0
  st.ingest(T0, ems(3200), T0)
  assert P.decode(s.build(T0 + 300 * NS_PER_MS, True), KEY).flags & P.FLAG_RPM_VALID
  p = P.decode(s.build(T0 + 301 * NS_PER_MS, True), KEY)
  assert not p.flags & P.FLAG_RPM_VALID and p.flags & P.FLAG_FRAME_SEEN and p.frame_age_ms == 301
  # engine stopped / fault are never valid
  st.ingest(T0 + 400 * NS_PER_MS, ems(0), T0 + 400 * NS_PER_MS)
  assert not P.decode(s.build(T0 + 400 * NS_PER_MS, True), KEY).flags & P.FLAG_RPM_VALID
  st.ingest(T0 + 410 * NS_PER_MS, ems(3200, fault=True), T0 + 410 * NS_PER_MS)
  assert not P.decode(s.build(T0 + 410 * NS_PER_MS, True), KEY).flags & P.FLAG_RPM_VALID
  # allow_valid=False never sets valid
  st.ingest(T0 + 420 * NS_PER_MS, ems(3200), T0 + 420 * NS_PER_MS)
  assert not P.decode(s.build(T0 + 420 * NS_PER_MS, False), KEY).flags & P.FLAG_RPM_VALID


def test_future_age_is_unknown():
  st = RpmState()
  st.ingest(T0, ems(3200), T0)
  assert st.frame_age_ms(T0 - 1) is None and not st.is_fresh_valid(T0 - 1)


def test_heartbeat_keeps_counter_and_rpm_and_rate_limits():
  st = RpmState()
  s = Sender(st, KEY, session_id=9)
  st.ingest(T0, ems(3200), T0)
  a = P.decode(s.maybe_build(T0, True), KEY)
  assert s.maybe_build(T0 + 10 * NS_PER_MS, True) is None
  st.ingest(T0 + 10 * NS_PER_MS, ems(3300), T0 + 10 * NS_PER_MS)
  assert s.maybe_build(T0 + 15 * NS_PER_MS, True) is None  # 20 ms rate limit
  b = P.decode(s.maybe_build(T0 + 20 * NS_PER_MS, True), KEY)
  assert b.frame_counter == a.frame_counter + 1 and b.seq == a.seq + 1
  assert s.maybe_build(T0 + 100 * NS_PER_MS, True) is None
  hb = P.decode(s.maybe_build(T0 + 220 * NS_PER_MS, True), KEY)
  assert (hb.frame_counter, hb.rpm_raw) == (b.frame_counter, b.rpm_raw) and hb.frame_age_ms == 210


def test_counter_wrap_rotates_session():
  st = RpmState()
  s = Sender(st, KEY, session_id=11)
  st.frame_counter = P.U32_MAX
  st.ingest(T0, ems(3200), T0)
  assert st.frame_counter == 1 and st.epoch == 1
  p = P.decode(s.build(T0, True), KEY)
  assert p.session_id != 11 and p.seq == 1 and p.frame_counter == 1


def test_seq_exhaustion_rotates_session():
  st = RpmState()
  s = Sender(st, KEY, session_id=12)
  s.seq = P.U32_MAX
  p = P.decode(s.build(T0, True), KEY)
  assert p.session_id != 12 and p.seq == 1

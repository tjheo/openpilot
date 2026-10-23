"""Freshness/backlog rules (ported from tools/svx_rpm/tests/test_core.py) plus the explicit status of SVXL."""
from openpilot.selfdrive.svx_live import wire as W
from openpilot.selfdrive.svx_live.rpm_state import INVALID, NS_PER_MS, RpmState, latest_frame, live_status, telemetry_fields
from openpilot.selfdrive.svx_live.tests.helpers import ems, event, frame

A = 0x316
T0 = 5_000_000_000


def test_latest_frame_correct_bus_only():
  evs = [event(T0, [frame(A, 0, ems(100)), frame(A, 1, ems(200)), frame(A, 128, ems(300)), frame(0x317, 0, ems(400))])]
  assert latest_frame(evs, A, 0) == (T0, ems(100))
  assert latest_frame(evs, A, 1) == (T0, ems(200))
  assert latest_frame(evs, A, 2) is None
  # TX echo of bus 0 is src 128: never matched by a receive bus
  assert latest_frame([event(T0, [frame(A, 128, ems(1))])], A, 0) is None


def test_latest_frame_newest_wins():
  evs = [event(T0, [frame(A, 0, ems(100))]), event(T0 + 10, [frame(A, 0, ems(150)), frame(A, 0, ems(200))])]
  assert latest_frame(evs, A, 0) == (T0 + 10, ems(200))


def test_newer_bad_length_frame_is_invalid_not_older_good_data():
  rej = {}
  evs = [event(T0, [frame(A, 0, ems(100))]), event(T0 + 10, [frame(A, 0, b"\x00" * 7)])]
  assert latest_frame(evs, A, 0, rej) == (INVALID, "len") and rej == {"len": 1}


def test_newer_invalid_event_is_invalid_not_older_healthy_data():
  rej = {}
  evs = [event(T0, [frame(A, 0, ems(100))]), event(T0 + 10, [frame(A, 0, ems(200))], valid=False)]
  assert latest_frame(evs, A, 0, rej) == (INVALID, "invalid_event") and rej == {"invalid_event": 1}
  evs = [event(T0, [frame(A, 0, ems(100))]), event(T0 + 10, [], valid=False)]
  assert latest_frame(evs, A, 0) == (INVALID, "invalid_event")
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


def test_backlog_never_refreshes_freshness():
  """A long msgq backlog drained later: logMonoTime decides the age, never the dequeue time."""
  st = RpmState()
  assert st.ingest(T0, ems(3200), T0)
  now = T0 + 2_000 * NS_PER_MS
  for i in range(50):  # 50 queued frames from up to 1.5 s ago, all older than 300 ms at dequeue
    assert not st.ingest(T0 + (i + 1) * 10 * NS_PER_MS, ems(3300), now)
  assert st.frame_counter == 1 and live_status(None, st, now) != W.STATUS_OK


def test_every_rejected_newest_frame_silences_immediately():
  for bad in (lambda st: st.ingest(T0 + 50 * NS_PER_MS, ems(3200), T0 + 10 * NS_PER_MS),   # future
              lambda st: st.ingest(T0 - 1, ems(3200), T0 + 10 * NS_PER_MS),                 # out of order
              lambda st: st.ingest(T0 - 400 * NS_PER_MS, ems(3200), T0 + 10 * NS_PER_MS),   # too old
              lambda st: st.ingest(T0 + 5 * NS_PER_MS, b"\x00" * 7, T0 + 10 * NS_PER_MS),    # length
              lambda st: st.invalidate("invalid_event")):
    st = RpmState()
    assert st.ingest(T0, ems(3200), T0)
    assert live_status(None, st, T0 + 10 * NS_PER_MS) == W.STATUS_OK
    bad(st)
    assert live_status(None, st, T0 + 10 * NS_PER_MS) == W.STATUS_INVALID
    assert not st.is_fresh_valid(T0 + 10 * NS_PER_MS)
    f = telemetry_fields(None, st, T0 + 10 * NS_PER_MS)
    assert f["status"] == W.STATUS_INVALID and not f["flags"] & W.FLAG_RPM_VALID
    # the next accepted frame clears it
    assert st.ingest(T0 + 20 * NS_PER_MS, ems(3200), T0 + 20 * NS_PER_MS)
    assert live_status(None, st, T0 + 20 * NS_PER_MS) == W.STATUS_OK


def test_status_mapping_and_valid_flag_only_for_ok():
  st = RpmState()
  assert live_status("car_params_unavailable", st, T0) == W.STATUS_WAIT_CAR
  assert live_status("unsupported_car", st, T0) == W.STATUS_UNSUPPORTED_CAR
  assert live_status("unsupported_canfd", st, T0) == W.STATUS_UNSUPPORTED_CANFD
  assert live_status("anything else", st, T0) == W.STATUS_WAIT_CAR  # unknown fails closed
  assert live_status(None, st, T0) == W.STATUS_NO_FRAME
  f = telemetry_fields(None, st, T0)
  assert f == {"frame_counter": 0, "rpm_raw": 0, "frame_age_ms": 0xFFFF, "status": W.STATUS_NO_FRAME, "flags": 0}
  st.ingest(T0, ems(12800), T0)
  f = telemetry_fields(None, st, T0 + 12 * NS_PER_MS)
  assert f == {"frame_counter": 1, "rpm_raw": 12800, "frame_age_ms": 12, "status": W.STATUS_OK,
               "flags": W.FLAG_RPM_VALID | W.FLAG_FRAME_SEEN}
  assert live_status(None, st, T0 + 300 * NS_PER_MS) == W.STATUS_OK
  assert live_status(None, st, T0 + 301 * NS_PER_MS) == W.STATUS_STALE
  # a good frame is never valid while the car selection is not verified
  f = telemetry_fields("unsupported_car", st, T0 + 12 * NS_PER_MS)
  assert f["status"] == W.STATUS_UNSUPPORTED_CAR and f["flags"] == W.FLAG_FRAME_SEEN
  st.ingest(T0 + 400 * NS_PER_MS, ems(0), T0 + 400 * NS_PER_MS)
  assert live_status(None, st, T0 + 400 * NS_PER_MS) == W.STATUS_ENGINE_OFF
  st.ingest(T0 + 410 * NS_PER_MS, ems(3200, fault=True), T0 + 410 * NS_PER_MS)
  assert live_status(None, st, T0 + 410 * NS_PER_MS) == W.STATUS_INVALID
  st.ingest(T0 + 420 * NS_PER_MS, ems(0xFFFF), T0 + 420 * NS_PER_MS)
  assert live_status(None, st, T0 + 420 * NS_PER_MS) == W.STATUS_INVALID


def test_future_age_is_unknown():
  st = RpmState()
  st.ingest(T0, ems(3200), T0)
  assert st.frame_age_ms(T0 - 1) is None and not st.is_fresh_valid(T0 - 1)
  assert live_status(None, st, T0 - 1) == W.STATUS_STALE


def test_counter_wrap_bumps_epoch():
  st = RpmState()
  st.frame_counter = W.U32_MAX
  st.ingest(T0, ems(3200), T0)
  assert st.frame_counter == 1 and st.epoch == 1

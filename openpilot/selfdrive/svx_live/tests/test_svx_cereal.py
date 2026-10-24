"""REAL cereal/car Cap'n Proto schemas from this tree (ported from tools/svx_rpm/tests/test_cereal_integration.py).
msgq is not built here, so the subscriber socket is a test double returning real serialized Event bytes."""
import pytest

pytest.importorskip("capnp")
cereal = pytest.importorskip("openpilot.cereal")
log, car = cereal.log, cereal.car

from openpilot.selfdrive.svx_live.cereal_source import (KEEP_PER_TICK, MAX_DRAIN_PER_TICK, CerealCanSource, drain_bounded,
                                                        selection_from_car_params_bytes)
from openpilot.selfdrive.svx_live.profile import HYUNDAI_FLAG_CANFD, PROFILES, selection_error
from openpilot.selfdrive.svx_live.rpm_state import INVALID
from openpilot.selfdrive.svx_live.tests.helpers import ems

T0 = 7_000_000_000


def can_bytes(t_ns, frames, valid=True):
  e = log.Event.new_message(logMonoTime=t_ns, valid=valid)
  c = e.init("can", len(frames))
  for i, (addr, src, dat) in enumerate(frames):
    c[i].address, c[i].src, c[i].dat = addr, src, dat
  return e.to_bytes()


def log_from_bytes(dat):  # same as openpilot.cereal.messaging.log_from_bytes
  with log.Event.from_bytes(dat, traversal_limit_in_words=2**64 - 1) as msg:
    return msg


class Sock:
  def __init__(self, msgs):
    self.msgs = list(msgs)

  def receive(self, non_blocking=False):
    return self.msgs.pop(0) if self.msgs else None


def source(msgs, bus=0):
  s = CerealCanSource.__new__(CerealCanSource)  # bypass msgq socket creation only
  s._log_from_bytes, s._sock, s.address, s.bus, s.rejects, s.drain_caps = log_from_bytes, Sock(msgs), 0x316, bus, {}, 0
  return s


def test_real_can_events_decode_newest_on_bus():
  s = source([can_bytes(T0, [(0x316, 0, ems(3200))]),
              can_bytes(T0 + 10_000_000, [(0x316, 128, ems(9)), (0x316, 0, ems(3300)), (0x2B0, 0, b"\x00" * 8)])])
  assert s.poll() == (T0 + 10_000_000, ems(3300))
  assert s.poll() is None  # queue consumed


def test_real_invalid_event_is_explicit_invalid():
  s = source([can_bytes(T0, [(0x316, 0, ems(3200))]), can_bytes(T0 + 10_000_000, [(0x316, 0, ems(3300))], valid=False)])
  assert s.poll() == (INVALID, "invalid_event") and s.rejects == {"invalid_event": 1}


def test_wrong_bus_then_garbage_newest_is_invalid():
  s = source([can_bytes(T0, [(0x316, 1, ems(3200))]), b"\x01\x02garbage"])
  assert s.poll() == (INVALID, "invalid_event") and s.rejects.get("parse") == 1
  s = source([b"\x01\x02garbage", can_bytes(T0, [(0x316, 0, ems(3200))])])  # older garbage, newer good
  assert s.poll() == (T0, ems(3200))


def test_real_short_ems11_newest_is_invalid():
  s = source([can_bytes(T0, [(0x316, 0, ems(3200))]), can_bytes(T0 + 10_000_000, [(0x316, 0, b"\x00" * 6)])])
  assert s.poll() == (INVALID, "len")


def test_drain_is_bounded_and_keeps_newest():
  class Endless:
    n = 0

    def receive(self, non_blocking=False):
      self.n += 1
      return self.n
  kept, n = drain_bounded(Endless())
  assert n == MAX_DRAIN_PER_TICK and kept == list(range(MAX_DRAIN_PER_TICK - KEEP_PER_TICK + 1, MAX_DRAIN_PER_TICK + 1))


def car_params(brand, fp, flags=0):
  cp = car.CarParams.new_message(brand=brand, carFingerprint=fp, flags=flags)
  return cp.to_bytes()


def test_real_car_params_selection():
  prof = PROFILES["kia_seltos_2021_ems11"]
  ok = selection_from_car_params_bytes(car_params("hyundai", "KIA_SELTOS"), car)
  assert selection_error(prof, ok) is None
  assert selection_error(prof, selection_from_car_params_bytes(car_params("hyundai", "KIA_SELTOS", HYUNDAI_FLAG_CANFD), car)) == "unsupported_canfd"
  assert selection_error(prof, selection_from_car_params_bytes(car_params("hyundai", "HYUNDAI_SONATA"), car)) == "unsupported_car"
  assert selection_error(prof, selection_from_car_params_bytes(car_params("toyota", "KIA_SELTOS"), car)) == "unsupported_car"
  assert selection_from_car_params_bytes(None, car) is None
  assert selection_from_car_params_bytes(b"", car) is None
  assert selection_from_car_params_bytes(b"\xff" * 5, car) is None
  assert selection_error(prof, None) == "car_params_unavailable"


def test_profile_constants_match_opendbc():
  try:
    from opendbc.car.hyundai.values import CAR, HyundaiFlags, HyundaiPlatformConfig
  except Exception as e:  # opendbc needs Python >= 3.10 and numpy
    pytest.skip(f"opendbc not importable here: {type(e).__name__}")
  assert HYUNDAI_FLAG_CANFD == int(HyundaiFlags.CANFD)
  assert isinstance(CAR.KIA_SELTOS.config, HyundaiPlatformConfig)  # non-CAN-FD platform config
  assert not CAR.KIA_SELTOS.config.flags & HyundaiFlags.CANFD
  assert str(CAR.KIA_SELTOS) == PROFILES["kia_seltos_2021_ems11"].fingerprint
  assert PROFILES["kia_seltos_2021_ems11"].allowed_buses == frozenset({0})
  assert PROFILES["kia_seltos_2021_ems11"].fallback_address == 0x366


def e366(raw):
  return bytes([0x10, raw & 0xFF, raw >> 8, 0x10, 0, 0, 0])  # 7 bytes, as on a Seltos without EMS11


def fallback_source(msgs, bus=0):
  s = source(msgs, bus)
  s.fallback, s.primary_seen = 0x366, False
  return s


def test_real_ems366_fallback_without_ems11():
  s = fallback_source([can_bytes(T0, [(0x366, 0, e366(2800))]),
                       can_bytes(T0 + 10_000_000, [(0x366, 128, e366(9)), (0x366, 0, e366(2804)), (0x366, 2, e366(1))])])
  f = s.poll()
  assert f == (T0 + 10_000_000, e366(2804)) and f.address == 0x366 and not s.primary_seen
  assert source([can_bytes(T0, [(0x366, 0, e366(2800))])]).poll() is None  # no fallback configured: unchanged


def test_real_ems11_wins_and_latches_off_the_fallback():
  s = fallback_source([can_bytes(T0, [(0x316, 0, ems(3200))]), can_bytes(T0 + 10_000_000, [(0x366, 0, e366(2800))])])
  f = s.poll()
  assert f == (T0, ems(3200)) and f.address == 0x316 and s.primary_seen  # older EMS11 beats newer EMS_366
  s._sock = Sock([can_bytes(T0 + 20_000_000, [(0x366, 0, e366(2800))])])
  assert s.poll() is None  # once EMS11 was seen, EMS_366 is never used again by this source


def test_real_wrong_length_ems11_also_latches_and_invalid_event_still_wins():
  s = fallback_source([can_bytes(T0, [(0x366, 0, e366(2800)), (0x316, 0, b"\x00" * 6)])])
  assert s.poll() == (INVALID, "len") and s.primary_seen
  s = fallback_source([can_bytes(T0, [(0x366, 0, e366(2800))]), can_bytes(T0 + 10_000_000, [(0x2B0, 0, b"\x00" * 6)], valid=False)])
  assert s.poll() == (INVALID, "invalid_event") and not s.primary_seen
  s = fallback_source([can_bytes(T0, [(0x366, 0, e366(2800)[:6])])])
  assert s.poll() == (INVALID, "len")

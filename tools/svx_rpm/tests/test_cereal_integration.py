"""Uses the REAL cereal/car Cap'n Proto schemas from this tree (pycapnp). msgq is not built here, so
the socket itself is a test double that returns real serialized Event bytes."""
import pytest

pytest.importorskip("capnp")
cereal = pytest.importorskip("openpilot.cereal")
log, car = cereal.log, cereal.car

from svxrpm.cereal_source import CerealCanSource, selection_from_car_params_bytes
from svxrpm.profile import HYUNDAI_FLAG_CANFD, PROFILES, selection_error
from helpers import ems

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


def test_real_invalid_event_rejected():
  s = source([can_bytes(T0, [(0x316, 0, ems(3200))]), can_bytes(T0 + 10_000_000, [(0x316, 0, ems(3300))], valid=False)])
  assert s.poll() is None and s.rejects == {"invalid_event": 1}


def test_wrong_bus_and_garbage_bytes():
  s = source([can_bytes(T0, [(0x316, 1, ems(3200))]), b"\x01\x02garbage"])
  assert s.poll() is None and s.rejects.get("parse") == 1


def car_params(brand, fp, flags=0):
  cp = car.CarParams.new_message(brand=brand, carFingerprint=fp, flags=flags)
  return cp.to_bytes()


def test_real_car_params_selection():
  prof = PROFILES["kia_seltos_2021_ems11"]
  ok = selection_from_car_params_bytes(car_params("hyundai", "KIA_SELTOS"), car)
  assert selection_error(prof, ok) is None
  assert selection_error(prof, selection_from_car_params_bytes(car_params("hyundai", "KIA_SELTOS", HYUNDAI_FLAG_CANFD), car)) == "unsupported_canfd"
  assert selection_error(prof, selection_from_car_params_bytes(car_params("hyundai", "HYUNDAI_SONATA"), car)) == "unsupported_car"
  assert selection_from_car_params_bytes(None, car) is None
  assert selection_from_car_params_bytes(b"", car) is None
  assert selection_from_car_params_bytes(b"\xff" * 5, car) is None


def test_profile_constants_match_opendbc():
  try:
    from opendbc.car.hyundai.values import CAR, HyundaiFlags, HyundaiPlatformConfig
  except Exception as e:  # opendbc needs Python >= 3.10 and numpy
    pytest.skip(f"opendbc not importable here: {type(e).__name__}")
  assert HYUNDAI_FLAG_CANFD == int(HyundaiFlags.CANFD)
  assert isinstance(CAR.KIA_SELTOS.config, HyundaiPlatformConfig)  # non-CAN-FD platform config
  assert not CAR.KIA_SELTOS.config.flags & HyundaiFlags.CANFD
  assert str(CAR.KIA_SELTOS) == PROFILES["kia_seltos_2021_ems11"].fingerprint

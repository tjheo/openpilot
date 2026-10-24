"""EMS_366 (0x366) fallback decoder: DBC layout, decoding and per-address selection in rpm_state. Synthetic frames only."""
import os
import re

import pytest

from openpilot.selfdrive.svx_live.ems11 import EMS11_ADDR, RPM_SCALE
from openpilot.selfdrive.svx_live.ems366 import EMS366_ADDR, EMS366_LENS, decode_ems366
from openpilot.selfdrive.svx_live.rpm_state import DECODERS, INVALID, Frame, RpmState, latest_frame
from openpilot.selfdrive.svx_live.tests.helpers import ems, event, frame

HERE = os.path.dirname(__file__)
DBC = os.path.join(HERE, "..", "..", "..", "..", "opendbc_repo", "opendbc", "dbc", "hyundai_kia_generic.dbc")
T0 = 5_000_000_000


def e366(raw, n=7, fill=0):
  """N in bytes 1-2 (LE); every other byte set to `fill`."""
  return bytes([fill, raw & 0xFF, raw >> 8] + [fill] * (n - 3))


@pytest.mark.parametrize("dat,raw,rpm,reason", [
  (e366(2800), 2800, 700.0, "ok"),                             # idle
  (bytes([0, 0x34, 0x12, 0, 0, 0, 0]), 0x1234, 1165.0, "ok"),   # little-endian, not big-endian
  (e366(16000, fill=0xFF), 16000, 4000.0, "ok"),                # other bytes ignored
  (e366(26000, n=8), 26000, 6500.0, "ok"),                      # the DBC's 8-byte length
  (e366(0), 0, 0.0, "engine_stopped"),
  (e366(0xFFFF), 0xFFFF, 16383.75, "sna"),
  (e366(40000), 40000, 10000.0, "implausible"),
])
def test_decode(dat, raw, rpm, reason):
  s = decode_ems366(dat)
  assert (s.rpm_raw, s.rpm, s.reason, s.fault) == (raw, rpm, reason, False)
  assert s.valid == (reason == "ok")


def test_rejects_wrong_length():
  for n in (0, 2, 3, 6, 9, 64):
    with pytest.raises(ValueError):
      decode_ems366(bytes(n))


def test_layout_matches_current_dbc():
  with open(DBC, encoding="latin-1") as f:
    txt = f.read()
  m = re.search(r"^BO_ (\d+) EMS_366: (\d+) EMS\n((?: SG_ .*\n)+)", txt, re.M)
  assert m and int(m.group(1)) == EMS366_ADDR and int(m.group(2)) in EMS366_LENS
  sigs = m.group(3)
  assert re.search(r"^ SG_ N : 8\|16@1\+ \(0\.25,0(\.0)?\) ", sigs, re.M)
  ends = [int(a) + int(b) for a, b in re.findall(r"^ SG_ \w+ : (\d+)\|(\d+)@1", sigs, re.M)]
  assert max(ends) <= 7 * 8  # every little-endian signal fits in the 7 bytes seen on the car
  assert RPM_SCALE == 0.25 and txt.count("BO_ 870 ") == 1
  # the cross-check message used to validate the layout offline is still defined as engine speed for the cluster
  assert re.search(r"^BO_ 354 TCU_DCT13: \d+ TCU\n(?: SG_ .*\n)*? SG_ Cluster_Engine_RPM : 10\|13@1\+ \(0\.9766,", txt, re.M)


def test_decoders_are_chosen_by_address_not_length():
  assert DECODERS[EMS11_ADDR][0] == frozenset({8}) and DECODERS[EMS366_ADDR][0] == EMS366_LENS
  dat = e366(2800, n=8, fill=0x10)  # an 8-byte EMS_366 frame is NOT read with the EMS11 layout
  st = RpmState()
  assert st.ingest(T0, dat, T0, EMS366_ADDR) and st.last_sample.rpm == 700.0
  st = RpmState()
  assert st.ingest(T0, dat, T0) and st.last_sample.rpm_raw == 0x100A  # default stays EMS11 (bytes 2-3)


def test_ingest_lengths_and_unknown_address():
  st = RpmState()
  assert not st.ingest(T0, e366(2800), T0) and st.invalid_reason == "len"  # 7 bytes is never EMS11
  st = RpmState()
  assert not st.ingest(T0, e366(2800, n=6), T0, EMS366_ADDR) and st.invalid_reason == "len"
  st = RpmState()
  assert not st.ingest(T0, e366(2800), T0, 0x162) and st.invalid_reason == "address"
  st = RpmState()
  assert st.ingest(T0, e366(2800), T0, EMS366_ADDR) and st.frame_counter == 1 and st.is_fresh_valid(T0)


def test_latest_frame_ems366_bus_lengths_and_address():
  A = EMS366_ADDR
  f = latest_frame([event(T0, [frame(A, 0, e366(2800)), frame(A, 128, e366(1)), frame(A, 2, e366(2))])], A, 0)
  assert f == (T0, e366(2800)) and isinstance(f, Frame) and f.address == A
  assert latest_frame([event(T0, [frame(A, 0, e366(2800, n=8))])], A, 0).address == A
  assert latest_frame([event(T0, [frame(A, 0, e366(2800, n=6))])], A, 0) == (INVALID, "len")
  assert latest_frame([event(T0, [frame(A, 128, e366(2800))])], A, 0) is None  # TX echo never matches
  assert latest_frame([event(T0, [frame(EMS11_ADDR, 0, ems(3200))])], EMS11_ADDR, 0).address == EMS11_ADDR

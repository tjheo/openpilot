"""Ported unchanged from the retired tools/svx_rpm/tests/test_ems11.py (decoder + current DBC layout)."""
import json
import os
import re

import pytest

from openpilot.selfdrive.svx_live.ems11 import EMS11_ADDR, RPM_SCALE, decode_ems11

HERE = os.path.dirname(__file__)
DBC = os.path.join(HERE, "..", "..", "..", "..", "opendbc_repo", "opendbc", "dbc", "hyundai_kia_generic.dbc")


def frames():
  with open(os.path.join(HERE, "fixtures", "ems11_frames.json")) as f:
    return json.load(f)["frames"]


@pytest.mark.parametrize("f", frames(), ids=lambda f: f["name"])
def test_fixture_decode(f):
  s = decode_ems11(bytes.fromhex(f["dat"]))
  assert (s.rpm_raw, s.rpm, s.reason) == (f["raw"], f["rpm"], f["reason"])
  assert s.valid == (f["reason"] == "ok")


def test_sample_idle_frame_little_endian():
  s = decode_ems11(bytes([0, 0, 0x80, 0x0C, 0, 0, 0, 0]))
  assert s.rpm_raw == 0x0C80 and s.rpm == 800.0 and s.valid


def test_rejects_wrong_length():
  for n in (0, 7, 9, 64):
    with pytest.raises(ValueError):
      decode_ems11(bytes(n))


def test_layout_matches_current_dbc():
  assert os.path.exists(DBC), "opendbc DBC must be in this tree"
  with open(DBC, encoding="latin-1") as f:
    txt = f.read()
  m = re.search(r"^BO_ (\d+) EMS11: (\d+) EMS\n((?: SG_ .*\n)+)", txt, re.M)
  assert m and int(m.group(1)) == EMS11_ADDR and int(m.group(2)) == 8
  sigs = m.group(3)
  assert re.search(r"^ SG_ N : 16\|16@1\+ \(0\.25,0(\.0)?\) ", sigs, re.M)
  assert re.search(r"^ SG_ F_N_ENG : 1\|1@1\+ ", sigs, re.M)
  assert RPM_SCALE == 0.25
  assert txt.count("BO_ 790 ") == 1

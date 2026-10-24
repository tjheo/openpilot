"""EMS_366 (0x366) engine-speed decoder, the fallback for cars whose harness bus carries no EMS11.

Checked against opendbc_repo/opendbc/dbc/hyundai_kia_generic.dbc in this tree (same definition in upstream
commaai/opendbc generator/hyundai/hyundai_can.dbc; layout from commaai/opendbc PR #319):
  BO_ 870 EMS_366: 8 EMS
   SG_ N : 8|16@1+ (0.25,0.0) [0.0|16383.75] "rpm"

`@1` is Intel/little-endian, start bit 8, length 16: raw = dat[1] | dat[2] << 8, rpm = raw * 0.25 (the same
raw unit as EMS11 N, so the wire `rpm_raw` meaning is unchanged). Every DBC signal of this message ends by bit 48,
so the 7-byte frames seen on a Seltos and the DBC's 8 bytes are both accepted. EMS_366 defines no fault flag.
A zero raw value is reported as engine stopped; that state has not been observed in a recording.
Nothing here sends anything.
"""
from .ems11 import MAX_PLAUSIBLE_RPM, RAW_SNA, RPM_SCALE, Ems11Sample

EMS366_ADDR = 0x366  # 870
EMS366_LENS = frozenset({7, 8})


def decode_ems366(dat: bytes) -> Ems11Sample:
  if len(dat) not in EMS366_LENS:
    raise ValueError("EMS_366 must be 7 or 8 bytes")
  raw = dat[1] | (dat[2] << 8)
  rpm = raw * RPM_SCALE
  if raw == RAW_SNA:
    reason = "sna"
  elif raw == 0:
    reason = "engine_stopped"
  elif rpm > MAX_PLAUSIBLE_RPM:
    reason = "implausible"
  else:
    reason = "ok"
  return Ems11Sample(rpm_raw=raw, rpm=rpm, fault=False, valid=(reason == "ok"), reason=reason)

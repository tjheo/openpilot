"""EMS11 (0x316) engine-speed decoder.

Checked against opendbc_repo/opendbc/dbc/hyundai_kia_generic.dbc in this tree:
  BO_ 790 EMS11: 8 EMS
   SG_ F_N_ENG : 1|1@1+ (1.0,0.0) [0.0|1.0] ""
   SG_ N : 16|16@1+ (0.25,0.0) [0.0|16383.75] "rpm"

`@1` is Intel/little-endian, start bit 16, length 16: raw = dat[2] | dat[3] << 8, rpm = raw * 0.25.
F_N_ENG (byte 0 bit 1) is treated conservatively as an engine-speed fault flag; if it is set,
the sample is invalid. Whether EMS11 is actually present on the comma harness bus must be
checked on the real car (the app shows "no EMS11 frame" if it is absent). Nothing here sends anything.
"""
from dataclasses import dataclass

EMS11_ADDR = 0x316  # 790
EMS11_LEN = 8
RPM_SCALE = 0.25
RAW_SNA = 0xFFFF            # all-ones treated as "signal not available"
MAX_PLAUSIBLE_RPM = 9000.0


@dataclass(frozen=True)
class Ems11Sample:
  rpm_raw: int
  rpm: float
  fault: bool
  valid: bool
  reason: str  # "ok", "engine_stopped", "fault", "sna", "implausible"


def decode_ems11(dat: bytes) -> Ems11Sample:
  if len(dat) != EMS11_LEN:
    raise ValueError("EMS11 must be 8 bytes")
  raw = dat[2] | (dat[3] << 8)
  rpm = raw * RPM_SCALE
  fault = bool((dat[0] >> 1) & 0x1)
  if raw == RAW_SNA:
    reason = "sna"
  elif fault:
    reason = "fault"
  elif raw == 0:
    reason = "engine_stopped"
  elif rpm > MAX_PLAUSIBLE_RPM:
    reason = "implausible"
  else:
    reason = "ok"
  return Ems11Sample(rpm_raw=raw, rpm=rpm, fault=fault, valid=(reason == "ok"), reason=reason)

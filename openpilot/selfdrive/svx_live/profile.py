"""Explicit vehicle profiles. Only profiles listed here are accepted; anything else fails closed.

Why raw EMS11 instead of carState.engineRpm: carState has an engineRpm field, but the Hyundai/Kia
carstate in this tree never sets it, so it would always read 0.

kia_seltos_2021_ems11:
  * opendbc platform KIA_SELTOS (HyundaiPlatformConfig, dbc hyundai_kia_generic): a non-CAN-FD
    platform whose powertrain parser is on bus 0 (CarState.get_can_parsers, `Bus.pt` -> 0).
  * The CarParams written by card must report brand "hyundai", carFingerprint "KIA_SELTOS" and
    no HyundaiFlags.CANFD bit. Any other selection, including CAN-FD variants, is rejected.
  * Only bus 0 is accepted. TX echoes (src >= 128) never match. That EMS11 is really present on
    this bus for a given car/year/engine is NOT known from the DBC; the app then shows STATUS_NO_FRAME.
"""
from __future__ import annotations

from dataclasses import dataclass

HYUNDAI_FLAG_CANFD = 2 ** 13  # opendbc HyundaiFlags.CANFD


@dataclass(frozen=True)
class Profile:
  name: str
  brand: str
  fingerprint: str
  address: int
  allowed_buses: frozenset


PROFILES = {
  "kia_seltos_2021_ems11": Profile("kia_seltos_2021_ems11", "hyundai", "KIA_SELTOS", 0x316, frozenset({0})),
}


@dataclass(frozen=True)
class CarSelection:
  brand: str
  fingerprint: str
  flags: int


def selection_error(profile: Profile, sel: CarSelection | None) -> str | None:
  """None if the current car selection matches the profile, else a short reason."""
  if sel is None:
    return "car_params_unavailable"
  if sel.brand != profile.brand or sel.fingerprint != profile.fingerprint:
    return "unsupported_car"
  if sel.flags & HYUNDAI_FLAG_CANFD:
    return "unsupported_canfd"
  return None

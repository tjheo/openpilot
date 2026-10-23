"""Read-only adapters to the running openpilot stack. openpilot modules are imported only inside the constructors.

* CerealCanSource: a plain msgq SUBSCRIBER to the existing `can` service. It never creates a
  publisher, never opens Panda/USB/SPI/socketcan, and never touches sendcan.
* ParamsCarSelection: reads the existing `CarParams` param (written by card) without writing.
"""
from __future__ import annotations

from collections import deque

from .rpm_state import INVALID, latest_frame
from .profile import CarSelection

MAX_DRAIN_PER_TICK = 512  # hard cap on messages dequeued per tick (msgq itself is a bounded ring)
KEEP_PER_TICK = 8         # only the newest few raw messages are kept/parsed per tick


def selection_from_car_params_bytes(raw: bytes | None, car_module) -> CarSelection | None:
  if not raw:
    return None
  try:
    with car_module.CarParams.from_bytes(raw) as cp:
      return CarSelection(brand=str(cp.brand), fingerprint=str(cp.carFingerprint), flags=int(cp.flags))
  except Exception:
    return None


def drain_bounded(sock, max_drain: int = MAX_DRAIN_PER_TICK, keep: int = KEEP_PER_TICK):
  """Non-blocking dequeue of at most `max_drain` messages; returns (newest `keep` raw msgs, count)."""
  kept = deque(maxlen=keep)
  n = 0
  while n < max_drain:
    m = sock.receive(non_blocking=True)
    if m is None:
      break
    kept.append(m)
    n += 1
  return list(kept), n


class _Unparseable:
  valid = False  # treated like an invalid event by latest_frame()
  can = ()
  logMonoTime = 0


_UNPARSEABLE = _Unparseable()


class CerealCanSource:
  def __init__(self, address: int, bus: int):
    from openpilot.cereal import messaging
    self._log_from_bytes = messaging.log_from_bytes
    self._sock = messaging.sub_sock("can", conflate=False)
    self.address = address
    self.bus = bus
    self.rejects: dict = {}
    self.drain_caps = 0

  def _drain(self):
    if self._sock is None:
      return []
    raw, n = drain_bounded(self._sock)
    if n >= MAX_DRAIN_PER_TICK:
      self.drain_caps += 1
    return raw

  def poll(self):
    """latest_frame() result for the queued messages: (logMonoTime_ns, dat), (INVALID, reason) or None.
    A message that cannot be parsed is explicit bad evidence when it is the newest one."""
    raw = self._drain()
    events = []
    for m in raw:
      try:
        events.append(self._log_from_bytes(m))
      except Exception:
        self.rejects["parse"] = self.rejects.get("parse", 0) + 1
        events.append(_UNPARSEABLE)
    try:
      return latest_frame(events, self.address, self.bus, self.rejects)
    except Exception:
      self.rejects["parse"] = self.rejects.get("parse", 0) + 1
      return INVALID, "parse"

  def discard(self) -> None:
    self._drain()

  def close(self) -> None:
    self._sock = None


class ParamsCarSelection:
  def __init__(self):
    from openpilot.cereal import car
    from openpilot.common.params import Params
    self._car = car
    self._params = Params()

  def read(self) -> CarSelection | None:
    try:
      raw = self._params.get("CarParams")
    except Exception:
      return None
    return selection_from_car_params_bytes(raw, self._car)

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
  """`address` is the primary RPM message (EMS11). `fallback` (EMS_366 or None) is used only while the primary
  has never been seen by this source: once a primary frame (or a wrong-length primary) appears, the source
  uses the primary alone, exactly as without a fallback. A new source is created after every data reset."""
  stats = None  # optional diagnostics.SourceStats; None -> exactly the plain RPM path
  fallback = None
  primary_seen = False

  def __init__(self, address: int, bus: int, stats=None, fallback: int | None = None):
    from openpilot.cereal import messaging
    self._log_from_bytes = messaging.log_from_bytes
    self._sock = messaging.sub_sock("can", conflate=False)
    self.address = address
    self.bus = bus
    self.rejects: dict = {}
    self.drain_caps = 0
    self.stats = stats
    self.fallback = fallback
    self.primary_seen = False

  def _stat(self, name: str, *args) -> None:
    """Diagnostics hook; a broken diagnostics object never changes what poll() returns or raises."""
    if self.stats is not None:
      try:
        getattr(self.stats, name)(*args)
      except Exception:
        pass

  def _drain(self):
    if self._sock is None:
      return []
    try:
      raw, n = drain_bounded(self._sock)
    except Exception as e:
      self._stat("on_read_error", e)
      raise  # unchanged: the daemon reports source_error as INVALID
    if n >= MAX_DRAIN_PER_TICK:
      self.drain_caps += 1
    self._stat("on_drain", n, len(raw), n >= MAX_DRAIN_PER_TICK)
    return raw

  def poll(self):
    """latest_frame() result for the queued messages: (logMonoTime_ns, dat), (INVALID, reason) or None.
    A message that cannot be parsed is explicit bad evidence when it is the newest one. A frame result
    is a rpm_state.Frame whose .address tells which message it came from."""
    raw = self._drain()
    events = []
    for m in raw:
      try:
        events.append(self._log_from_bytes(m))
      except Exception:
        self.rejects["parse"] = self.rejects.get("parse", 0) + 1
        self._stat("on_parse_error")
        events.append(_UNPARSEABLE)
    if self.stats is not None:
      try:
        self.stats.census(events, self.address, skip=_UNPARSEABLE)  # count only, never feeds acceptance
      except Exception:
        self._stat("on_census_error")
    try:
      result = latest_frame(events, self.address, self.bus, self.rejects)
      if result is not None and (result[0] != INVALID or result[1] == "len"):
        self.primary_seen = True
      elif result is None and self.fallback is not None and not self.primary_seen:
        result = latest_frame(events, self.fallback, self.bus, self.rejects)
    except Exception:
      self.rejects["parse"] = self.rejects.get("parse", 0) + 1
      self._stat("on_scan_error")
      return INVALID, "parse"
    self._stat("on_result", result)
    return result

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

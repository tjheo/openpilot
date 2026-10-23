"""Pure, hardware-free logic: EMS11 extraction from `can` events, freshness and the reported status.

Freshness invariant: `frame_counter` and the frame time advance ONLY in RpmState.ingest() for an
accepted EMS11 frame. Building/sending a telemetry frame (heartbeat or not) never makes data fresher.
All times are CLOCK_BOOTTIME nanoseconds, the clock pandad stamps `can` logMonoTime with
(MessageBuilder::initEvent -> nanos_since_boot()).
"""
from __future__ import annotations

from . import wire as W
from .ems11 import EMS11_LEN, Ems11Sample, decode_ems11

NS_PER_MS = 1_000_000
DEFAULT_STALE_MS = 300  # app LiveTracker staleMs

SELECTION_STATUS = {
  "car_params_unavailable": W.STATUS_WAIT_CAR,
  "unsupported_car": W.STATUS_UNSUPPORTED_CAR,
  "unsupported_canfd": W.STATUS_UNSUPPORTED_CANFD,
}


INVALID = "invalid"  # latest_frame() result kind: the newest evidence is explicitly bad


def latest_frame(events, address: int, bus: int, rejects: dict | None = None):
  """Newest evidence for `address` received on exactly `bus` from `events` (oldest -> newest order).

  Events are cereal Event readers (or test doubles) with .valid, .logMonoTime and .can[] of
  (.address, .src, .dat). Scans newest-first and stops at the first event that is invalid or contains
  a match, so normally only one event is walked per tick. TX echoes (src >= 128) never equal a receive
  bus. Returns:
    (logMonoTime_ns, dat_bytes)  the newest frame of the newest matching event
    (INVALID, reason)            a NEWER event is marked invalid (pandad communications health), or the
                                 newest match has a wrong length: older healthy data is never used instead
    None                         nothing relevant queued
  """
  def bad(reason):
    if rejects is not None:
      rejects[reason] = rejects.get(reason, 0) + 1
    return INVALID, reason

  for evt in reversed(events):
    if not evt.valid:
      return bad("invalid_event")
    found = None
    for c in evt.can:
      if c.address == address and c.src == bus:
        found = bytes(c.dat)  # the last match in an event is its newest
    if found is not None:
      if len(found) != EMS11_LEN:
        return bad("len")
      return int(evt.logMonoTime), found
  return None


class RpmState:
  def __init__(self, stale_ms: int = DEFAULT_STALE_MS):
    self.stale_ms = stale_ms
    self.frame_counter = 0
    self.last_sample: Ems11Sample | None = None
    self.last_frame_ns: int | None = None
    self.epoch = 0  # +1 whenever frame_counter would wrap; the server then ends every session
    self.rejects: dict = {}
    # set when the newest evidence was rejected/invalid; cleared only by a newer accepted frame
    self.invalid_reason: str | None = None

  def _reject(self, reason: str) -> bool:
    self.rejects[reason] = self.rejects.get(reason, 0) + 1
    self.invalid_reason = reason
    return False

  def invalidate(self, reason: str) -> None:
    """Explicitly bad newest evidence: report INVALID now instead of letting the last good sample age out."""
    self._reject(reason)

  def ingest(self, frame_ns: int, dat: bytes, now_ns: int) -> bool:
    """`frame_ns` is the event's logMonoTime (pandad receive time), never the dequeue time.
    `now_ns` must be sampled AFTER dequeuing, so a genuine frame can never lie in the future."""
    if len(dat) != EMS11_LEN:
      return self._reject("len")
    if frame_ns <= 0:
      return self._reject("bad_time")
    if frame_ns > now_ns:
      return self._reject("future")
    if now_ns - frame_ns > self.stale_ms * NS_PER_MS:
      return self._reject("too_old")  # backlog: never advance the counter with old data
    if self.last_frame_ns is not None and frame_ns <= self.last_frame_ns:
      return self._reject("out_of_order")
    self.last_sample = decode_ems11(dat)
    self.last_frame_ns = frame_ns
    self.invalid_reason = None
    if self.frame_counter >= W.U32_MAX:
      self.frame_counter = 0
      self.epoch += 1
    self.frame_counter += 1
    return True

  def frame_age_ms(self, now_ns: int) -> int | None:
    """None if no frame yet or the frame time is in the future (unknown age is never fresh)."""
    if self.last_frame_ns is None:
      return None
    age_ns = now_ns - self.last_frame_ns
    if age_ns < 0:
      return None
    return age_ns // NS_PER_MS

  def is_fresh_valid(self, now_ns: int) -> bool:
    age = self.frame_age_ms(now_ns)
    return (age is not None and age <= self.stale_ms and self.invalid_reason is None and
            self.last_sample is not None and self.last_sample.valid)


def live_status(sel_error: str | None, st: RpmState, now_ns: int) -> int:
  """Status reported in every telemetry frame. Only STATUS_OK may carry FLAG_RPM_VALID.
  Unknown selection errors fail closed as WAIT_CAR."""
  if sel_error is not None:
    return SELECTION_STATUS.get(sel_error, W.STATUS_WAIT_CAR)
  if st.invalid_reason is not None:
    return W.STATUS_INVALID
  if st.last_sample is None:
    return W.STATUS_NO_FRAME
  age = st.frame_age_ms(now_ns)
  if age is None or age > st.stale_ms:
    return W.STATUS_STALE
  if st.last_sample.reason == "engine_stopped":
    return W.STATUS_ENGINE_OFF
  if not st.last_sample.valid:
    return W.STATUS_INVALID
  return W.STATUS_OK


def telemetry_fields(sel_error: str | None, st: RpmState, now_ns: int) -> dict:
  """Session-independent part of a telemetry frame (session_id and seq are added per connection)."""
  status = live_status(sel_error, st, now_ns)
  flags = 0
  if st.last_sample is not None:
    flags |= W.FLAG_FRAME_SEEN
  if status == W.STATUS_OK:
    flags |= W.FLAG_RPM_VALID
  age = st.frame_age_ms(now_ns)
  return {
    "frame_counter": st.frame_counter,
    "rpm_raw": st.last_sample.rpm_raw if st.last_sample else 0,
    "frame_age_ms": W.U16_MAX if age is None else min(age, W.U16_MAX),
    "status": status,
    "flags": flags,
  }

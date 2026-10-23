"""Pure, hardware-free logic: EMS11 extraction from `can` events, freshness, datagram scheduling.

Freshness invariant: `frame_counter` and the frame time advance ONLY in RpmState.ingest() for an
accepted EMS11 frame. Building/sending a datagram (heartbeat or not) never makes data fresher.
All times are CLOCK_BOOTTIME nanoseconds, the clock pandad stamps `can` logMonoTime with
(MessageBuilder::initEvent -> nanos_since_boot()).
"""
from __future__ import annotations

import os

from . import protocol as P
from .ems11 import EMS11_LEN, Ems11Sample, decode_ems11

NS_PER_MS = 1_000_000
DEFAULT_STALE_MS = 300  # app TrackerConfig.staleMs


def latest_frame(events, address: int, bus: int, rejects: dict | None = None):
  """Newest frame with `address` received on exactly `bus` from `events` (oldest -> newest order).

  Events are cereal Event readers (or test doubles) with .valid, .logMonoTime and .can[] of
  (.address, .src, .dat). Scans newest-first and stops at the first event that contains a match,
  so normally only one event is walked per tick. If a NEWER event is marked invalid (pandad
  communications health), nothing is returned: older healthy data is never promoted as fresh.
  TX echoes (src >= 128) never equal a receive bus. Returns (logMonoTime_ns, dat_bytes) or None."""
  for evt in reversed(events):
    if not evt.valid:
      if rejects is not None:
        rejects["invalid_event"] = rejects.get("invalid_event", 0) + 1
      return None
    found = None
    for c in evt.can:
      if c.address != address or c.src != bus:
        continue
      dat = bytes(c.dat)
      if len(dat) != EMS11_LEN:
        if rejects is not None:
          rejects["len"] = rejects.get("len", 0) + 1
        continue
      found = dat
    if found is not None:
      return int(evt.logMonoTime), found
  return None


class RpmState:
  def __init__(self, stale_ms: int = DEFAULT_STALE_MS):
    self.stale_ms = stale_ms
    self.frame_counter = 0
    self.last_sample: Ems11Sample | None = None
    self.last_frame_ns: int | None = None
    self.epoch = 0  # +1 whenever frame_counter would wrap; the Sender then starts a new session
    self.rejects: dict = {}

  def _reject(self, reason: str) -> bool:
    self.rejects[reason] = self.rejects.get(reason, 0) + 1
    return False

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
    if self.frame_counter >= P.U32_MAX:
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
    return age is not None and age <= self.stale_ms and self.last_sample is not None and self.last_sample.valid


def new_session_id() -> int:
  while True:
    sid = int.from_bytes(os.urandom(4), "big")
    if sid != 0:
      return sid


class Sender:
  """Data datagrams follow new frames (rate limited); heartbeats carry the *old* frame_counter
  and rpm_raw with a growing age. Always authenticated: there is no unauthenticated mode."""

  MIN_KEY_LEN = 32

  def __init__(self, state: RpmState, key: bytes, min_interval_ms: int = 20, heartbeat_ms: int = 200,
               session_id: int | None = None):
    if not isinstance(key, bytes) or len(key) < self.MIN_KEY_LEN:
      raise ValueError("an authentication key of at least 32 bytes is required")
    self.state = state
    self._key = key
    self.min_interval_ns = min_interval_ms * NS_PER_MS
    self.heartbeat_ns = heartbeat_ms * NS_PER_MS
    self.session_id = session_id or new_session_id()
    self.seq = 0
    self.last_sent_ns: int | None = None
    self.last_sent_frame = 0
    self._epoch = state.epoch

  def reset(self, state: RpmState) -> None:
    """Start a new session on a fresh state. The app treats a new session's first datagram as
    no fresh evidence, so it stops playing until a new frame advances the counter."""
    self.state = state
    self._epoch = state.epoch
    self.session_id = new_session_id()
    self.seq = 0
    self.last_sent_ns = None
    self.last_sent_frame = 0

  def __repr__(self):
    return f"Sender(session=0x{self.session_id:08X}, seq={self.seq}, key=<redacted>)"

  def maybe_build(self, now_ns: int, allow_valid: bool) -> bytes | None:
    new_frame = self.state.frame_counter != self.last_sent_frame
    since = None if self.last_sent_ns is None else now_ns - self.last_sent_ns
    due_data = new_frame and (since is None or since >= self.min_interval_ns)
    due_hb = since is None or since >= self.heartbeat_ns
    if not (due_data or due_hb):
      return None
    return self.build(now_ns, allow_valid)

  def build(self, now_ns: int, allow_valid: bool) -> bytes:
    if self.seq >= P.U32_MAX or self.state.epoch != self._epoch:
      # seq/frame_counter never wrap within a session (the app would reject a regression forever)
      self._epoch = self.state.epoch
      self.session_id = new_session_id()
      self.seq = 0
    self.seq += 1
    st = self.state
    flags = 0
    if st.last_sample is not None:
      flags |= P.FLAG_FRAME_SEEN
    if allow_valid and st.is_fresh_valid(now_ns):
      flags |= P.FLAG_RPM_VALID
    age = st.frame_age_ms(now_ns)
    pkt = P.Packet(
      flags=flags,
      session_id=self.session_id,
      seq=self.seq,
      frame_counter=st.frame_counter,
      rpm_raw=st.last_sample.rpm_raw if st.last_sample else 0,
      frame_age_ms=P.U16_MAX if age is None else min(age, P.U16_MAX),
      sender_mono_ms=(now_ns // NS_PER_MS) & P.U32_MAX,
    )
    self.last_sent_ns = now_ns
    self.last_sent_frame = st.frame_counter
    return P.encode(pkt, self._key)

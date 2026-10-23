"""Bounded, rate-limited diagnostic summaries for svx_lived, written through the existing cloudlog (swaglog).

Purpose: tell apart, from the logs of one ordinary drive, why the app shows NO_FRAME: no `can` events at all,
events without EMS11, EMS11 only on another bus / as TX echo, rejected frames, drain saturation, scheduling stalls,
or subscriber/read/parse failures.

What is logged (cloudlog event "svxLiveDiag", one JSON line each):
  kind "start"       once, when the daemon first sees the toggle ON: fixed config and the validated git commit
  kind "summary"     every SUMMARY_PERIOD_S (the first one after FIRST_SUMMARY_S): window counters + a few totals
  kind "transition"  reported-status / selection / subscriber changes, at most TRANSITION_LOGS_PER_WINDOW per window
                     and TRANSITION_MIN_GAP_S apart; the rest are only counted
  kind "stop"        a final summary at the start of close(), i.e. a snapshot taken BEFORE the server/source are
                     closed (sessions/listening still show the running state)
Never logged: CAN payload bytes, RPM values, peer addresses, exception messages or type names (only a fixed
category). Every string is from a fixed allowlist, every map has a capped key set, every counter is capped and every
float clamped; a record whose UTF-8 JSON exceeds MAX_RECORD_BYTES is replaced by a minimal one.

Coverage: the CAN census counts only the messages that are actually parsed (the newest KEEP_PER_TICK of each
drain, which the RPM path parses anyway). Messages dequeued beyond that are discarded unparsed and are counted
as `drain_discarded`; their frames are unknown. Bus counts are COUNT ONLY: acceptance is still latest_frame()
on the profile bus, unchanged.

Pure stdlib; importing this has no side effects. Nothing here raises into the daemon loop.
"""
from __future__ import annotations

import json

from . import wire as W

EVENT = "svxLiveDiag"
DIAG_VERSION = 1
SUMMARY_PERIOD_S = 10.0
FIRST_SUMMARY_S = 3.0          # a short drive still gets a summary
TRANSITION_MIN_GAP_S = 1.0
TRANSITION_LOGS_PER_WINDOW = 4
MAX_BUS_KEYS = 8               # distinct RX bus keys per window; further buses go to "other"
MAX_SERVER_KEYS = 24
CENSUS_MAX_FRAMES_PER_TICK = 2048
COUNTER_MAX = 2 ** 32 - 1
FLOAT_MAX = 1e9
MAX_RECORD_BYTES = 4096        # UTF-8 JSON of the event payload (the swaglog envelope adds its fixed context)
TX_ECHO_MIN_SRC = 128          # pandad: src >= 128 is a TX echo / rejected TX, never a receive bus
SLOW_TICK_FACTOR = 2.0         # an interval between ticks > 2 * TICK_S counts as a late tick

STATUS_NAMES = {v: k[len("STATUS_"):].lower() for k, v in vars(W).items() if k.startswith("STATUS_") and isinstance(v, int)}
SELECTIONS = frozenset({"ok", "car_params_unavailable", "unsupported_car", "unsupported_canfd"})
REJECT_REASONS = frozenset({"invalid_event", "len", "parse", "source_error", "bad_time", "future", "too_old", "out_of_order"})
SAMPLE_REASONS = frozenset({"ok", "engine_stopped", "fault", "sna", "implausible"})
# exception CATEGORIES (never the type name or message, which could carry payload); first isinstance match wins
ERROR_CATEGORIES = (("timeout", TimeoutError), ("connection", ConnectionError), ("os", OSError), ("import", ImportError),
                    ("memory", MemoryError), ("value", ValueError), ("type", TypeError), ("attribute", AttributeError),
                    ("key", KeyError), ("index", IndexError), ("runtime", RuntimeError))


def _cap(v) -> int:
  try:
    v = int(v)
  except Exception:
    return 0
  return 0 if v < 0 else (COUNTER_MAX if v > COUNTER_MAX else v)


def _num(v: float, nd: int = 2) -> float:
  if v != v:  # NaN
    return 0.0
  return round(max(-FLOAT_MAX, min(FLOAT_MAX, v)), nd)


def error_category(e: BaseException) -> str:
  for name, cls in ERROR_CATEGORIES:
    if isinstance(e, cls):
      return name
  return "other"


def _safe_key(k) -> str:
  """Server stat keys come from fixed literals in server.py; still allow only short [a-z0-9_] keys."""
  s = str(k)
  return s if 0 < len(s) <= 24 and all(c in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in s) else "other"


def _counts(src: dict, allowed=None, limit: int = 16) -> dict:
  out: dict = {}
  for k, v in src.items():
    key = (k if k in allowed else "other") if allowed is not None else _safe_key(k)
    if key not in out and len(out) >= limit - 1:  # the last slot is reserved for "other"
      key = "other"
    out[key] = _cap(out.get(key, 0) + _cap(v))
  return out


def _delta(now: dict, before: dict) -> dict:
  """Per-window increase of cumulative counters; a counter that went down was reset (new RpmState) and counts in full."""
  out = {}
  for k, v in now.items():
    d = v - before.get(k, 0)
    d = v if d < 0 else d
    if d:
      out[k] = d
  return out


def _add_category(table: dict, e: BaseException) -> None:
  c = error_category(e)  # fixed key set, so the table is bounded by len(ERROR_CATEGORIES) + 1
  table[c] = table.get(c, 0) + 1


def valid_commit(value) -> str:
  """A 40-hex git commit (as written to the GitCommit param by the manager), else "unknown"."""
  if isinstance(value, bytes):
    try:
      value = value.decode("ascii")
    except UnicodeDecodeError:
      return "unknown"
  if isinstance(value, str):
    value = value.strip().lower()
    if len(value) == 40 and all(c in "0123456789abcdef" for c in value):
      return value
  return "unknown"


class _Timing:
  __slots__ = ("n", "total", "max", "min")

  def __init__(self):
    self.n, self.total, self.max, self.min = 0, 0.0, 0.0, None

  def add(self, v: float) -> None:
    self.n += 1
    self.total += v
    if self.n == 1 or v > self.max:
      self.max = v
    if self.min is None or v < self.min:
      self.min = v

  def out(self, scale: float = 1000.0) -> dict | None:
    if not self.n:
      return None
    return {"n": _cap(self.n), "mean": _num(self.total / self.n * scale), "max": _num(self.max * scale),
            "min": _num(self.min * scale)}


class SourceStats:
  """Counters filled by CerealCanSource (only when attached). Every method is O(1) or bounded per tick."""

  def __init__(self):
    self.reset_window()
    self.newest_event_ns: int | None = None  # logMonoTime of the newest valid parsed event of THIS poll only
    self.total_events = 0
    self.total_target_rx = 0

  def reset_window(self) -> None:
    self.w = dict.fromkeys(("polls", "empty_polls", "drained", "parsed", "drain_discarded", "drain_saturated", "read_errors",
                            "parse_errors", "scan_errors", "census_errors", "events_valid", "events_invalid", "census_frames",
                            "census_skipped_frames", "matches", "poll_invalid", "poll_none"), 0)
    self.rx_frames: dict = {}   # bus -> frames on that RX bus (parsed events only)
    self.rx_target: dict = {}   # bus -> target-address frames on that RX bus (count only; acceptance is elsewhere)
    self.tx_frames = 0
    self.tx_target = 0
    self.read_error_types: dict = {}

  def on_drain(self, drained: int, kept: int, saturated: bool) -> None:
    self.newest_event_ns = None
    w = self.w
    w["polls"] += 1
    w["drained"] += drained
    w["parsed"] += kept
    w["drain_discarded"] += drained - kept
    if drained == 0:
      w["empty_polls"] += 1
    if saturated:
      w["drain_saturated"] += 1

  def on_read_error(self, e: BaseException) -> None:
    self.newest_event_ns = None  # never report the age of an event from an earlier poll
    self.w["polls"] += 1
    self.w["read_errors"] += 1
    _add_category(self.read_error_types, e)

  def on_parse_error(self) -> None:
    self.w["parse_errors"] += 1

  def on_scan_error(self) -> None:
    self.w["scan_errors"] += 1

  def on_census_error(self) -> None:
    self.newest_event_ns = None
    self.w["census_errors"] += 1

  def on_result(self, result) -> None:
    if result is None:
      self.w["poll_none"] += 1
    elif result[0] == "invalid":
      self.w["poll_invalid"] += 1
    else:
      self.w["matches"] += 1

  @staticmethod
  def _bus_key(table: dict, bus: int):
    return bus if bus in table or len(table) < MAX_BUS_KEYS else "other"

  def census(self, events, address: int, skip=None) -> None:
    """Count frames per bus in already-parsed events (oldest -> newest), at most CENSUS_MAX_FRAMES_PER_TICK."""
    w = self.w
    budget = CENSUS_MAX_FRAMES_PER_TICK
    newest = None
    for evt in events:
      if evt is skip:
        continue
      if not evt.valid:
        w["events_invalid"] += 1
        continue
      w["events_valid"] += 1
      self.total_events += 1
      newest = evt
      can = evt.can
      n = len(can)
      scan = min(n, budget)
      w["census_frames"] += scan
      w["census_skipped_frames"] += n - scan
      budget -= scan
      for i in range(scan):
        c = can[i]
        src = int(c.src)
        hit = c.address == address
        if src >= TX_ECHO_MIN_SRC:
          self.tx_frames += 1
          self.tx_target += hit
          continue
        k = self._bus_key(self.rx_frames, src)
        self.rx_frames[k] = self.rx_frames.get(k, 0) + 1
        if hit:
          k = self._bus_key(self.rx_target, src)
          self.rx_target[k] = self.rx_target.get(k, 0) + 1
          self.total_target_rx += 1
    self.newest_event_ns = int(newest.logMonoTime) if newest is not None else None

  def window_out(self) -> dict:
    out = {k: _cap(v) for k, v in self.w.items()}
    out["rx_frames"] = {str(k): _cap(v) for k, v in self.rx_frames.items()}
    out["rx_target"] = {str(k): _cap(v) for k, v in self.rx_target.items()}
    out["tx_frames"] = _cap(self.tx_frames)
    out["tx_target"] = _cap(self.tx_target)
    if self.read_error_types:
      out["read_error_types"] = {k: _cap(v) for k, v in self.read_error_types.items()}
    return out


def record_bytes(rec: dict) -> int:
  return len(json.dumps(rec, ensure_ascii=False, allow_nan=False).encode("utf-8"))


class LiveDiagnostics:
  """Owned by LiveDaemon. Does nothing (no emit, no param read) until start(); emission never raises."""

  def __init__(self, emit, revision_fn=None, period_s: float = SUMMARY_PERIOD_S, first_s: float = FIRST_SUMMARY_S):
    self._emit = emit
    self._revision_fn = revision_fn
    self.period_s = period_s
    self.first_s = first_s
    self.active = False
    self.source_stats = SourceStats()
    self.records = 0
    self.emit_errors = 0
    self.truncated = 0
    self._reset_window()
    self._last_tick: float | None = None
    self._next_summary = 0.0
    self._last_transition_log: float | None = None
    self._status: int | None = None
    self._sel: str | None = None
    self._src_present = False
    self._subscriber_error_types: dict = {}
    self._prev_state_rejects: dict = {}
    self._prev_server: dict = {}
    self._started_at = 0.0
    self._last_accept: float | None = None
    self._last_match: float | None = None
    self.total_accepted = 0

  def _reset_window(self) -> None:
    self.w = dict.fromkeys(("ticks", "late_ticks", "source_errors", "accepted", "status_changes",
                            "transition_logs_suppressed", "subscriber_errors", "diag_errors"), 0)
    self.status_ticks: dict = {}
    self.sample_reasons: dict = {}
    self.poll_s = _Timing()
    self.tick_gap_s = _Timing()
    self.event_age_s = _Timing()
    self.window_start: float | None = None
    self.transition_logs = 0

  # ---- emission ----
  def _send(self, kind: str, fields: dict) -> None:
    rec = {"kind": kind, "v": DIAG_VERSION, **fields}
    try:
      if record_bytes(rec) > MAX_RECORD_BYTES:  # not expected by construction; keep the line bounded anyway
        self.truncated += 1
        rec = {"kind": kind, "v": DIAG_VERSION, "truncated": True}
      self._emit(EVENT, **rec)
      self.records += 1
    except Exception:
      self.emit_errors += 1

  def start(self, now: float, config: dict) -> None:
    if self.active:
      return
    self.active = True
    self._started_at = now
    self.window_start = now
    self._next_summary = now + self.first_s
    commit = "unknown"
    if self._revision_fn is not None:
      try:
        commit = valid_commit(self._revision_fn())
      except Exception:
        pass
    self._send("start", {**config, "commit": commit})

  # ---- daemon hooks (cheap; called only while active) ----
  def tick(self, now: float, tick_s: float) -> None:
    w = self.w
    w["ticks"] += 1
    if self._last_tick is not None:
      gap = now - self._last_tick
      self.tick_gap_s.add(gap)
      if gap > tick_s * SLOW_TICK_FACTOR:
        w["late_ticks"] += 1
    self._last_tick = now

  def poll_timing(self, seconds: float) -> None:
    self.poll_s.add(seconds)

  def event_age(self, age_ns: int) -> None:
    self.event_age_s.add(age_ns / 1e9)  # reported in ms like the other timings; negative = event in the future

  def source_error(self) -> None:
    self.w["source_errors"] += 1

  def subscriber_error(self, e: BaseException) -> None:
    self.w["subscriber_errors"] += 1
    _add_category(self._subscriber_error_types, e)

  def accepted(self, now: float, sample_reason: str) -> None:
    self.w["accepted"] += 1
    self.total_accepted += 1
    self._last_accept = now
    k = sample_reason if sample_reason in SAMPLE_REASONS else "other"
    self.sample_reasons[k] = self.sample_reasons.get(k, 0) + 1

  def matched(self, now: float) -> None:
    self._last_match = now

  def observe(self, now: float, status: int, sel: str | None, src_present: bool) -> None:
    """Called once per tick with the reported status; logs a bounded number of transitions."""
    status = status if status in STATUS_NAMES else -1
    self.status_ticks[status] = self.status_ticks.get(status, 0) + 1
    sel = "ok" if sel is None else (sel if sel in SELECTIONS else "other")
    src_present = bool(src_present)
    if status == self._status and sel == self._sel and src_present == self._src_present:
      return
    prev = (self._status, self._sel, self._src_present)
    self._status, self._sel, self._src_present = status, sel, src_present
    self.w["status_changes"] += 1
    if prev[0] is None:
      return  # the first observation is part of the next summary, not a transition
    gap_ok = self._last_transition_log is None or now - self._last_transition_log >= TRANSITION_MIN_GAP_S
    if not gap_ok or self.transition_logs >= TRANSITION_LOGS_PER_WINDOW:
      self.w["transition_logs_suppressed"] += 1
      return
    self._last_transition_log = now
    self.transition_logs += 1
    self._send("transition", {"t": _num(now - self._started_at, 3),
                              "status_from": STATUS_NAMES.get(prev[0], "unknown"), "status": STATUS_NAMES.get(status, "unknown"),
                              "sel_from": prev[1], "sel": sel, "src_from": prev[2], "src": src_present})

  def due(self, now: float) -> bool:
    return self.active and now >= self._next_summary

  def summary(self, now: float, snap: dict, kind: str = "summary") -> None:
    """`snap` holds daemon state read by the caller: state rejects, server stats, sessions, listening, tick errors."""
    self._next_summary = now + self.period_s
    try:
      st = self.source_stats
      state_rejects = dict(snap.get("state_rejects", {}))
      server = dict(snap.get("server", {}))
      fields = {
        "t": _num(now - self._started_at, 3),
        "window_s": _num(now - (self.window_start if self.window_start is not None else now), 3),
        "sel": self._sel, "src": self._src_present,
        "status": STATUS_NAMES.get(self._status, "unknown"),
        "status_ticks": {STATUS_NAMES.get(k, "unknown"): _cap(v) for k, v in self.status_ticks.items()},
        **{k: _cap(v) for k, v in self.w.items()},
        "can": st.window_out(),
        "state_rejects": _counts(_delta(state_rejects, self._prev_state_rejects), REJECT_REASONS),
        "sample_reasons": _counts(self.sample_reasons, SAMPLE_REASONS),
        "server": _counts(_delta(server, self._prev_server), limit=MAX_SERVER_KEYS),
        "sessions": _cap(snap.get("sessions", 0)), "listening": bool(snap.get("listening", False)),
        "poll_ms": self.poll_s.out(), "tick_gap_ms": self.tick_gap_s.out(), "event_age_ms": self.event_age_s.out(),
        "since_accept_ms": None if self._last_accept is None else _cap(round((now - self._last_accept) * 1000)),
        "since_match_ms": None if self._last_match is None else _cap(round((now - self._last_match) * 1000)),
        "total_events": _cap(st.total_events), "total_target_rx": _cap(st.total_target_rx),
        "total_accepted": _cap(self.total_accepted), "tick_errors": _cap(snap.get("tick_errors", 0)),
        "records": _cap(self.records), "emit_errors": _cap(self.emit_errors), "truncated_records": _cap(self.truncated),
      }
      if self._subscriber_error_types:
        fields["subscriber_error_types"] = {k: _cap(v) for k, v in self._subscriber_error_types.items()}
      self._prev_state_rejects = state_rejects
      self._prev_server = server
    except Exception:
      self.emit_errors += 1
      fields = {"failed": True}  # never an "error" key: cloudlog.event would log the record at ERROR level
    self._send(kind, fields)
    self._reset_window()
    self.window_start = now
    self.source_stats.reset_window()

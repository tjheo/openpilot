#!/usr/bin/env python3
"""svx_lived: optional read-only EMS11 engine-RPM telemetry for the SVX phone app (SVXL v1 over TCP).

Started by the manager only while onroad AND Params SvxLiveEnabled is on (manager_policy). It then listens
on TCP SvxLivePort (default 47816); the phone connects, does the keyless HELLO/WELCOME handshake and
receives RPM telemetry on the same connection. The handshake is NOT authentication: any LAN/tailnet peer
that speaks the protocol can read the RPM. Nothing a peer sends can control anything.

READ-ONLY: subscribes to the existing cereal `can` service (only after the supported car is identified)
and reads the CarParams/SvxLive* params. No publishers, no sendcan, no Panda/USB/socketcan access,
no diagnostic requests, no param writes.

Importing this module has no side effects; everything happens in main().
"""
from __future__ import annotations

import os
import signal
import sys
import threading
import time

from . import wire as W
from .cereal_source import KEEP_PER_TICK, MAX_DRAIN_PER_TICK
from .diagnostics import CENSUS_MAX_FRAMES_PER_TICK, LiveDiagnostics, error_category
from .manager_policy import DEFAULT_PORT, ENABLED_PARAM, PORT_PARAM, parse_port
from .netpolicy import AddressPolicy
from .profile import PROFILES, selection_error
from .rpm_state import INVALID, RpmState, telemetry_fields
from .server import LiveServer

PROFILE = "kia_seltos_2021_ems11"
TICK_S = 0.02                  # 50 Hz: CAN poll, telemetry and socket I/O
ENABLED_RECHECK_S = 0.5        # toggle OFF -> sessions closed within ~0.5 s (manager also stops us)
SELECTION_RECHECK_S = 2.0
BIND_RETRY_S = 5.0
IDLE_S = 0.5
ERROR_LOG_PERIOD_S = 10.0     # also the minimum interval between "can subscriber unavailable" lines


def clock_boottime_ns() -> int:
  return time.clock_gettime_ns(time.CLOCK_BOOTTIME)


def _stderr_log(msg: str) -> None:
  print(f"[svx_lived] {msg}", file=sys.stderr, flush=True)


class LiveDaemon:
  def __init__(self, server: LiveServer | None, enabled_fn, selection_reader, source_factory,
               clock_ns=clock_boottime_ns, mono=time.monotonic, logger=_stderr_log, sleep=time.sleep,
               diag: LiveDiagnostics | None = None):
    self.server = server
    self.enabled_fn = enabled_fn
    self.sel_reader = selection_reader
    self.source_factory = source_factory
    self.clock_ns = clock_ns
    self.mono = mono
    self._logger = logger
    self.sleep = sleep
    self.profile = PROFILES[PROFILE]
    self.state = RpmState()
    self._epoch = self.state.epoch
    self.selection = None
    self.sel_error: str | None = "car_params_unavailable"
    self.src = None
    self._next_enabled = 0.0
    self._next_sel = 0.0
    self._next_bind = 0.0
    self.frames = 0
    self.errors = 0
    self._last_error_log: float | None = None
    self._last_subscriber_log: float | None = None
    self.diag = diag  # bounded svxLiveDiag summaries; emits nothing until the toggle has been seen ON

  def _diag_failed(self) -> None:
    try:
      self.diag.w["diag_errors"] += 1
    except Exception:
      pass

  def _diag_start(self, now: float) -> None:
    try:
      self.diag.start(now, {
        "profile": self.profile.name, "address": self.profile.address, "bus": sorted(self.profile.allowed_buses),
        "protocol": W.VERSION, "tick_ms": round(TICK_S * 1000), "stale_ms": self.state.stale_ms,
        "max_drain": MAX_DRAIN_PER_TICK, "keep": KEEP_PER_TICK, "census_max_frames": CENSUS_MAX_FRAMES_PER_TICK,
        "summary_s": self.diag.period_s, "port_valid": self.server is not None,
      })
    except Exception:
      self._diag_failed()

  def _diag_snapshot(self) -> dict:
    srv = self.server
    return {"state_rejects": self.state.rejects, "server": srv.stats if srv is not None else {},
            "sessions": len(srv.sessions) if srv is not None else 0, "listening": bool(srv is not None and srv.listening),
            "tick_errors": self.errors}

  def log(self, msg: str) -> None:
    try:
      self._logger(msg)
    except Exception:
      pass

  def _enabled(self) -> bool:
    try:
      return bool(self.enabled_fn())
    except Exception:
      return False

  def _reset_data(self, why: str) -> None:
    """Drop every sample and end every session: no pre-reset frame can ever be reported again."""
    if self.server is not None:
      self.server.end_sessions(W.BYE_CAR_CHANGED)
    self._close_source()
    self.state = RpmState()
    self._epoch = self.state.epoch
    self.log(f"data reset ({why})")

  def _close_source(self) -> None:
    if self.src is not None:
      try:
        self.src.close()
      except Exception:
        pass
      self.src = None

  def _check_selection(self, now: float) -> None:
    if now < self._next_sel:
      return
    self._next_sel = now + SELECTION_RECHECK_S
    try:
      sel = self.sel_reader.read()
    except Exception:
      sel = None
    err = selection_error(self.profile, sel)
    was_ok = self.sel_error is None
    if was_ok and (err is not None or sel != self.selection):
      self._reset_data("car selection lost or changed")
    if err != self.sel_error:
      self.log(f"selection: {err or 'ok'} (profile {self.profile.name})")
    self.sel_error = err
    self.selection = sel if err is None else None
    if err is None and self.src is None:
      try:
        self.src = self.source_factory(self.profile)  # the `can` subscription exists only for the supported car
      except Exception as e:
        self.src = None
        if self._last_subscriber_log is None or now - self._last_subscriber_log >= ERROR_LOG_PERIOD_S:
          self._last_subscriber_log = now  # retried every SELECTION_RECHECK_S, logged at most every 10 s
          self.log(f"can subscriber unavailable ({error_category(e)}); reporting no frames")
        if self.diag is not None:
          try:
            self.diag.subscriber_error(e)
          except Exception:
            self._diag_failed()

  def step(self) -> bool:
    """One bounded iteration that takes at least TICK_S of wall-clock time: one selector round with the
    remaining tick budget, then sleep to the deadline. A peer that keeps a socket readable therefore
    cannot make the loop spin faster than 50 Hz. Returns False when the daemon must stop."""
    now = self.mono()
    deadline = now + TICK_S
    try:
      ok = self._work(now, deadline)
    except Exception as e:  # never crash-loop under the manager: report (rate-limited) and keep the cadence
      ok = True
      self.errors += 1
      if self._last_error_log is None or now - self._last_error_log >= ERROR_LOG_PERIOD_S:
        self._last_error_log = now
        self.log(f"tick failed ({error_category(e)}); {self.errors} total, continuing")  # category only, never the message
    rest = deadline - self.mono()
    if ok and rest > 0:
      self.sleep(rest)
    return ok

  def _work(self, now: float, deadline: float) -> bool:
    if now >= self._next_enabled:
      self._next_enabled = now + ENABLED_RECHECK_S
      if not self._enabled():
        self.log("disabled: closing sessions")
        return False
      if self.diag is not None and not self.diag.active:
        self._diag_start(now)
    if self.server is None:
      self.sleep(IDLE_S)  # no valid port: idle until disabled or stopped
      return True
    if not self.server.listening and now >= self._next_bind:
      self._next_bind = now + BIND_RETRY_S
      self.server.open()
    diag = self.diag if self.diag is not None and self.diag.active else None
    if diag is not None:
      try:
        diag.tick(now, TICK_S)
      except Exception:
        self._diag_failed()
    self._check_selection(now)
    if self.sel_error is None and self.src is not None:
      t_poll = self.mono() if diag is not None else 0.0
      try:
        frame = self.src.poll()
      except Exception:
        frame = (INVALID, "source_error")
        if diag is not None:
          try:
            diag.source_error()
          except Exception:
            self._diag_failed()
      accepted = False
      if frame is not None:
        if frame[0] == INVALID:
          self.state.invalidate(frame[1])  # explicit bad newest evidence -> INVALID now, never older data
        elif self.state.ingest(frame[0], frame[1], self.clock_ns()):
          self.frames += 1
          accepted = True
      if diag is not None:
        self._diag_poll(diag, now, t_poll, frame, accepted)
    if self.state.epoch != self._epoch:
      self.server.end_sessions(W.BYE_CAR_CHANGED)  # counter wrapped: sessions must never see it regress
      self._epoch = self.state.epoch
    fields = telemetry_fields(self.sel_error, self.state, self.clock_ns())
    self.server.publish(fields)
    if diag is not None:
      try:
        diag.observe(now, fields["status"], self.sel_error, self.src is not None)
        if diag.due(now):
          diag.summary(now, self._diag_snapshot())
      except Exception:
        self._diag_failed()
    self.server.poll(max(0.0, deadline - self.mono()))
    return True

  def _diag_poll(self, diag: LiveDiagnostics, now: float, t_poll: float, frame, accepted: bool) -> None:
    try:
      diag.poll_timing(self.mono() - t_poll)
      newest = diag.source_stats.newest_event_ns
      if newest is not None:
        diag.event_age(self.clock_ns() - newest)
      if frame is not None and frame[0] != INVALID:
        diag.matched(now)
      if accepted:
        diag.accepted(now, self.state.last_sample.reason)
    except Exception:
      self._diag_failed()

  def run(self, stop: threading.Event) -> int:
    try:
      while not stop.is_set():
        if not self.step():
          break
    finally:
      self.close()
    return 0

  def close(self) -> None:
    if self.diag is not None and self.diag.active:
      try:
        self.diag.summary(self.mono(), self._diag_snapshot(), kind="stop")  # pre-close snapshot, once
      except Exception:
        self._diag_failed()
      self.diag.active = False
    if self.server is not None:
      self.server.close(W.BYE_STOPPING)
    self._close_source()
    stats = self.server.stats if self.server is not None else {}
    self.log(f"stopped: frames={self.frames} rejects={self.state.rejects} server={stats}")


def lower_priority() -> None:
  try:
    os.setpriority(os.PRIO_PROCESS, 0, 19)  # lowest SCHED_OTHER priority; never competes with RT processes
  except (AttributeError, OSError):
    pass


def install_stop_handlers(stop: threading.Event) -> None:
  for s in (signal.SIGINT, signal.SIGTERM):  # manager stop() sends SIGINT
    signal.signal(s, lambda signum, frame: stop.set())


def main() -> int:
  from openpilot.common.params import Params
  from .cereal_source import CerealCanSource, ParamsCarSelection

  try:
    from openpilot.common.swaglog import cloudlog
    logger, emit = cloudlog.info, cloudlog.event
  except Exception:
    logger, emit = _stderr_log, None

  lower_priority()
  stop = threading.Event()
  install_stop_handlers(stop)

  params = Params()
  # automatic while enabled, persisted by logmessaged; GitCommit is read once, only when the toggle is seen ON
  diag = LiveDiagnostics(emit, revision_fn=lambda: params.get("GitCommit")) if emit is not None else None
  try:
    port = parse_port(params.get(PORT_PARAM))
  except Exception:
    port = None
  server = None
  if port is None:
    logger(f"{PORT_PARAM} invalid (expected 1024..65535, default {DEFAULT_PORT}): not listening")
  else:
    server = LiveServer(port, AddressPolicy(allow_loopback=False), host="0.0.0.0", logger=logger)
  daemon = LiveDaemon(server, lambda: params.get_bool(ENABLED_PARAM), ParamsCarSelection(),
                      lambda prof: CerealCanSource(prof.address, 0, stats=diag.source_stats if diag is not None else None),
                      logger=logger, diag=diag)
  return daemon.run(stop)


if __name__ == "__main__":
  sys.exit(main())

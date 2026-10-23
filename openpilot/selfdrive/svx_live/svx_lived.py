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
ERROR_LOG_PERIOD_S = 10.0


def clock_boottime_ns() -> int:
  return time.clock_gettime_ns(time.CLOCK_BOOTTIME)


def _stderr_log(msg: str) -> None:
  print(f"[svx_lived] {msg}", file=sys.stderr, flush=True)


class LiveDaemon:
  def __init__(self, server: LiveServer | None, enabled_fn, selection_reader, source_factory,
               clock_ns=clock_boottime_ns, mono=time.monotonic, logger=_stderr_log, sleep=time.sleep):
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
        self.log(f"can subscriber unavailable ({type(e).__name__}); reporting no frames")

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
        self.log(f"tick failed ({type(e).__name__}: {e}); {self.errors} total, continuing")
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
    if self.server is None:
      self.sleep(IDLE_S)  # no valid port: idle until disabled or stopped
      return True
    if not self.server.listening and now >= self._next_bind:
      self._next_bind = now + BIND_RETRY_S
      self.server.open()
    self._check_selection(now)
    if self.sel_error is None and self.src is not None:
      try:
        frame = self.src.poll()
      except Exception:
        frame = (INVALID, "source_error")
      if frame is not None:
        if frame[0] == INVALID:
          self.state.invalidate(frame[1])  # explicit bad newest evidence -> INVALID now, never older data
        elif self.state.ingest(frame[0], frame[1], self.clock_ns()):
          self.frames += 1
    if self.state.epoch != self._epoch:
      self.server.end_sessions(W.BYE_CAR_CHANGED)  # counter wrapped: sessions must never see it regress
      self._epoch = self.state.epoch
    self.server.publish(telemetry_fields(self.sel_error, self.state, self.clock_ns()))
    self.server.poll(max(0.0, deadline - self.mono()))
    return True

  def run(self, stop: threading.Event) -> int:
    try:
      while not stop.is_set():
        if not self.step():
          break
    finally:
      self.close()
    return 0

  def close(self) -> None:
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
    logger = cloudlog.info
  except Exception:
    logger = _stderr_log

  lower_priority()
  stop = threading.Event()
  install_stop_handlers(stop)

  params = Params()
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
                      lambda prof: CerealCanSource(prof.address, 0), logger=logger)
  return daemon.run(stop)


if __name__ == "__main__":
  sys.exit(main())

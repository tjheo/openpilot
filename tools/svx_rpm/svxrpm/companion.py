"""Companion main loop: fixed cadence, bounded work per tick, fail-closed, bounded stop.

Dependencies (CAN source, car selection reader, UDP output, clock) are injected so the loop is
testable without openpilot. Nothing in here can block the driving stack: it only reads.
"""
import errno
import socket
import sys
import threading
import time

from .config import Config
from .core import RpmState, Sender
from .profile import PROFILES, selection_error

TICK_S = 0.02                 # 50 Hz loop; also the max data datagram rate
HEARTBEAT_MS = 200
SELECTION_RECHECK_S = 2.0
SEND_ERROR_LOG_PERIOD_S = 10.0
EXIT_UNSUPPORTED = 3


def clock_boottime_ns() -> int:
  return time.clock_gettime_ns(time.CLOCK_BOOTTIME)


def log(msg: str) -> None:
  print(f"[svx_rpm] {msg}", file=sys.stderr, flush=True)


class UdpOut:
  """Non-blocking unicast UDP. SO_BROADCAST is never enabled, so broadcast sends fail in the kernel."""

  def __init__(self, target_ip: str, port: int):
    self.dest = (target_ip, port)
    self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    self.sock.setblocking(False)

  def send(self, data: bytes) -> None:
    self.sock.sendto(data, self.dest)

  def close(self) -> None:
    if self.sock is not None:
      self.sock.close()
      self.sock = None


class Companion:
  def __init__(self, cfg: Config, can_source, selection_reader, out, clock_ns=clock_boottime_ns, logger=log,
               mono_s=time.monotonic):
    self.cfg = cfg
    self.profile = PROFILES[cfg.profile]
    self.src = can_source
    self.sel_reader = selection_reader
    self.out = out
    self.clock_ns = clock_ns
    self.mono_s = mono_s
    self.log = logger
    self.state = RpmState()
    self.sender = Sender(self.state, key=cfg.key, min_interval_ms=int(TICK_S * 1000), heartbeat_ms=HEARTBEAT_MS)
    self.sel_error = "car_params_unavailable"
    self._next_sel_check = 0.0
    self.sent = 0
    self.send_errors = 0
    self._last_err_log = None
    self.exit_code = None

  def _check_selection(self) -> None:
    now = self.mono_s()
    if now < self._next_sel_check:
      return
    self._next_sel_check = now + SELECTION_RECHECK_S
    try:
      err = selection_error(self.profile, self.sel_reader.read())
    except Exception:
      err = "car_params_unavailable"
    if err != self.sel_error:
      self.log(f"selection: {err or 'ok'} (profile {self.profile.name})")
      if self.sel_error is None:
        # Lost a verified selection: drop all data and start a new session, so the app cannot keep
        # playing a cached valid advance and no pre-loss frame can ever be reported again.
        self.state = RpmState()
        self.sender.reset(self.state)
      self.sel_error = err
    if err in ("unsupported_car", "unsupported_canfd"):
      self.exit_code = EXIT_UNSUPPORTED

  def step(self) -> bool:
    """One bounded iteration. Returns False when the companion must stop."""
    self._check_selection()
    if self.exit_code is not None:
      self._send(self.sender.build(self.clock_ns(), allow_valid=False))  # final no-evidence heartbeat
      return False
    if self.sel_error is None:
      frame = self.src.poll()
      if frame is not None:
        self.state.ingest(frame[0], frame[1], self.clock_ns())
    else:
      self.src.discard()  # keep the subscriber drained without using any data
    data = self.sender.maybe_build(self.clock_ns(), allow_valid=self.sel_error is None)
    if data is not None:
      self._send(data)
    return True

  def _send(self, data: bytes) -> None:
    try:
      self.out.send(data)
      self.sent += 1
    except OSError as e:
      self.send_errors += 1
      now = self.mono_s()
      if self._last_err_log is None or now - self._last_err_log >= SEND_ERROR_LOG_PERIOD_S:
        self._last_err_log = now
        name = errno.errorcode.get(e.errno, str(e.errno))
        self.log(f"send failed ({name}); {self.send_errors} total, continuing at normal cadence")

  def run(self, stop: threading.Event, duration_s: float = 0.0) -> int:
    t_end = self.mono_s() + duration_s if duration_s > 0 else None
    try:
      while not stop.is_set() and (t_end is None or self.mono_s() < t_end):
        if not self.step():
          break
        stop.wait(TICK_S)
    finally:
      self.close()
    return self.exit_code or 0

  def close(self) -> None:
    for obj in (self.out, self.src):
      try:
        obj.close()
      except Exception:
        pass
    self.log(f"stopped: datagrams={self.sent} send_errors={self.send_errors} frames={self.state.frame_counter} " +
             f"rejects={self.state.rejects} source_rejects={getattr(self.src, 'rejects', {})}")

  def status_line(self) -> str:
    now = self.clock_ns()
    age = self.state.frame_age_ms(now)
    rpm = self.state.last_sample.rpm if self.state.last_sample else None
    valid = self.sel_error is None and self.state.is_fresh_valid(now)
    return f"selection={self.sel_error or 'ok'} rpm={rpm} age_ms={age} valid={valid} sent={self.sent}"

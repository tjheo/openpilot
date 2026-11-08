"""Fresh, bounded request-episode lifecycle. Thresholds are NOT road-validated.

Caller now and camera EOF share modeld's monotonic clock (see modeld.py's
camera_age_at_run_ms). No clock supplied => unavailable, never force a turn.
"""
import math


class TurnLifecycle:
  MAX_DURATION = 30.0
  MAX_FRAME_AGE = 0.35
  MAX_OBSERVATION_GAP = 0.35

  def __init__(self):
    self.reset()

  def reset(self):
    self.direction = 0
    self.episode = None
    self.peak = self.started = self.settled = 0.0
    self.completed = False
    self.cancelled = False
    self.request_since = None
    self.execution_since = None
    self.last_now = self.last_eof = self.last_frame = None
    self.reason = 'idle'

  def _break_geometry(self):
    self.peak = self.started = self.settled = 0.0

  def update(self, direction, angle, speed, rates, headings, valid, dt,
             *, now=None, frame_id=None, timestamp_eof=None, cancel=False, episode=None,
             driver_request=False):
    if direction not in (1, 2):
      self.reset()
      return False
    if direction != self.direction or (episode is not None and episode != self.episode):
      self.reset()
      self.direction = direction
      self.episode = episode
    if self.completed or self.cancelled:
      return True  # consume only this observed lamp/request episode
    if cancel:
      self.cancelled = True
      self.reason = 'cancelled'
      return True
    clock_ok = type(now) in (int, float) and math.isfinite(now) and now >= 0
    if not clock_ok or (self.last_now is not None and now <= self.last_now):
      self._break_geometry()
      self.reason = 'clock_unavailable'
      return True
    if self.request_since is None:
      self.request_since = now
    # Driver lamps may remain held throughout a traffic-light wait. Only
    # observed fresh moving steering initiation starts their execution cap.
    # Automatic preparation retains its separate bounded request lifetime.
    deadline_since = self.execution_since if driver_request else self.request_since
    if deadline_since is not None and now - deadline_since >= self.MAX_DURATION:
      self.cancelled = True
      self.reason = 'deadline'
      return True
    gap = now - self.last_now if self.last_now is not None else None
    self.last_now = now
    metadata_ok = (type(frame_id) is int and frame_id >= 0 and
                   type(timestamp_eof) is int and timestamp_eof > 0 and
                   0 <= now - timestamp_eof * 1e-9 < self.MAX_FRAME_AGE)
    progress = (metadata_ok and (self.last_frame is None or
                (frame_id > self.last_frame and timestamp_eof > self.last_eof)))
    samples = list(rates)[:16] + list(headings)[:16] + [angle, speed]
    geometry_ok = (valid and len(rates) >= 16 and len(headings) >= 16 and
                   all(math.isfinite(x) for x in samples))
    if not progress or not geometry_ok or (gap is not None and gap > self.MAX_OBSERVATION_GAP):
      # Store a progressing observation even if geometry is unavailable, so a
      # repeated invalid frame cannot be accepted on recovery. No settle credit.
      if progress:
        self.last_frame, self.last_eof = frame_id, timestamp_eof
      self._break_geometry()
      self.reason = 'observation_unavailable'
      return True
    self.last_frame, self.last_eof = frame_id, timestamp_eof
    # First sample proves no elapsed interval. Caller cadence never substitutes
    # for camera progress; long gaps never count as initiation or settle time.
    elapsed = min(dt, gap) if gap is not None else 0.0
    self.reason = 'tracking'
    sign = 1 if direction == 1 else -1
    if speed > 1.0 and angle * sign > 20:
      self.started = min(0.3, self.started + elapsed)
      self.peak = max(self.peak, abs(angle))
    elif self.started < 0.3:
      self.started = 0.0
    if self.started >= 0.3 and self.execution_since is None:
      self.execution_since = now
    # Geometry discontinuity clears initiation/settle credit, not execution
    # lifetime: a stop or stale-frame recovery must never restart the cap.
    settled = (self.started >= 0.3 and speed > 1.0 and
               abs(angle) <= min(20.0, self.peak * 0.25) and
               max(abs(x) for x in rates[:16]) < 0.05 and
               abs(headings[15] - headings[5]) < 0.06)
    self.settled = self.settled + elapsed if settled else 0.0
    self.completed = self.settled >= 1.0
    if self.completed:
      self.reason = 'completed'
    elif self.started < 0.3:
      self.reason = 'awaiting_steering_initiation'
    elif speed <= 1.0:
      self.reason = 'stopped'
    elif abs(angle) > min(20.0, self.peak * 0.25):
      self.reason = 'steering_not_settled'
    elif max(abs(x) for x in rates[:16]) >= 0.05:
      self.reason = 'predicted_yaw_not_settled'
    elif abs(headings[15] - headings[5]) >= 0.06:
      self.reason = 'predicted_heading_not_settled'
    else:
      self.reason = 'settling'
    return self.completed

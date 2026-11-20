"""Early gentle deceleration toward a confirmed slower lead, and a smooth stop.

Two bounded post-MPC corrections, applied to the published acceleration
target only (the MPC state, obstacles, follow distance and stop position are
not modified):

1. Anticipation (``LeadEarlyDecel``). While closing on a confirmed slower or
   stopped radar+vision lead, compute the constant relative deceleration that
   would match the lead speed exactly at the MPC's own equilibrium gap
   (``t_follow * vLead + stop_distance``). Once that requirement exceeds a small
   onset, command a mild, jerk-limited deceleration of the same size. The
   result is ``min(mpc, anticipation)``: braking is never weaker, and a car
   that starts braking earlier needs a lower peak later.

2. Smooth stop (``LeadSmoothStop``, requires 1). Only for a confirmed
   stationary lead that is the MPC's binding obstacle, after anticipation has
   engaged on the same track. If the MPC asks for harder braking than the
   lag-compensated kinematic requirement to stop *before* the lead stop gap,
   ease the MPC target to that requirement times a safety factor. The floor is
   always negative, limited in size, re-tightens immediately when the need
   rises, and disappears on any gate failure (late detection, cut-in, FCW,
   leadTwo/traffic/cruise obstacle binding, driver input, stop/hold states).

Neither path runs below standstill-hold speeds, sends a stop/go request or
touches LongControl, CAN or lateral control.
"""

from __future__ import annotations

from dataclasses import dataclass
import math


# Lead confirmation (both paths)
MIN_MODEL_PROB = 0.5
STATIONARY_MIN_MODEL_PROB = 0.7
STATIONARY_LEAD_SPEED = 1.0       # m/s, at or below: treated as a stopped lead
MAX_RANGE_MOVING = 120.0          # m
MAX_RANGE_STATIONARY = 100.0      # m
CONFIRM_TIME = 0.5                # s of the same radar track, continuously eligible
CLOSING_SPEED_MIN = 0.5           # m/s, vRel must be <= -this
PULL_AWAY_HORIZON = 1.5           # s, positive lead accel predicted to stop the closing
MAX_DT = 0.2                      # s, a longer planner gap resets state

# Anticipation
ONSET_DECEL = 0.30                # m/s^2 required before anything is commanded
FULL_DECEL = 0.70                 # m/s^2 required for the full (1:1) command
ANTICIPATION_GAIN = 1.15          # command slightly above the requirement -> it shrinks
MAX_ANTICIPATION_DECEL = 1.5      # m/s^2, mild comfort braking cap
ATTACK_JERK_BRAKE = 1.0           # m/s^3 while building braking (state <= 0)
ATTACK_JERK_THROTTLE = 1.0        # m/s^3 while only removing positive accel
RELEASE_JERK = 1.5                # m/s^3 when the correction is no longer needed
LOW_SPEED_FADE = (1.5, 3.0)       # m/s, anticipation fades out below this band
ENGAGED_DECEL = 0.3               # m/s^2 anticipation reached -> smooth stop allowed
ENGAGED_MARGIN = 0.05             # m/s^2 ... while braking harder than the plan

# Smooth stop (final approach to a stationary lead)
RELIEF_LAG = 0.4                  # s of travel at the current decel before braking changes
RELIEF_BUFFER = 1.5               # m kept in front of the lead stop gap
RELIEF_MIN_DISTANCE = 0.5         # m of usable distance, else legacy
RELIEF_SAFETY_FACTOR = 1.2        # floor = factor * need + offset
RELIEF_OFFSET = 0.05              # m/s^2
RELIEF_MIN_FLOOR = 0.3            # m/s^2, the eased target always brakes at least this much
RELIEF_MAX_NEED = 1.5             # m/s^2, larger floors mean margin is gone -> legacy
RELIEF_MAX_DELTA = 1.0            # m/s^2, never ease the MPC target by more than this
RELIEF_ENTRY_JERK = 2.0           # m/s^3, ramp of the easing allowance after (re)entry
RELIEF_SPEED = (0.3, 20.0)        # m/s
RELIEF_LOW_SPEED_FADE = (0.3, 1.5)  # m/s, easing hands back to the MPC target continuously


def _smoothstep(x: float, lo: float, hi: float) -> float:
  if x <= lo:
    return 0.0
  if x >= hi:
    return 1.0
  t = (x - lo) / (hi - lo)
  return t * t * (3.0 - 2.0 * t)


def _finite(*values) -> bool:
  try:
    return all(math.isfinite(float(v)) for v in values)
  except (TypeError, ValueError):
    return False


def lead_is_confirmable(lead) -> bool:
  """Radar+vision lead with sane values, plausible range and probability."""
  if lead is None or not bool(getattr(lead, 'status', False)) or not bool(getattr(lead, 'radar', False)):
    return False
  if int(getattr(lead, 'radarTrackId', -1)) < 0:
    return False
  d_rel, v_rel, v_lead = getattr(lead, 'dRel', math.nan), getattr(lead, 'vRel', math.nan), getattr(lead, 'vLead', math.nan)
  a_lead, prob = getattr(lead, 'aLeadK', math.nan), getattr(lead, 'modelProb', math.nan)
  if not _finite(d_rel, v_rel, v_lead, a_lead, prob) or d_rel <= 0.0 or v_lead < -0.5:
    return False
  stationary = v_lead <= STATIONARY_LEAD_SPEED
  max_range = MAX_RANGE_STATIONARY if stationary else MAX_RANGE_MOVING
  min_prob = STATIONARY_MIN_MODEL_PROB if stationary else MIN_MODEL_PROB
  return d_rel <= max_range and prob >= min_prob


def lead_is_closing(lead) -> bool:
  v_rel = float(lead.vRel)
  if v_rel > -CLOSING_SPEED_MIN:
    return False
  # A lead that is clearly accelerating away soon stops the closing: no brake.
  predicted = v_rel + max(0.0, float(lead.aLeadK)) * PULL_AWAY_HORIZON
  return predicted <= -CLOSING_SPEED_MIN


def required_relative_decel(d_rel: float, v_rel: float, v_lead: float, t_follow: float, stop_distance: float) -> float:
  """Constant relative decel matching lead speed at the MPC equilibrium gap."""
  closing = max(0.0, -float(v_rel))
  if closing <= 0.0:
    return 0.0
  gap_target = max(0.0, float(t_follow)) * max(0.0, float(v_lead)) + max(0.0, float(stop_distance))
  available = float(d_rel) - gap_target
  if available <= RELIEF_MIN_DISTANCE:
    return math.inf
  return closing * closing / (2.0 * available)


def anticipation_target(required: float, v_ego: float) -> float:
  """Negative accel target (0 = none) for a given kinematic requirement."""
  if not math.isfinite(required):
    command = MAX_ANTICIPATION_DECEL
  else:
    command = min(MAX_ANTICIPATION_DECEL,
                  ANTICIPATION_GAIN * required * _smoothstep(required, ONSET_DECEL, FULL_DECEL))
  command *= _smoothstep(float(v_ego), *LOW_SPEED_FADE)
  return -command


def stationary_stop_need(d_rel: float, v_ego: float, stop_distance: float, a_ego: float = 0.0) -> float:
  """Lag-compensated decel to stop before the lead stop gap; inf if no margin.

  During the lag the car keeps its measured deceleration, credited at most
  RELIEF_MAX_NEED; positive measured accel or nonfinite values credit none.
  """
  v = max(0.0, float(v_ego))
  a = max(-RELIEF_MAX_NEED, min(0.0, float(a_ego))) if math.isfinite(float(a_ego)) else 0.0
  lag = RELIEF_LAG if a == 0.0 else min(RELIEF_LAG, v / -a)
  v_after = max(0.0, v + a * lag)
  usable = (float(d_rel) - max(0.0, float(stop_distance)) - RELIEF_BUFFER
            - (v + v_after) * 0.5 * lag - v_after * (RELIEF_LAG - lag))
  v = v_after
  if usable <= RELIEF_MIN_DISTANCE:
    return math.inf
  return v * v / (2.0 * usable)


def smooth_stop_floor(d_rel: float, v_ego: float, stop_distance: float, a_ego: float = 0.0) -> float | None:
  """Weakest allowed target while easing a final approach, or None (legacy)."""
  need = stationary_stop_need(d_rel, v_ego, stop_distance, a_ego)
  if not math.isfinite(need):
    return None
  floor = max(RELIEF_MIN_FLOOR, RELIEF_SAFETY_FACTOR * need + RELIEF_OFFSET)
  if floor > RELIEF_MAX_NEED:
    return None
  return -floor


@dataclass(frozen=True)
class EarlyDecelResult:
  accel: float
  anticipation: float | None = None   # current rate-limited anticipation cap, if any
  relief: float = 0.0                 # how much the MPC target was eased (>= 0)
  active: bool = False                # output differs from the MPC target
  reason: str = 'off'


class LeadEarlyDecelState:
  def __init__(self):
    self.reset()

  def reset(self):
    self.track_id = -1
    self.confirm_time = 0.0
    self.state = None          # rate-limited anticipation cap (m/s^2) or None
    self.engaged_track = -1    # track on which anticipation actually braked
    self.relief_cap = 0.0

  def _confirm(self, lead, dt: float) -> bool:
    if not lead_is_confirmable(lead):
      self.track_id, self.confirm_time = -1, 0.0
      return False
    track_id = int(lead.radarTrackId)
    if track_id != self.track_id:
      self.track_id, self.confirm_time = track_id, 0.0
      if self.engaged_track != track_id:
        self.engaged_track = -1
      return False
    self.confirm_time = min(self.confirm_time + dt, 10.0)
    return self.confirm_time >= CONFIRM_TIME - 1e-9

  def _release(self, base: float, dt: float) -> None:
    if self.state is not None:
      self.state += RELEASE_JERK * dt
      if self.state >= base:
        self.state = None

  def update(self, *, enabled: bool, smooth_stop_enabled: bool, relief_allowed: bool, dt: float,
             base_accel: float, v_ego: float, lead, t_follow: float, stop_distance: float,
             a_ego: float = 0.0) -> EarlyDecelResult:
    base = float(base_accel)
    if not enabled or not _finite(base, v_ego, t_follow, stop_distance, dt) or not 0.0 < dt <= MAX_DT:
      self.reset()
      return EarlyDecelResult(base, reason='off' if not enabled else 'invalid')

    confirmed = self._confirm(lead, dt)
    closing = confirmed and lead_is_closing(lead)

    # 1. anticipation cap
    target = 0.0
    if closing:
      required = required_relative_decel(lead.dRel, lead.vRel, lead.vLead, t_follow, stop_distance)
      target = anticipation_target(required, v_ego)
    if target < 0.0:
      if self.state is None:
        # Start from the MPC target (no step). If the MPC already brakes harder,
        # start at the requirement so a later MPC release cannot expose an
        # over-strong cap.
        self.state = max(base, target)
      if self.state > target:
        rate = ATTACK_JERK_THROTTLE if self.state > 0.0 else ATTACK_JERK_BRAKE
        self.state = max(target, self.state - rate * dt)
      else:
        self.state = min(target, self.state + RELEASE_JERK * dt)
      if self.state <= -ENGAGED_DECEL and self.state < base - ENGAGED_MARGIN:
        # Anticipation itself braked before the plan did: margin was gained.
        self.engaged_track = self.track_id
    else:
      self._release(base, dt)

    # 2. smooth stop: ease only an MPC target that brakes harder than needed
    relief = 0.0
    floor = None
    if (smooth_stop_enabled and relief_allowed and closing and self.engaged_track == self.track_id >= 0
        and float(lead.vLead) <= STATIONARY_LEAD_SPEED
        and RELIEF_SPEED[0] <= float(v_ego) <= RELIEF_SPEED[1]):
      floor = smooth_stop_floor(lead.dRel, v_ego, stop_distance, a_ego)
    if floor is None:
      self.relief_cap = 0.0
    else:
      speed_cap = RELIEF_MAX_DELTA * _smoothstep(float(v_ego), *RELIEF_LOW_SPEED_FADE)
      self.relief_cap = min(speed_cap, self.relief_cap + RELIEF_ENTRY_JERK * dt)
      relief = max(0.0, min(floor - base, self.relief_cap))

    eased = base + relief
    # relief <= floor - base, so an eased target is never weaker than the floor.
    accel = min(eased, self.state) if self.state is not None else eased
    reason = 'relief' if relief > 0.0 else 'anticipation' if accel < base else 'standby' if confirmed else 'unconfirmed'
    return EarlyDecelResult(float(accel), self.state, float(relief), accel != base, reason)

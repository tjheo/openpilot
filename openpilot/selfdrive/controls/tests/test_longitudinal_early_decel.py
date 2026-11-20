"""LeadEarlyDecel / LeadSmoothStop offline regressions.

The closed-loop checks use a simple point-mass car with a 0.3s actuator lag,
a LongControl-like stopping latch below 0.5m/s (hold last command, ramp to
-0.5m/s^2 at 0.8m/s^3, never unwind stronger braking; identical for legacy and
new), and two surrogate "legacy" planners that follow the MPC's stopped-lead
reference curve (soft: tracks it promptly, late: reacts later and harder).
They are NOT the acados MPC and do not prove vehicle behavior; they check that
the helper only adds earlier braking, lowers the peak when it starts earlier,
keeps at least the legacy stopping margin and leaves late/urgent cases legacy.
"""
import ast
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from openpilot.selfdrive.controls.lib import longitudinal_early_decel as ed
from openpilot.selfdrive.controls.lib.longitudinal_early_decel import (
  LeadEarlyDecelState,
  anticipation_target,
  required_relative_decel,
  smooth_stop_floor,
  stationary_stop_need,
)

ROOT = Path(__file__).resolve().parents[3]
DT = 0.05
TF = 1.45
STOP = 6.0
CB = 2.5


def lead(d, v_rel, v_lead, *, a_lead=0.0, prob=0.9, track=7, radar=True, status=True):
  return SimpleNamespace(status=status, radar=radar, radarTrackId=track, dRel=d, vRel=v_rel, vLead=v_lead,
                         aLeadK=a_lead, modelProb=prob)


def desired(v, vl):
  return v * v / (2 * CB) + TF * v + STOP - vl * vl / (2 * CB)


def legacy_soft(v, d, vl, vset, filt):
  cruise = max(-1.0, min(1.0, 0.5 * (vset - v)))
  follow = (vl - v) / (0.4 * max(v, 0.0) + 1.45) + 0.2 * (d - desired(v, vl))
  return filt + (max(-3.5, min(cruise, follow)) - filt) * DT / 0.5


def legacy_late(v, d, vl, vset, filt):
  cruise = max(-1.0, min(1.0, 0.5 * (vset - v)))
  follow = (vl - v) / (0.4 * max(v, 0.0) + 1.45) + 0.1 * (d - desired(v, vl))
  return filt + (max(-3.5, min(cruise, follow)) - filt) * DT / 0.9


def simulate(v0, d0, vl0, *, enabled, smooth=True, planner=legacy_soft, a_lead=0.0, prob=0.9, track_fn=None,
             relief_allowed=True, horizon=60.0, latch=False):
  v, a, x, xl, vl, base = v0, 0.0, 0.0, d0, vl0, 0.0
  state = LeadEarlyDecelState()
  stopping, last = False, 0.0
  rows = []
  for k in range(int(horizon / DT)):
    d = xl - x
    base = planner(v, d, vl, v0, base)
    track = track_fn(k) if track_fn else 7
    result = state.update(enabled=enabled, smooth_stop_enabled=smooth, relief_allowed=relief_allowed, dt=DT,
                          base_accel=base, v_ego=v, a_ego=a, lead=lead(d, vl - v, vl, a_lead=a_lead, prob=prob, track=track),
                          t_follow=TF, stop_distance=STOP)
    stopping = stopping or (latch and vl0 == 0.0 and v < 0.5)
    if stopping:
      cmd = last if last <= -0.5 else min(last, 0.0) - 0.8 * DT
    else:
      cmd = result.accel
    last = cmd
    rows.append(SimpleNamespace(t=k * DT, v=v, a=a, cmd=cmd, base=base, d=d, result=result, stopping=stopping))
    if v < 0.05 and cmd <= 0.0:
      v, a = 0.0, 0.0
    else:
      a += (cmd - a) * DT / 0.3
      v = max(0.0, v + a * DT)
    x += v * DT
    vl = max(0.0, vl + a_lead * DT)
    xl += vl * DT
    if v == 0.0 and vl == 0.0 and k > 20:
      rows.append(SimpleNamespace(t=(k + 1) * DT, v=v, a=a, cmd=cmd, base=base, d=xl - x, result=result,
                                  stopping=stopping))
      break
  return rows


def metrics(rows):
  moving = [r for r in rows if r.v > 0.0]
  onset = next((r.t for r in rows if r.cmd < -0.3), math.inf)
  jerk = max(abs(moving[i + 1].a - moving[i].a) / DT for i in range(len(moving) - 1))
  final = [r for r in moving if r.v < 5.0]
  final_peak = min((r.a for r in final), default=0.0)
  final_jerk = max((abs(final[i + 1].a - final[i].a) / DT for i in range(len(final) - 1)), default=0.0)
  return SimpleNamespace(onset=onset, peak=min(r.a for r in rows), jerk=jerk, final_peak=final_peak, final_jerk=final_jerk,
                         gap=rows[-1].d, min_gap=min(r.d for r in rows), v_end=rows[-1].v)


STOPPED_CASES = [(13.9, 100.0), (11.1, 80.0), (8.3, 60.0)]


@pytest.mark.parametrize("planner", [legacy_soft, legacy_late])
@pytest.mark.parametrize("v0,d0", STOPPED_CASES)
def test_stopped_lead_brakes_earlier_with_lower_peak_and_no_less_margin(planner, v0, d0):
  legacy = metrics(simulate(v0, d0, 0.0, enabled=False, planner=planner))
  new = metrics(simulate(v0, d0, 0.0, enabled=True, smooth=False, planner=planner))
  assert new.onset < legacy.onset
  assert new.peak > legacy.peak + 0.25           # lower peak deceleration
  assert new.v_end == 0.0 and legacy.v_end == 0.0
  assert new.gap >= legacy.gap - 0.02            # stop no further than legacy
  assert new.min_gap >= legacy.min_gap - 0.02


@pytest.mark.parametrize("planner", [legacy_soft, legacy_late])
@pytest.mark.parametrize("v0,d0", STOPPED_CASES)
def test_smooth_stop_lowers_final_approach_peak_without_moving_stop_point(planner, v0, d0):
  legacy = metrics(simulate(v0, d0, 0.0, enabled=False, planner=planner))
  early = metrics(simulate(v0, d0, 0.0, enabled=True, smooth=False, planner=planner))
  smooth_rows = simulate(v0, d0, 0.0, enabled=True, smooth=True, planner=planner, latch=True)
  smooth = metrics(smooth_rows)
  planner_stop = metrics(simulate(v0, d0, 0.0, enabled=True, smooth=True, planner=planner))
  assert planner_stop.gap >= legacy.gap - 0.05 and planner_stop.v_end == 0.0
  legacy_latch = metrics(simulate(v0, d0, 0.0, enabled=False, planner=planner, latch=True))
  assert smooth.final_peak >= early.final_peak - 1e-6
  assert smooth.final_peak > legacy_latch.final_peak + 0.1     # lower final-approach peak decel
  assert smooth.final_jerk <= legacy_latch.final_jerk + 0.02   # no higher final-approach jerk
  # Overall jerk is bounded by the anticipation build-up rate (<=1.0m/s^3) or legacy.
  assert smooth.jerk <= max(legacy.jerk, ed.ATTACK_JERK_BRAKE) + 0.05
  # The surrogate legacy latches short of its own target after hard braking; the
  # eased profile must still stay well outside the configured stop gap.
  assert smooth.min_gap >= STOP + 1.0
  # No creep: once braking started, speed never increases before standstill.
  started = next(i for i, r in enumerate(smooth_rows) if r.cmd < -0.3)
  speeds = [r.v for r in smooth_rows[started:]]
  assert all(r.cmd <= 0.0 for r in smooth_rows[started:])
  assert all(b <= a + 1e-9 for a, b in zip(speeds, speeds[1:], strict=False))
  # Every eased target still brakes at least the lag-compensated stop requirement.
  for r in smooth_rows:
    if r.result.relief > 0.0:
      need = stationary_stop_need(r.d, r.v, STOP, r.a)
      assert r.cmd < 0.0 and r.cmd <= -(ed.RELIEF_SAFETY_FACTOR * need + ed.RELIEF_OFFSET) + 1e-9


def test_late_detection_keeps_legacy_peak():
  # 16.7m/s with a stopped lead first seen at 70m: margin insufficient -> legacy braking dominates.
  legacy = metrics(simulate(16.7, 70.0, 0.0, enabled=False))
  new = metrics(simulate(16.7, 70.0, 0.0, enabled=True))
  assert new.peak <= legacy.peak + 0.02
  assert new.gap >= legacy.gap - 0.02


@pytest.mark.parametrize("v0,vl,d0", [(16.7, 8.3, 90.0), (22.2, 11.1, 110.0), (25.0, 15.0, 110.0)])
def test_slower_moving_lead_earlier_mild_decel_never_closer(v0, vl, d0):
  legacy_rows = simulate(v0, d0, vl, enabled=False, horizon=40.0)
  new_rows = simulate(v0, d0, vl, enabled=True, horizon=40.0)
  legacy, new = metrics(legacy_rows), metrics(new_rows)
  assert new.onset <= legacy.onset
  assert new.peak >= legacy.peak - 1e-6
  assert new.min_gap >= legacy.min_gap - 0.02
  assert new.min_gap >= TF * vl + STOP - 0.5
  assert all(r.cmd <= r.base + 1e-12 for r in new_rows)   # never weaker than the plan (no relief for moving leads)
  assert all(r.result.relief == 0.0 for r in new_rows)


def run_sequence(leads, bases, *, enabled=True, smooth=True, relief_allowed=True, v_ego=15.0, a_ego=0.0, state=None):
  state = state or LeadEarlyDecelState()
  return state, [state.update(enabled=enabled, smooth_stop_enabled=smooth, relief_allowed=relief_allowed, dt=DT,
                              base_accel=b, v_ego=v_ego, a_ego=a_ego, lead=ld, t_follow=TF, stop_distance=STOP)
                 for ld, b in zip(leads, bases, strict=True)]


@pytest.mark.parametrize("ld", [
  lead(60.0, 2.0, 17.0),                       # pulling away
  lead(60.0, 0.0, 15.0),                       # matched speed
  lead(60.0, -0.3, 14.7),                      # closing below the deadband
  lead(60.0, -3.0, 12.0, a_lead=2.5),          # strongly accelerating lead: will pull away
])
def test_non_closing_or_pulling_away_lead_adds_no_decel(ld):
  _, results = run_sequence([ld] * 60, [0.4] * 60)
  assert all(r.accel == 0.4 and not r.active for r in results)


def test_release_when_lead_starts_pulling_away_never_adds_decel():
  state, results = run_sequence([lead(50.0, -8.0, 7.0)] * 60, [0.2] * 60)
  assert results[-1].accel < -0.5
  _, released = run_sequence([lead(50.0, 1.0, 16.0)] * 40, [0.2] * 40, state=state)
  values = [results[-1].accel] + [r.accel for r in released]
  assert all(b >= a - 1e-12 for a, b in zip(values, values[1:], strict=False))
  assert released[-1].accel == 0.2 and state.state is None
  assert max(b - a for a, b in zip(values, values[1:], strict=False)) <= ed.RELEASE_JERK * DT + 1e-9


@pytest.mark.parametrize("ld", [
  lead(50.0, -8.0, 7.0, prob=0.3),             # low vision probability
  lead(50.0, -15.0, 0.0, prob=0.6),            # stopped lead needs 0.7
  lead(50.0, -8.0, 7.0, radar=False),          # vision-only
  lead(50.0, -8.0, 7.0, track=-1),             # no radar track
  lead(50.0, -8.0, 7.0, status=False),
  lead(130.0, -10.0, 5.0),                     # beyond moving range
  lead(105.0, -15.0, 0.0),                     # beyond stationary range
  lead(math.nan, -8.0, 7.0),
  lead(50.0, math.inf, 7.0),
])
def test_unconfirmed_noisy_or_far_lead_adds_nothing(ld):
  _, results = run_sequence([ld] * 60, [0.3] * 60)
  assert all(r.accel == 0.3 and not r.active for r in results)


def test_flickering_track_never_confirms():
  leads = [lead(50.0, -8.0, 7.0, track=7 + (i // 8) % 2) for i in range(80)]  # id switches every 0.4s
  _, results = run_sequence(leads, [0.3] * 80)
  assert all(r.accel == 0.3 for r in results)


def test_cutin_first_half_second_and_urgent_braking_are_identical_to_legacy():
  # New close track: unchanged during confirmation even though kinematics are urgent.
  leads = [lead(15.0, -6.0, 9.0, track=31)] * 9
  bases = [-0.5 - 0.3 * i for i in range(9)]
  _, results = run_sequence(leads, bases)
  assert [r.accel for r in results] == bases
  # Confirmed lead, MPC already braking harder than the mild cap: exactly the MPC value.
  state, _ = run_sequence([lead(40.0, -10.0, 5.0)] * 20, [-3.0] * 20)
  _, urgent = run_sequence([lead(40.0, -10.0, 5.0)] * 20, [-3.5] * 20, state=state)
  assert all(r.accel == -3.5 for r in urgent)


def test_anticipation_is_bounded_mild_and_jerk_limited():
  state, results = run_sequence([lead(30.0, -15.0, 0.0)] * 80, [0.5] * 80)
  values = [0.5] + [r.accel for r in results]
  assert min(values) >= -ed.MAX_ANTICIPATION_DECEL - 1e-9
  steps = [a - b for a, b in zip(values, values[1:], strict=False)]
  assert max(steps) <= max(ed.ATTACK_JERK_BRAKE, ed.ATTACK_JERK_THROTTLE) * DT + 1e-9
  assert results[-1].accel == pytest.approx(-ed.MAX_ANTICIPATION_DECEL)


def test_requirement_helpers():
  assert required_relative_decel(106.0, -20.0, 0.0, TF, STOP) == pytest.approx(2.0)
  assert required_relative_decel(50.0, 1.0, 15.0, TF, STOP) == 0.0
  assert math.isinf(required_relative_decel(6.5, -5.0, 0.0, TF, STOP))
  assert anticipation_target(0.2, 15.0) == 0.0
  assert anticipation_target(1.0, 15.0) == pytest.approx(-1.15)
  assert anticipation_target(1.0, 1.0) == 0.0          # below the low-speed fade
  assert anticipation_target(math.inf, 15.0) == -ed.MAX_ANTICIPATION_DECEL
  assert smooth_stop_floor(8.0, 10.0, STOP) is None     # no margin
  floor = smooth_stop_floor(40.0, 6.0, STOP, -1.0)
  assert floor is not None and floor <= -ed.RELIEF_MIN_FLOOR
  assert stationary_stop_need(40.0, 6.0, STOP, -1.0) <= stationary_stop_need(40.0, 6.0, STOP, 0.0)
  assert stationary_stop_need(40.0, 6.0, STOP, -9.0) == stationary_stop_need(40.0, 6.0, STOP, -ed.RELIEF_MAX_NEED)


def _engaged_state():
  state, results = run_sequence([lead(45.0, -10.0, 0.0)] * 40, [-0.2] * 40, v_ego=10.0)
  assert state.engaged_track == 7
  return state


def test_smooth_stop_requires_prior_early_braking_and_permission():
  ld = lead(30.0, -6.0, 0.0)
  # Without prior anticipation on this track: no easing.
  state = LeadEarlyDecelState()
  state.track_id, state.confirm_time = 7, 1.0
  _, results = run_sequence([ld] * 10, [-2.5] * 10, v_ego=6.0, a_ego=-1.0, state=state)
  assert all(r.relief == 0.0 for r in results)
  # With prior anticipation: eased, bounded, ramped.
  _, results = run_sequence([ld] * 20, [-2.5] * 20, v_ego=6.0, a_ego=-1.0, state=_engaged_state())
  assert results[-1].relief > 0.0 and results[-1].accel < 0.0
  assert results[0].relief <= ed.RELIEF_ENTRY_JERK * DT + 1e-9
  assert all(r.relief <= ed.RELIEF_MAX_DELTA + 1e-9 for r in results)
  # Permission withdrawn (cut-in risk / other obstacle binding / FCW): legacy immediately.
  for kwargs in ({"relief_allowed": False}, {"smooth": False}):
    state = _engaged_state()
    run_sequence([ld] * 20, [-2.5] * 20, v_ego=6.0, a_ego=-1.0, state=state)
    _, blocked = run_sequence([ld], [-2.5], v_ego=6.0, a_ego=-1.0, state=state, **kwargs)
    assert blocked[0].relief == 0.0 and blocked[0].accel <= -2.5


def test_smooth_stop_not_for_moving_lead_or_late_need():
  _, results = run_sequence([lead(30.0, -6.0, 3.0)] * 20, [-2.5] * 20, v_ego=9.0, state=_engaged_state())
  assert all(r.relief == 0.0 for r in results)
  _, results = run_sequence([lead(15.0, -12.0, 0.0)] * 20, [-3.0] * 20, v_ego=12.0, state=_engaged_state())
  assert all(r.relief == 0.0 and r.accel == -3.0 for r in results)


@pytest.mark.parametrize("dt", [0.0, -0.05, 0.5, math.nan])
def test_invalid_interval_resets_to_plan(dt):
  state = _engaged_state()
  result = state.update(enabled=True, smooth_stop_enabled=True, relief_allowed=True, dt=dt, base_accel=0.3,
                        v_ego=10.0, a_ego=0.0, lead=lead(45.0, -10.0, 0.0), t_follow=TF, stop_distance=STOP)
  assert result.accel == 0.3 and state.state is None and state.engaged_track == -1


def test_off_reproduces_legacy_exactly():
  for planner in (legacy_soft, legacy_late):
    for v0, d0 in STOPPED_CASES:
      rows = simulate(v0, d0, 0.0, enabled=False, planner=planner)
      assert all(r.cmd == r.base and not r.result.active for r in rows)
      latched = simulate(v0, d0, 0.0, enabled=False, planner=planner, latch=True)
      assert all(r.cmd == r.base and not r.result.active for r in latched if not r.stopping)
  _, results = run_sequence([lead(30.0, -15.0, 0.0)] * 30, [0.5 - 0.1 * i for i in range(30)], enabled=False)
  assert [r.accel for r in results] == [0.5 - 0.1 * i for i in range(30)]


# --- production wiring -------------------------------------------------------

PLANNER = ROOT / "selfdrive" / "controls" / "lib" / "longitudinal_planner.py"


@pytest.fixture
def planner_method():
  tree = ast.parse(PLANNER.read_text(encoding="utf-8"))
  fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "apply_lead_early_decel")
  namespace = {"time": __import__("time")}
  exec(compile(ast.Module([fn], []), str(PLANNER), "exec"), namespace)
  return namespace["apply_lead_early_decel"]


def fake_planner(**overrides):
  values = dict(dt=DT, lead_early_decel=LeadEarlyDecelState(), lead_early_decel_time=None, lead_early_decel_result=None,
                tesla_traffic_decision=None, tesla_traffic_blended=False, fcw=False, output_should_stop=False,
                mpc=SimpleNamespace(mode='acc', crash_cnt=0, source='lead0', solution_status=0, t_follow=TF))
  values.update(overrides)
  return SimpleNamespace(**values)


def fake_inputs(*, enabled=True, gas=False, brake=False, standstill=False, soft_hold=0, lane_change=False, ld=None):
  cs = SimpleNamespace(gasPressed=gas, brakePressed=brake, standstill=standstill, vEgo=12.0, aEgo=0.0)
  sm = {'carState': cs, 'radarState': SimpleNamespace(leadOne=ld or lead(60.0, -12.0, 0.0))}
  carrot = SimpleNamespace(leadEarlyDecel=enabled, leadSmoothStop=True, soft_hold_active=soft_hold,
                           lane_change_active=lane_change, stop_distance=STOP)
  return sm, carrot


def drive(method, planner, sm, carrot, frames=40, base=0.4, cutin=None):
  out = None
  for i in range(frames):
    out = method(planner, sm, carrot, base, 12.0, False, False, cutin, now=i * DT)
  return out


def test_planner_wiring_applies_when_enabled(planner_method):
  planner = fake_planner()
  sm, carrot = fake_inputs()
  a, v = drive(planner_method, planner, sm, carrot)
  assert a < 0.0 and v <= 12.0


@pytest.mark.parametrize("blocked", [
  {"enabled": False}, {"gas": True}, {"brake": True}, {"standstill": True}, {"soft_hold": 1}, {"lane_change": True},
])
def test_planner_inhibits_return_inputs_unchanged(planner_method, blocked):
  planner = fake_planner()
  sm, carrot = fake_inputs(**blocked)
  assert drive(planner_method, planner, sm, carrot) == (0.4, 12.0)


@pytest.mark.parametrize("override", [
  {"fcw": True}, {"output_should_stop": True}, {"tesla_traffic_blended": True},
  {"tesla_traffic_decision": SimpleNamespace(active=True)},
  {"mpc": SimpleNamespace(mode='blended', crash_cnt=0, source='lead0', solution_status=0, t_follow=TF)},
  {"mpc": SimpleNamespace(mode='acc', crash_cnt=3, source='lead0', solution_status=0, t_follow=TF)},
])
def test_planner_emergency_and_mode_inhibits_are_legacy(planner_method, override):
  planner = fake_planner(**override)
  sm, carrot = fake_inputs()
  assert drive(planner_method, planner, sm, carrot) == (0.4, 12.0)


def test_planner_relief_requires_lead0_converged_and_no_cutin(planner_method):
  ld = lead(30.0, -6.0, 0.0)
  for override, cutin in (({"source": "lead1"}, None), ({"source": "e2e"}, None), ({"solution_status": 4}, None),
                          ({}, -0.4)):
    mpc = dict(mode='acc', crash_cnt=0, source='lead0', solution_status=0, t_follow=TF)
    mpc.update(override)
    planner = fake_planner(mpc=SimpleNamespace(**mpc))
    planner.lead_early_decel = _engaged_state()
    sm, carrot = fake_inputs(ld=ld)
    sm['carState'].vEgo, sm['carState'].aEgo = 6.0, -1.0
    a, _ = drive(planner_method, planner, sm, carrot, frames=20, base=-2.5, cutin=cutin)
    assert a <= -2.5


def test_planner_update_calls_hook_after_target_selection():
  source = PLANNER.read_text(encoding="utf-8")
  call = source.index("output_a_target, output_v_target_now = self.apply_lead_early_decel(")
  assert source.index("self.output_should_stop = output_should_stop_e2e or output_should_stop_mpc") < call
  assert call < source.index("self.output_a_target = output_a_target\n")


def test_params_catalog_and_carrot_wiring():
  keys = (ROOT / "common" / "params_keys.h").read_text(encoding="utf-8")
  for name in ("LeadEarlyDecel", "LeadSmoothStop"):
    assert f'{{"{name}", {{PERSISTENT, BOOL, "1"}}}}' in keys
  functions = (ROOT / "selfdrive" / "carrot" / "carrot_functions.py").read_text(encoding="utf-8")
  assert functions.count('self.leadEarlyDecel = self.params.get_bool("LeadEarlyDecel")') == 2
  assert functions.count('self.leadSmoothStop = self.params.get_bool("LeadSmoothStop")') == 2
  catalog = json.loads((ROOT / "selfdrive" / "carrot_settings.json").read_text(encoding="utf-8"))
  entries = {e["name"]: e for e in catalog["params"] if isinstance(e, dict) and "name" in e} if "params" in catalog else None
  if entries is None:
    found = {}

    def walk(node):
      if isinstance(node, dict):
        if node.get("name") in ("LeadEarlyDecel", "LeadSmoothStop") and "default" in node:
          found[node["name"]] = node
        for value in node.values():
          walk(value)
      elif isinstance(node, list):
        for value in node:
          walk(value)
    walk(catalog)
    entries = found
  for name in ("LeadEarlyDecel", "LeadSmoothStop"):
    assert entries[name]["default"] == 1 and entries[name]["control"] == "toggle"


def test_untouched_safety_modules_do_not_reference_feature():
  for rel in ("selfdrive/controls/lib/longcontrol.py", "selfdrive/controls/lib/longitudinal_stopping_lead.py",
              "selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py"):
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert "early_decel" not in text and "LeadSmoothStop" not in text

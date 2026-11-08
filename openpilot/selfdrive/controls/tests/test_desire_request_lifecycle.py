"""Regression tests through full imported production request/FSM dependency graph."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from openpilot.selfdrive.controls.tests.production_loader import DesireHelper, car, log, custom
from openpilot.selfdrive.carrot.bluetooth.model import CommandReader, atomic_json
from openpilot.selfdrive.controls.lib.desire_lib.assistant_lane import AssistantLane
from openpilot.selfdrive.controls.lib.desire_lib.constants import LaneChangeState

class Production(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.root = Path(self.tmp.name)
    self.now = 100.
    self.frame = 0
    self.h = DesireHelper()
    self.h.bluetooth_commands = CommandReader('lane', self.root)
    self.h.bluetooth_commands.started = 99.
    self.h.assistant_lane = AssistantLane(self.root/'voice', clock=lambda:self.now)
    self.h.assistant_lane.started = 99.
    self.c = car.CarState.new_message()
    self.c.canValid = True
    self.c.gearShifter = 'drive'
    self.c.vEgo = 5.
    self.c.leftBlinker = True
    self.c.steeringAngleDeg = 60.
    self.c.vehicleSensorsInvalid = False
    self.c.turnSignalDriverValid = True
    self.m = log.ModelDataV2.new_message()
    self.m.laneLines = [dict(x=[float(i) for i in range(33)],y=[y]*33,z=[0.]*33,t=[i*.05 for i in range(33)]) for y in (-5.2,-1.7,1.7,5.2)]
    self.m.laneLineProbs = [1.]*4
    self.m.roadEdges = [dict(x=[float(i) for i in range(33)],y=[y]*33,z=[0.]*33,t=[i*.05 for i in range(33)]) for y in (-7.,7.)]
    self.m.meta.desireState = [0.]*8
    self.cm = custom.CarrotMan.new_message()
    self.cm.atcType = 'turn left'
    self.r = log.RadarState.new_message()
    self.r.leadsLeft = []
    self.r.leadsRight = []
  def tearDown(self): self.tmp.cleanup()
  def step(self, *, angle=None, rates=.2, valid=True, frozen=False, clock=True, active=True, eof_age=.001, missing=False):
    self.now += .05
    if not frozen: self.frame += 1
    if angle is not None: self.c.steeringAngleDeg = angle
    self.m.frameId = self.frame
    if not frozen: self.m.timestampEof = int((self.now-eof_age)*1e9)
    self.m.orientationRate.z = [] if missing else [rates]*33
    self.m.orientation.z = [rates*i*.01 for i in range(33)]
    with patch('time.monotonic', return_value=self.now):
      return self.h.update(self.c,self.m,active,0.,self.cm,self.r,indicator_valid=valid,assistant_valid=valid,lifecycle_now=self.now if clock else None)
  def initiate(self):
    for _ in range(12): self.step()
    self.assertGreaterEqual(self.h.turn_lifecycle.started,.3)
  def command(self, side, idx):
    self.c.leftBlinker = self.c.rightBlinker = False
    self.cm.carrotCmd = 'LANECHANGE'
    self.cm.carrotArg = side
    self.cm.carrotCmdIndex = idx
  def test_normal_exit(self):
    self.initiate()
    for _ in range(25): self.step(angle=10,rates=.01)
    self.assertTrue(self.h.turn_lifecycle.completed)
    self.assertEqual(self.h.desire,log.Desire.none)
    for _ in range(40): self.step(angle=10,rates=.01)
    self.assertEqual(self.h.desire,log.Desire.none)
  def test_long_turn_not_old_ten_second_timeout(self):
    self.initiate()
    for _ in range(260): self.step()
    self.assertFalse(self.h.turn_lifecycle.cancelled)
    self.assertEqual(self.h.desire,log.Desire.turnLeft)
    for _ in range(400): self.step()
    self.assertEqual(self.h.turn_lifecycle.reason,'deadline')
  def test_frozen_stale_missing_then_new_initiation(self):
    for kw in ({'frozen':True},{'valid':False},{'clock':False},{'eof_age':1.},{'missing':True}):
      self.tearDown(); self.setUp()
      self.initiate()
      for _ in range(25): self.step(angle=10,rates=.01,**kw)
      self.assertFalse(self.h.turn_lifecycle.completed)
      self.assertEqual(self.h.desire,log.Desire.none)
  def test_driver_override_and_same_side_stalk_rearm(self):
    self.initiate()
    self.c.steeringPressed=True; self.c.steeringTorque=-1.
    self.step()
    self.assertTrue(self.h.turn_lifecycle.cancelled)
    self.c.steeringPressed=False
    self.step()
    self.assertEqual(self.h.desire,log.Desire.none)
    self.c.leftBlinkerStalkCount += 1
    self.step()
    self.assertEqual(self.h.desire,log.Desire.turnLeft)
  def test_owned_opposite_request_cancels_turn_without_side_latch(self):
    self.initiate(); self.command('RIGHT',1)
    self.step(angle=-60)
    self.assertEqual(self.h.request_diagnostic,'opposite_owned_request')
    self.assertEqual(self.h.desire,log.Desire.none)
    self.step(angle=-60)
    self.assertEqual(self.h.request_diagnostic,'rejected_owned_episode')
    self.assertEqual(self.h.desire,log.Desire.none)
    self.command('RIGHT',2)
    self.step(angle=-60)
    self.assertEqual(self.h.resolved_request.episode,('app',2))
    self.assertIn(self.h.request_diagnostic,('accepted_owned','accepted_observed'))
    for _ in range(6): self.step(angle=-60)
    self.command('RIGHT',3); self.step(angle=-60)
    self.assertEqual(self.h.request_diagnostic,'opposite_owned_request')
    self.step(angle=-60)
    self.assertEqual(self.h.request_diagnostic,'rejected_owned_episode')
    self.command('RIGHT',4); self.step(angle=-60)
    self.assertEqual(self.h.resolved_request.episode,('app',4))
  def test_navigation_identityless_no_indefinite_latch(self):
    self.initiate(); self.c.leftBlinker=False; self.cm.atcType='turn right'
    self.step(angle=-60)
    self.assertEqual(self.h.request_diagnostic,'opposite_identityless_observation')
    self.assertIsNone(self.h._rejected_auto_request)
    self.step(angle=-60)
    self.assertEqual(self.h.request_diagnostic,'accepted_observed')
    self.assertFalse(self.h.resolved_request.owned)
  def test_genuine_driver_reverse_allowed(self):
    self.initiate(); self.c.leftBlinker=False; self.c.rightBlinker=True
    self.step(angle=-60)
    self.assertEqual(self.h.request_diagnostic,'driver_precedence')
    self.assertEqual(self.h.desire,log.Desire.turnRight)
  def test_opposite_lane_change_curved_road_not_curvature_guard(self):
    self.c.vEgo=20.; self.c.steeringAngleDeg=-50.; self.cm.atcType='none'
    for _ in range(20): self.step(rates=-.2)
    self.assertIn(self.h.lane_change_state,(LaneChangeState.laneChangeStarting,LaneChangeState.laneChangeFinishing))
    self.assertEqual(self.h.desire,log.Desire.laneChangeLeft)
  def test_executing_lc_opposite_app_rejected_keeps_direction(self):
    self.c.vEgo=20.; self.cm.atcType='none'
    for _ in range(20): self.step()
    self.command('RIGHT',1); self.step()
    self.assertEqual(self.h.request_diagnostic,'opposite_owned_request')
    self.assertEqual(self.h.desire,log.Desire.laneChangeLeft)
    self.c.steeringPressed=True; self.c.steeringTorque=-1.
    self.c.leftBlinker=True
    self.step()
    self.assertEqual(self.h.desire,log.Desire.none)
  def test_bluetooth_event_identity_is_accepted_not_last_seen(self):
    self.c.leftBlinker=False; self.c.vEgo=20.; self.cm.atcType='none'
    for _ in range(20): self.step()
    atomic_json(self.root/'lane.json', {'events':[{'id':'good','time':self.now,'action':'laneLeft'}, {'id':'bad','time':0.,'action':'laneRight'}]})
    self.step()
    self.assertEqual(self.h.resolved_request.episode,('bluetooth','good'))
    self.assertTrue(self.h.resolved_request.owned)
  def test_voice_identity_from_selected_resolver_and_cancellation(self):
    self.c.vEgo=20.; self.c.leftBlinker=False; self.cm.atcType='none'
    for _ in range(20): self.step()
    atomic_json(self.root/'voice'/'request.json', {'id':'A'*43,'time':self.now,'direction':'left'})
    for _ in range(8): self.step()
    self.assertEqual(self.h.request_guard.owner.origin,'voice')
    self.assertEqual(self.h.request_guard.owner.episode,('voice','A'*43))
    atomic_json(self.root/'voice'/'cancel.json', {'id':'A'*43})
    self.step()
    self.assertIsNone(self.h.assistant_lane.active)
    self.assertEqual(self.h.assistant_lane.result['reason'],'permission_revoked')
  def test_stopped_turn_no_false_completion_then_normal_exit(self):
    self.initiate(); self.c.vEgo=0.
    for _ in range(200): self.step(angle=0,rates=0.)
    self.assertFalse(self.h.turn_lifecycle.completed)
    self.assertEqual(self.h.turn_lifecycle.reason,'stopped')
    self.c.vEgo=5.
    for _ in range(25): self.step(angle=10,rates=.01)
    self.assertTrue(self.h.turn_lifecycle.completed)
  def test_owned_same_side_source_replacement_rearms_completed_episode(self):
    self.initiate()
    for _ in range(25): self.step(angle=10,rates=.01)
    self.assertTrue(self.h.turn_lifecycle.completed)
    self.command('LEFT',10); self.step(angle=60)
    self.assertEqual(self.h.resolved_request.episode,('app',10))
    self.assertFalse(self.h.turn_lifecycle.completed)
    self.assertEqual(self.h.desire,log.Desire.turnLeft)
  def test_waiting_voice_cancel_does_not_reappear_as_app_pulse(self):
    self.c.vEgo=20.; self.c.leftBlinker=False; self.cm.atcType='none'
    for _ in range(20): self.step()
    self.h.laneChangeNeedTorque=1
    atomic_json(self.root/'voice'/'request.json', {'id':'B'*43,'time':self.now,'direction':'left'})
    self.step()
    self.assertEqual(self.h.lane_change_state,LaneChangeState.preLaneChange)
    atomic_json(self.root/'voice'/'cancel.json', {'id':'B'*43})
    self.step()
    self.assertIsNone(self.h.assistant_lane.active)
    self.assertEqual(self.h.carrot_lane_change_count,0)
    self.assertEqual(self.h.lane_change_state,LaneChangeState.off)
    self.assertEqual(self.h.desire,log.Desire.none)
  def test_tesla_pending_opposite_request_exposed_without_replacing_owned_signal(self):
    from openpilot.selfdrive.controls.lib.desire_lib.request import ManeuverRequest
    self.c.vEgo=20.; self.c.leftBlinker=False
    self.h.tesla_auto_signals=True
    self.h.lane_change_state=LaneChangeState.laneChangeStarting
    self.h.lane_change_direction=log.LaneChangeDirection.left
    self.h._auto_app_direction=1
    self.h._app_request=ManeuverRequest(1,'app',('app',20),True)
    self.h._auto_app_deadline=1000.
    self.command('RIGHT',21)
    self.step()
    self.assertEqual(self.h.request_diagnostic,'opposite_owned_request')
    self.assertEqual(self.h._app_request.episode,('app',20))
    self.assertEqual(self.h.lane_change_direction,log.LaneChangeDirection.left)
  def test_safety_interlocks_blindspot_trailer_and_inactive(self):
    for mode in ('bsd','trailer','inactive'):
      self.tearDown(); self.setUp()
      self.c.vEgo=20.; self.c.leftBlinker=False; self.cm.atcType='none'
      for _ in range(20): self.step()
      if mode=='bsd': self.c.leftBlindspot=True; self.h.laneChangeBsd=1
      if mode=='trailer': self.c.trailerConnected=True
      self.command('LEFT',30)
      for _ in range(5): self.step(active=mode!='inactive')
      self.assertNotIn(self.h.lane_change_state,(LaneChangeState.laneChangeStarting,LaneChangeState.laneChangeFinishing))
  def test_stale_data_deadline_and_recovery_cannot_rearm_same_episode(self):
    self.initiate()
    for _ in range(650): self.step(valid=False)
    self.assertTrue(self.h.turn_lifecycle.cancelled)
    self.assertEqual(self.h.turn_lifecycle.reason,'deadline')
    for _ in range(12): self.step()
    self.assertEqual(self.h.desire,log.Desire.none)
    self.c.leftBlinkerStalkCount += 1
    self.step()
    self.assertEqual(self.h.desire,log.Desire.turnLeft)

  def test_actual_bluetooth_reused_id_is_not_new_request(self):
    self.c.leftBlinker=False; self.c.vEgo=20.; self.cm.atcType='none'
    for _ in range(20): self.step()
    atomic_json(self.root/'lane.json', {'id':'once','time':self.now,'action':'laneLeft'})
    self.step()
    self.assertEqual(self.h.resolved_request.episode,('bluetooth','once'))
    for _ in range(5): self.step()
    self.assertIsNone(self.h.bluetooth_commands.accepted_id)
    atomic_json(self.root/'lane.json', {'id':'once','time':self.now,'action':'laneRight'})
    self.step()
    self.assertIsNone(self.h.bluetooth_commands.accepted_id)
    self.assertNotEqual(self.h.request_diagnostic,'opposite_owned_request')

  def test_running_voice_keeps_owner_when_same_side_navigation_resolves(self):
    self.c.leftBlinker=False; self.c.vEgo=20.; self.cm.atcType='none'
    for _ in range(20): self.step()
    atomic_json(self.root/'voice'/'request.json', {'id':'C'*43,'time':self.now,'direction':'left'})
    for _ in range(8): self.step()
    self.assertTrue(self.h.assistant_lane.active['began'])
    self.cm.atcType='fork left'
    for _ in range(8): self.step()
    self.assertEqual(self.h.resolved_request.origin,'navigation')
    self.assertEqual(self.h.request_guard.owner.episode,('voice','C'*43))

  def test_driver_precedes_fresh_opposite_app_and_bluetooth(self):
    self.initiate()
    self.cm.carrotCmd='LANECHANGE'; self.cm.carrotArg='RIGHT'; self.cm.carrotCmdIndex=80
    self.step()
    self.assertEqual(self.h.resolved_request.origin,'driver')
    self.assertEqual(self.h.desire,log.Desire.turnLeft)
    self.c.vEgo=20.
    atomic_json(self.root/'lane.json', {'id':'driver-wins','time':self.now,'action':'laneRight'})
    self.step()
    self.assertEqual(self.h.resolved_request.origin,'driver')
    self.assertEqual(self.h.request_diagnostic,'driver_precedence')
    self.assertIsNone(self.h.request_guard.rejected)

  def held_driver(self, direction):
    self.c.leftBlinker = direction == 1
    self.c.rightBlinker = direction == 2
    self.c.vEgo = 0.
    self.c.steeringAngleDeg = 0.
    self.cm.atcType = 'none'
    self.m.meta.desireState = [0., .2 if direction == 1 else 0., .2 if direction == 2 else 0., 0., 0., 0., 0., 0.]
    return log.Desire.turnLeft if direction == 1 else log.Desire.turnRight

  def wait_and_depart(self, direction, seconds, *, late=False):
    expected = self.held_driver(direction)
    for _ in range(int(seconds / .05)):
      self.step(active=not late)
    if not late:
      self.assertEqual(self.h.desire, expected)
      self.assertIsNone(self.h.turn_lifecycle.execution_since)
      self.assertFalse(self.h.turn_lifecycle.cancelled)
    self.c.vEgo = 5.
    for _ in range(12): self.step(angle=60. if direction == 1 else -60.)
    self.assertEqual(self.h.desire, expected)
    self.assertEqual(self.h.resolved_request.origin, 'driver')
    self.assertIsNotNone(self.h.turn_lifecycle.execution_since)
    self.assertGreater(self.h.turn_lifecycle.execution_since, 100. + seconds)

  def test_left_held_lamp_33_seconds_then_departure(self):
    self.wait_and_depart(1, 33.)

  def test_right_held_lamp_33_seconds_then_departure(self):
    self.wait_and_depart(2, 33.)

  def test_left_held_lamp_61_seconds_then_departure(self):
    self.wait_and_depart(1, 61.)

  def test_right_held_lamp_61_seconds_then_departure(self):
    self.wait_and_depart(2, 61.)

  def test_left_held_lamp_121_seconds_then_departure(self):
    self.wait_and_depart(1, 121.)

  def test_right_held_lamp_121_seconds_then_departure(self):
    self.wait_and_depart(2, 121.)

  def test_lateral_activation_late_in_held_lamp_wait(self):
    self.held_driver(1)
    for _ in range(1300): self.step(active=False)
    # Activation itself is not initiation; an additional >30 s active wait
    # must retain the same physical signal with no new stalk press.
    for _ in range(660): self.step()
    self.assertIsNone(self.h.turn_lifecycle.execution_since)
    self.assertEqual(self.h.desire, log.Desire.turnLeft)
    self.c.vEgo = 5.
    self.initiate_with_angle()
    self.assertEqual(self.h.desire, log.Desire.turnLeft)

  def test_initiated_stopped_turn_deadline_is_not_paused(self):
    self.initiate()
    start = self.h.turn_lifecycle.execution_since
    self.c.vEgo = 0.
    for _ in range(650): self.step(angle=0., rates=0.)
    self.assertFalse(self.h.turn_lifecycle.completed)
    self.assertEqual(self.h.turn_lifecycle.reason, 'deadline')
    self.assertEqual(self.h.turn_lifecycle.execution_since, start)
    self.c.vEgo = 5.
    for _ in range(12): self.step(angle=60.)
    self.assertEqual(self.h.desire, log.Desire.none)

  def test_preinitiation_stale_missing_recovery_keeps_freshness_gate(self):
    self.held_driver(1)
    for _ in range(660): self.step()
    episode = self.h.turn_lifecycle.episode
    for kw in ({'frozen':True}, {'missing':True}, {'valid':False}, {'clock':False}, {'eof_age':1.}):
      for _ in range(12): self.step(**kw)
      self.assertEqual(self.h.desire, log.Desire.none)
      self.assertFalse(self.h.turn_lifecycle.completed)
      self.assertFalse(self.h.turn_lifecycle.cancelled)
      self.assertIsNone(self.h.turn_lifecycle.execution_since)
      # Missing clock leaves last_now unchanged; first recovery after its
      # >350 ms gap is still suppressed. Only the next fresh interval resumes.
      self.step()
      self.step()
      self.assertEqual(self.h.desire, log.Desire.turnLeft)
      self.assertEqual(self.h.turn_lifecycle.episode, episode)
    self.c.vEgo = 5.
    self.initiate_with_angle()

  def test_initiated_missing_stale_recovery_never_restarts_deadline(self):
    self.initiate()
    start = self.h.turn_lifecycle.execution_since
    for kw in ({'frozen':True}, {'missing':True}, {'valid':False}):
      for _ in range(12): self.step(**kw)
      self.assertEqual(self.h.desire, log.Desire.none)
      self.assertEqual(self.h.turn_lifecycle.started, 0.)
      self.assertEqual(self.h.turn_lifecycle.execution_since, start)
      self.initiate()
      self.assertEqual(self.h.turn_lifecycle.execution_since, start)
    for _ in range(650): self.step(missing=True)
    self.assertEqual(self.h.turn_lifecycle.reason, 'deadline')
    for _ in range(12): self.step()
    self.assertEqual(self.h.desire, log.Desire.none)

  def test_automatic_preparation_keeps_separate_request_deadline(self):
    self.c.leftBlinker = False
    self.c.vEgo = 0.
    self.c.steeringAngleDeg = 0.
    for _ in range(660): self.step()
    self.assertEqual(self.h.resolved_request.origin, 'navigation')
    self.assertIsNone(self.h.turn_lifecycle.execution_since)
    self.assertEqual(self.h.turn_lifecycle.reason, 'deadline')
    self.assertEqual(self.h.desire, log.Desire.none)

  def test_automatic_execution_does_not_extend_preexisting_request_cap(self):
    self.c.leftBlinker = False
    self.c.vEgo = 0.
    self.c.steeringAngleDeg = 0.
    for _ in range(500): self.step()
    self.c.vEgo = 5.
    self.initiate_with_angle()
    self.assertIsNotNone(self.h.turn_lifecycle.execution_since)
    for _ in range(120): self.step()
    self.assertEqual(self.h.turn_lifecycle.reason, 'deadline')

  def test_waiting_driver_cancel_and_replacement(self):
    self.held_driver(1)
    for _ in range(660): self.step()
    self.c.steeringPressed = True
    self.c.steeringTorque = -1.
    self.step()
    self.assertTrue(self.h.turn_lifecycle.cancelled)
    self.c.steeringPressed = False
    for _ in range(12): self.step()
    self.assertEqual(self.h.desire, log.Desire.none)
    self.c.leftBlinkerStalkCount += 1
    self.step()
    self.assertEqual(self.h.desire, log.Desire.turnLeft)
    self.assertIsNone(self.h.turn_lifecycle.execution_since)

  def test_stationary_steering_or_moving_straight_is_not_initiation(self):
    self.held_driver(1)
    for _ in range(660): self.step(angle=60.)
    self.assertIsNone(self.h.turn_lifecycle.execution_since)
    self.c.vEgo = 5.
    for _ in range(660): self.step(angle=0.)
    self.assertIsNone(self.h.turn_lifecycle.execution_since)
    self.assertEqual(self.h.desire, log.Desire.turnLeft)
    self.initiate_with_angle()

  def initiate_with_angle(self):
    for _ in range(12): self.step(angle=60.)
    self.assertIsNotNone(self.h.turn_lifecycle.execution_since)

  def test_reset_boundaries(self):
    for mode in ('trailer','lateral','disable','withdraw'):
      self.tearDown(); self.setUp(); self.initiate()
      if mode=='trailer': self.c.trailerConnected=True
      if mode=='disable': self.h.desire_disable_count=3
      if mode=='withdraw': self.c.leftBlinker=False; self.cm.atcType='none'
      self.step(active=mode!='lateral')
      self.assertEqual(self.h.desire,log.Desire.none)

if __name__=='__main__': unittest.main(verbosity=2)

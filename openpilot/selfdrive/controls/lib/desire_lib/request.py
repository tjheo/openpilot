"""Observed request provenance, not authentication or road-curvature intent.

An app command index / consumed Bluetooth event / AssistantLane ID owns an
input episode. Navigation only exposes a cue: its local observation generation
is explicitly NOT an external maneuver ID. Driver lamps alone have the same
limitation. Neither direction nor output lamp IDs identify a command.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ManeuverRequest:
  direction: int
  origin: str
  episode: tuple
  owned: bool = False


class RequestGuard:
  def __init__(self):
    self.owner = None
    self.rejected = None
    self.reason = 'idle'

  def reset(self):
    self.owner = self.rejected = None
    self.reason = 'idle'

  def resolve(self, request, *, executing, active_direction, driver=False):
    # A rejected owned command stays consumed while that exact episode resolves,
    # even if rejecting it cancelled the turn. Withdrawal/replacement ends it.
    # Identityless observations are never latched across execution lifetime.
    if not executing:
      self.owner = None
    if request is None or (self.rejected and request.episode != self.rejected):
      self.rejected = None
    if driver:
      self.owner = self.rejected = None
      self.reason = 'driver_precedence'
      return request
    if request is None:
      self.reason = 'withdrawn'
      return None
    if request.owned and self.rejected == request.episode:
      self.reason = 'rejected_owned_episode'
      return None
    if executing and active_direction in (1, 2) and request.direction != active_direction:
      self.rejected = request.episode if request.owned else None
      self.reason = 'opposite_owned_request' if request.owned else 'opposite_identityless_observation'
      return None
    self.reason = 'accepted_owned' if request.owned else 'accepted_observed'
    return request

  def executing(self, request):
    if self.owner is None and request is not None:
      self.owner = request

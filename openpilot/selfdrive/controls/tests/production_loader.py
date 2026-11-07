"""Ordinary full-module import; only unavailable native process/Params edges.
No resolver, FSM, classifier, SideState, AssistantLane or schema doubles.
"""
import sys
import types
from pathlib import Path
SOURCE = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(SOURCE))
# Prefer real installed dependencies. Offline source-only worker lacks native
# Params/scheduling edges; narrow fallbacks do not replace any motion logic.
try:
  from openpilot.common.realtime import DT_MDL
except ModuleNotFoundError:
  rt = types.ModuleType('openpilot.common.realtime')
  rt.DT_MDL = .05
  rt.DT_CTRL = .01
  sys.modules[rt.__name__] = rt
try:
  from openpilot.common.params import Params
except ModuleNotFoundError:
  params = types.ModuleType('openpilot.common.params')
  class Params:
    def __init__(self, *a, **kw): self.values = {}
    def get_int(self, key): return int(self.values.get(key, 0))
    def get_float(self, key): return float(self.values.get(key, 0))
    def get_bool(self, key): return bool(self.values.get(key, False))
  params.Params = Params
  sys.modules[params.__name__] = params
from openpilot.cereal import car, log, custom
from openpilot.selfdrive.controls.lib.desire_helper import DesireHelper

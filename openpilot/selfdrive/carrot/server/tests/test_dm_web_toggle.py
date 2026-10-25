"""Carrot Web ON/OFF for DriverMonitoringEnabled through the existing settings API.

The switch reuses /api/param_set and /api/params_bulk; no separate disable
route exists. Params are file-backed in tmp_path so a "restart" (new Params
object, new app) reads the value back the way PERSISTENT storage does.
"""
import asyncio
import enum
import json
import re
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from openpilot.selfdrive.carrot.server.features import params as params_feature
from openpilot.selfdrive.carrot.server.services import param_changes
from openpilot.selfdrive.carrot.server.services import params as params_service

NAME = "DriverMonitoringEnabled"
BOOL_DEFAULTS = {NAME: True, "IsOnroad": False, "DriverMonitoringSessionDisabled": False}


class FakeType(enum.Enum):
  BOOL = 1
  INT = 2
  FLOAT = 3
  TIME = 4
  STRING = 5
  JSON = 6
  BYTES = 7


class FileParams:
  """Minimal Params double: one file per key, typed BOOL keys only."""

  def __init__(self, root: Path, fail_writes: bool = False) -> None:
    self.root = root
    self.fail_writes = fail_writes
    root.mkdir(parents=True, exist_ok=True)

  def get_type(self, key):
    if key in BOOL_DEFAULTS:
      return FakeType.BOOL
    raise KeyError(key)

  def get_bool(self, key):
    path = self.root / key
    return path.read_text() == "1" if path.exists() else BOOL_DEFAULTS[key]

  def put_bool(self, key, value):
    if self.fail_writes:
      raise OSError("read-only params")
    (self.root / key).write_text("1" if value else "0")

  def get(self, key, **_kwargs):
    path = self.root / key
    return path.read_bytes() if path.exists() else None

  def put(self, key, value):
    (self.root / key).write_text(json.dumps(value) if isinstance(value, (dict, list)) else str(value))

  def remove(self, key):
    (self.root / key).unlink(missing_ok=True)


class FakeBroker:
  def __init__(self, engaged: bool) -> None:
    self.last_snapshot = {"services": {"selfdriveState": {"enabled": engaged}}}


@pytest.fixture
def device(tmp_path, monkeypatch):
  root = tmp_path / "params"
  state = {"params": FileParams(root)}
  monkeypatch.setattr(params_service, "HAS_PARAMS", True)
  monkeypatch.setattr(params_service, "ParamKeyType", FakeType)
  monkeypatch.setattr(params_service, "Params", lambda: state["params"])
  monkeypatch.setattr(param_changes, "CARROT_PARAM_CHANGES_PATH", str(tmp_path / "state" / "param_changes.jsonl"))
  # The observer remembers the last value per name in-process; start clean.
  param_changes._known_values.clear()
  yield root, state
  param_changes._known_values.clear()


def run(coro):
  return asyncio.run(coro)


async def call(method, path, payload=None, engaged=False):
  app = web.Application()
  app["realtime_broker"] = FakeBroker(engaged)
  params_feature.register(app)
  async with TestClient(TestServer(app)) as client:
    if method == "GET":
      response = await client.get(path)
    else:
      response = await client.post(path, json=payload)
    return response.status, await response.json()


def read_web_value():
  status, body = run(call("GET", f"/api/params_bulk?names={NAME}"))
  assert status == 200 and body["ok"]
  return body["values"][NAME]


def set_web_value(value, engaged=False):
  return run(call("POST", "/api/param_set", {"name": NAME, "value": value, "source": "web_ui"}, engaged=engaged))


def test_default_is_on_without_any_stored_value(device):
  root, _ = device
  assert not (root / NAME).exists()
  assert read_web_value() is True


def test_off_then_on_round_trips_and_is_audited(device):
  root, _ = device
  status, body = set_web_value(0)
  assert status == 200 and body["ok"] and body["value"] == 0
  assert (root / NAME).read_text() == "0"
  assert read_web_value() is False

  status, _ = set_web_value(1)
  assert status == 200
  assert (root / NAME).read_text() == "1"
  assert read_web_value() is True

  changes = param_changes.read_param_changes(name=NAME)
  # Newest first; only the transitions, with the web source and no other data.
  assert [(c["prev"], c["next"], c["source"], c["engaged"]) for c in changes] == [
    (False, 1, "web_ui", False),
    (True, 0, "web_ui", False),
  ]
  assert all(set(c) <= {"ts", "name", "prev", "next", "source", "engaged", "prev_hash", "hash"} for c in changes)
  assert param_changes.verify_param_changes()["ok"]


def test_repeating_the_current_value_adds_no_audit_record(device):
  set_web_value(0)
  set_web_value(0)
  assert len(param_changes.read_param_changes(name=NAME)) == 1


def test_off_persists_across_a_restart(device):
  root, state = device
  set_web_value(0)
  # Restart: fresh Params object and a fresh app reading the same storage.
  state["params"] = FileParams(root)
  assert read_web_value() is False


def test_write_failure_reports_error_keeps_value_and_records_nothing(device):
  root, state = device
  state["params"] = FileParams(root, fail_writes=True)
  status, body = set_web_value(0)
  assert status == 500 and not body["ok"]
  assert not (root / NAME).exists()
  assert read_web_value() is True
  assert param_changes.read_param_changes(name=NAME) == []


def test_engaged_change_is_allowed_and_marked_in_audit(device):
  status, _ = set_web_value(0, engaged=True)
  assert status == 200
  [change] = param_changes.read_param_changes(name=NAME)
  assert change["engaged"] is True


def test_turning_off_offroad_clears_the_uncertain_dm_offroad_alert(device):
  root, _ = device
  (root / "Offroad_DriverMonitoringUncertain").write_text("{}")
  set_web_value(0)
  assert not (root / "Offroad_DriverMonitoringUncertain").exists()


def test_session_disable_marker_stays_internal(device):
  root, _ = device
  status, body = run(call("POST", "/api/param_set",
                          {"name": "DriverMonitoringSessionDisabled", "value": 1, "source": "web_ui"}))
  assert status == 403 and not body["ok"]
  assert not (root / "DriverMonitoringSessionDisabled").exists()


def test_no_dedicated_driver_monitoring_route_exists():
  # Static scan: importing every feature pulls device-only dependencies.
  features_dir = Path(params_feature.__file__).parent
  routes = []
  for source in features_dir.rglob("*.py"):
    routes += re.findall(r"add_(?:get|post|put|delete|patch|route)\(\s*[\"']([^\"']+)",
                         source.read_text(encoding="utf-8"))
  assert "/api/param_set" in routes
  assert not [r for r in routes if "monitor" in r.lower() or "/dm" in r.lower()]

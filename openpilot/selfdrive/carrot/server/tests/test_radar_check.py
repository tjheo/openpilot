import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

# Load portable receive-only code without device-only feature package imports.
ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("radar_check_service", ROOT / "services/radar_check.py")
rc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = rc
spec.loader.exec_module(rc)


def state(now=0):
  return {name: {"valid": True, "received": now, "data": data} for name, data in {
    "carState": {"standstill": True, "vEgo": 0., "gearShifter": "park"},
    "deviceState": {"started": True}, "pandaStates": {"ignition": True}}.items()}


def scc(index, bus=2):
  a = bytes([(index % 16) << 4]) + bytes(7)
  b = bytearray(8)
  b[7] = index % 15
  b[7] |= ((-sum((x >> 4) + (x & 15) for x in b)) % 16) << 4
  return [(0x420, a, bus), (0x421, bytes(b), bus)]


def observation(bus=2, mode="scc", transform=lambda frames, i: frames):
  obs = rc.Observation(bus, mode, 0)
  for i in range(500):
    obs.feed(transform(scc(i), i), (i + 1) / 50)
  return obs.result(10)


def test_stable_scc_and_both_wrap_moduli():
  result = observation()
  assert result["verdict"] == "stable_rx"
  assert all(r["count"] == 500 and r["hz"] == 50 and r["counter_errors"] == 0 for r in result["rows"])
  assert result["rows"][1]["checksum_errors"] == 0
  assert result["rows"][0]["checksum_errors"] is None


def test_only_wrong_bus():
  result = observation(bus=0)
  assert result["verdict"] == "no_signal"
  assert {r["bus"] for r in result["rows"]} == {2}


def test_missing_one():
  assert observation(transform=lambda frames, _: frames[:1])["verdict"] == "inconclusive"


def test_dropped_frame_counter_detected():
  result = observation(transform=lambda frames, i: [] if i == 30 else frames)
  assert result["verdict"] == "inconclusive"
  assert all(r["counter_errors"] == 1 for r in result["rows"])


def test_stale_can_not_pass():
  assert observation(transform=lambda frames, i: [] if i > 450 else frames)["verdict"] == "inconclusive"


def test_no_can_is_not_zero_hz_pass():
  assert rc.Observation(2, "scc", 0).result(10)["verdict"] == "inconclusive"


def test_tx_echo_excluded_even_with_valid_payload():
  result = observation(transform=lambda frames, i: scc(i, 130))
  assert result["verdict"] == "inconclusive" and result["rows"] == []


def test_raw_empty_objects_and_optional_slots():
  obs = rc.Observation(1, "raw", 0)
  for i in range(200):
    obs.feed([(0x500, bytes(8), 1)], (i + 1) / 20)
  result = obs.result(10)
  assert result["verdict"] == "stable_rx"
  assert result["rows"][0]["counter_errors"] is None
  assert len(result["rows"]) == 1  # neither 64 mandatory slots nor ObjValid required


@pytest.mark.parametrize("change", [
  lambda s: s["carState"]["data"].update(vEgo=1),
  lambda s: s["carState"]["data"].update(standstill=False),
  lambda s: s["carState"]["data"].update(gearShifter="unknown"),
  lambda s: s["carState"]["data"].update(gearShifter="drive"),
  lambda s: s["deviceState"]["data"].update(started=False),
  lambda s: s["pandaStates"]["data"].update(ignition=False),
  lambda s: s["carState"].update(valid=False),
  lambda s: s["carState"].update(received=-2),
  lambda s: s["carState"]["data"].update(vEgo=float("nan")),
])
def test_unsafe_admission(change):
  s = state()
  change(s)
  assert rc.safety_reason(s, 0)


def test_safe_admission():
  assert rc.safety_reason(state(), 0) is None


class FakeSource:
  def __init__(self, moving=False):
    self.closed = False
    self.polls = 0
    self.moving = moving

  def poll(self, now):
    self.polls += 1
    s = state(now)
    if self.moving and self.polls > 3:
      s["carState"]["data"]["gearShifter"] = "drive"
    return s, scc(self.polls)

  def close(self):
    self.closed = True


def context():
  return {"expected_bus": 2, "HyundaiCameraSCC": 1, "EnableRadarTracks": 0}


def test_rapid_click_one_job_and_released_source():
  source = FakeSource()
  check = rc.RadarCheck(lambda: source, context, duration=.05)
  job = check.start()
  assert check.start()["id"] == job["id"]
  check.thread.join(1)
  assert not check.thread.is_alive() and source.closed
  assert check.snapshot()["status"] == "done"
  check.close()


def test_motion_mid_test_aborts_and_closes():
  source = FakeSource(moving=True)
  check = rc.RadarCheck(lambda: source, context, duration=.5)
  check.start()
  check.thread.join(1)
  assert source.closed
  assert "검사 중단" in check.snapshot()["result"]["reason"]
  assert check.snapshot()["result"]["verdict"] == "inconclusive"


def test_shutdown_releases_active_job():
  source = FakeSource()
  check = rc.RadarCheck(lambda: source, context)
  check.start()
  check.close()
  assert source.closed and not check.thread.is_alive()


def test_native_close_releases_socket_references():
  source = rc.NativeSource.__new__(rc.NativeSource)
  released = []
  class Socket:
    def __del__(self):
      released.append(True)
  source.sockets = {name: Socket() for name in ("can", "carState", "deviceState", "pandaStates")}
  source.state = state()
  source.close()
  assert len(released) == 4 and not source.sockets and not source.state


def test_checksum_counter_and_bad_lengths():
  stats = rc.FrameStats()
  stats.add(0x421, bytes(7), 0)
  stats.add(0x421, bytes(7) + b"\x0f", .02)
  assert stats.invalid_length == stats.counter_errors == stats.checksum_errors == 1


def test_read_only_source_contract():
  source = (ROOT / "services/radar_check.py").read_text()
  assert "pub_sock(" not in source and "put(" not in source and "put_int(" not in source
  assert "recv_one_or_none" in source


def load_routes():
  name = "radar_test.features.radar_check"
  for package in ("radar_test", "radar_test.features", "radar_test.services"):
    sys.modules.setdefault(package, types.ModuleType(package))
  sys.modules["radar_test.services.radar_check"] = rc
  spec = importlib.util.spec_from_file_location(name, ROOT / "features/radar_check.py")
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def test_http_async_poll_same_origin_and_validation():
  async def run():
    app = web.Application()
    routes = load_routes()
    routes.register(app)
    source = FakeSource()
    app["radar_check"] = rc.RadarCheck(lambda: source, context, duration=.05)
    async with TestClient(TestServer(app)) as client:
      response = await client.post("/api/radar_check/start", json={})
      assert response.status == 403
      response = await client.post("/api/radar_check/start", json={}, headers={"X-Carrot-Radar-Check": "1", "Origin": "https://evil.invalid"})
      assert response.status == 403
      response = await client.post("/api/radar_check/start", json={"bus": 128}, headers={"X-Carrot-Radar-Check": "1"})
      assert response.status == 400
      response = await client.post("/api/radar_check/start", json={"bus": 2, "mode": "scc"}, headers={"X-Carrot-Radar-Check": "1"})
      assert response.status == 202
      job = (await response.json())["job"]
      response = await client.post("/api/radar_check/start", json={}, headers={"X-Carrot-Radar-Check": "1"})
      assert (await response.json())["job"]["id"] == job["id"]
      await asyncio.sleep(.1)
      response = await client.get("/api/radar_check/status")
      assert (await response.json())["job"]["status"] == "done"
      assert response.headers["Cache-Control"] == "no-store"
    assert source.closed
  asyncio.run(run())


def test_native_source_separates_boot_and_monotonic_clocks(monkeypatch):
  # Simulated prior suspend: BOOTTIME is 100 seconds ahead of MONOTONIC.
  monkeypatch.setattr(rc.time, "clock_gettime", lambda _: 110.)
  monkeypatch.setattr(rc.time, "monotonic", lambda: 10.)
  source = rc.NativeSource.__new__(rc.NativeSource)
  source.state = {}
  source.sockets = {name: name for name in ("can", "carState", "deviceState", "pandaStates")}
  class Message:
    valid = True
    def __init__(self, name, stamp, data):
      self.logMonoTime = int(stamp * 1e9)
      setattr(self, name, data)
  messages = {
    "carState": [Message("carState", 10., types.SimpleNamespace(standstill=True, vEgo=0., gearShifter="park"))],
    "deviceState": [Message("deviceState", 10., types.SimpleNamespace(started=True))],
    "pandaStates": [Message("pandaStates", 110., [types.SimpleNamespace(ignitionLine=True, ignitionCan=False)])],
    "can": [Message("can", 110., [types.SimpleNamespace(address=0x420, dat=bytes(8), src=2)])],
  }
  source.messaging = types.SimpleNamespace(recv_one_or_none=lambda name: messages[name].pop(0) if messages[name] else None)
  s, frames = source.poll(10.)
  assert rc.safety_reason(s, 10.) is None and len(frames) == 1
  assert s["pandaStates"]["received"] == 10.
  # An old queued event must not become fresh merely because it was just read.
  messages["carState"] = [Message("carState", 1., types.SimpleNamespace(standstill=True, vEgo=0., gearShifter="park"))]
  s, _ = source.poll(10.)
  assert rc.safety_reason(s, 10.)
  source.close()


def test_unsafe_start_never_enters_running_and_closes():
  source = FakeSource()
  clock = iter((0., 3., 3.))
  source.poll = lambda now: ({}, [])
  check = rc.RadarCheck(lambda: source, context, clock=lambda: next(clock))
  check.start()
  check.thread.join(1)
  assert source.closed
  assert check.snapshot()["progress"] == 0
  assert check.snapshot()["result"]["verdict"] == "inconclusive"


def test_source_exception_is_inconclusive_and_released():
  source = FakeSource()
  def broken(_):
    raise RuntimeError("no source")
  source.poll = broken
  check = rc.RadarCheck(lambda: source, context)
  check.start()
  check.thread.join(1)
  assert source.closed and check.snapshot()["result"]["verdict"] == "inconclusive"


def native_fake(monkeypatch, mono=10., boot=110.):
  monkeypatch.setattr(rc.time, "monotonic", lambda: mono)
  monkeypatch.setattr(rc.time, "clock_gettime", lambda _: boot)
  source = rc.NativeSource.__new__(rc.NativeSource)
  source.state = {}
  source.sockets = {name: name for name in ("can", "carState", "deviceState", "pandaStates")}
  messages = {name: [] for name in source.sockets}
  source.messaging = types.SimpleNamespace(recv_one_or_none=lambda name: messages[name].pop(0) if messages[name] else None)
  return source, messages


def test_native_old_state_does_not_gain_extra_freshness(monkeypatch):
  source, messages = native_fake(monkeypatch)
  for name in ("carState", "deviceState", "pandaStates"):
    data = (types.SimpleNamespace(standstill=True, vEgo=0., gearShifter="park") if name == "carState" else
            types.SimpleNamespace(started=True) if name == "deviceState" else [types.SimpleNamespace(ignitionLine=True, ignitionCan=False)])
    messages[name] = [types.SimpleNamespace(valid=True, logMonoTime=int((108.6 if name == "pandaStates" else 8.6) * 1e9), **{name: data})]
  s, _ = source.poll(9.99)  # publishers may publish after poll start
  assert rc.safety_reason(s, 10.) is None
  assert rc.safety_reason(s, 10.2)  # no additional 1.5s granted from dequeue


def test_native_publication_after_poll_start_is_accepted(monkeypatch):
  source, messages = native_fake(monkeypatch)
  messages["carState"] = [types.SimpleNamespace(valid=True, logMonoTime=int(9.999999 * 1e9),
                                               carState=types.SimpleNamespace(standstill=True, vEgo=0., gearShifter="park"))]
  s, _ = source.poll(9.99)
  assert s["carState"]["valid"]


def test_invalid_native_can_is_rejected(monkeypatch):
  source, messages = native_fake(monkeypatch)
  messages["can"] = [types.SimpleNamespace(valid=False, logMonoTime=int(110 * 1e9), can=[])]
  with pytest.raises(ValueError, match="invalid CAN"):
    source.poll(10.)


def test_batched_can_uses_event_time_not_dequeue_time():
  obs = rc.Observation(2, "scc", 0.)
  for batch in range(100):
    frames = []
    for i in range(batch * 5, batch * 5 + 5):
      frames += [(addr, data, bus, (i + 1) / 50) for addr, data, bus in scc(i)]
    obs.feed(frames, (batch + 1) / 10)
  result = obs.result(10)
  assert result["verdict"] == "stable_rx"
  assert all(row["max_gap_ms"] == 20 for row in result["rows"])
  # Before the observation window is never counted.
  obs.feed([(0x420, bytes(8), 2, -.1)], 10.)
  assert obs.result(10)["rows"][0]["count"] == 500


def test_auto_raw_uses_legacy_track_bus_not_scc_bus():
  source = FakeSource()
  check = rc.RadarCheck(lambda: source, context, duration=.01)
  assert check.start(mode="raw")["bus"] == 1
  check.close()

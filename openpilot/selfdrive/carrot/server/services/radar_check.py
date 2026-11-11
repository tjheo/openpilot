"""Bounded, receive-only Hyundai legacy CAN diagnostic. No publisher or Params writes."""
from __future__ import annotations

import math
import threading
import time
import uuid
from dataclasses import dataclass

DURATION = 10.0
FRESH_STATE = 1.5
SCC_IDS = (0x420, 0x421)


def safety_reason(state, now):
  for name in ("carState", "deviceState", "pandaStates"):
    item = state.get(name)
    if not item or not item["valid"] or not 0 <= now - item["received"] <= FRESH_STATE:
      return f"{name} 최신 유효 데이터 없음"
  car = state["carState"]["data"]
  if not car.get("standstill") or not math.isfinite(car.get("vEgo", math.nan)) or abs(car["vEgo"]) > 0.1:
    return "정차 확인 실패 / 차량 움직임"
  if car.get("gearShifter") != "park":
    return "P 기어 확인 필요"
  if not state["deviceState"]["data"].get("started") or not state["pandaStates"]["data"].get("ignition"):
    return "시동 ON 확인 필요"
  return None


@dataclass
class FrameStats:
  count: int = 0
  first: float | None = None
  last: float | None = None
  max_gap: float = 0.0
  counter: int | None = None
  counter_errors: int = 0
  checksum_errors: int = 0
  invalid_length: int = 0

  def add(self, address, data, now):
    if len(data) != 8:
      self.invalid_length += 1
      return
    if self.last is not None:
      self.max_gap = max(self.max_gap, now - self.last)
    self.first = now if self.first is None else self.first
    self.last = now
    self.count += 1
    if address in SCC_IDS:
      counter = data[0] >> 4 if address == 0x420 else data[7] & 15
      modulus = 16 if address == 0x420 else 15
      if counter >= modulus or (self.counter is not None and counter != (self.counter + 1) % modulus):
        self.counter_errors += 1
      self.counter = counter
      if address == 0x421 and sum((b >> 4) + (b & 15) for b in data) % 16:
        self.checksum_errors += 1

  def summary(self, address, now, duration):
    return {"id": f"0x{address:03X}", "count": self.count,
            "hz": round(self.count / max(duration, 0.001), 1),
            "age_ms": None if self.last is None else round((now - self.last) * 1000),
            "max_gap_ms": round(self.max_gap * 1000),
            "counter_errors": self.counter_errors if address in SCC_IDS else None,
            "checksum_errors": self.checksum_errors if address == 0x421 else None,
            "invalid_length": self.invalid_length}


class Observation:
  def __init__(self, bus, mode, start):
    self.bus, self.mode, self.start = bus, mode, start
    self.stats = {}
    self.last_can = None

  def feed(self, frames, now):
    for frame in frames:
      address, data, src = frame[:3]
      received = frame[3] if len(frame) == 4 else now
      if received < self.start or received > now:
        continue
      if src not in (0, 1, 2):  # never treat Panda TX echo (128+) as RX
        continue
      self.last_can = max(self.last_can or received, received)
      if address in SCC_IDS or 0x500 <= address < 0x540:
        self.stats.setdefault((src, address), FrameStats()).add(address, data, received)

  def result(self, now):
    elapsed = max(now - self.start, 0.001)
    rows = [dict(bus=bus, **stats.summary(address, now, elapsed))
            for (bus, address), stats in sorted(self.stats.items())]
    selected = [row for row in rows if row["bus"] == self.bus and
                (row["id"] in ("0x420", "0x421") if self.mode == "scc" else 0x500 <= int(row["id"], 16) < 0x540)]
    def stable(row, hz):
      return (hz * 0.9 <= row["hz"] <= hz * 1.1 and row["age_ms"] is not None and row["age_ms"] <= 150 and
              row["max_gap_ms"] <= 120 and not row["counter_errors"] and
              not row["checksum_errors"] and not row["invalid_length"])
    if self.last_can is None or now - self.last_can > 0.5:
      verdict, reason = "inconclusive", "최신 CAN RX 없음 (0 Hz를 정상으로 판정하지 않음)"
    elif not selected:
      verdict, reason = "no_signal", "선택 버스에 해당 신호 없음 / 모드·배선 확인 필요"
    elif self.mode == "scc" and len(selected) == 2 and all(stable(row, 50) for row in selected):
      verdict, reason = "stable_rx", "SCC11·SCC12 50 Hz 수신 및 카운터 연속성 확인"
    elif self.mode == "raw" and any(stable(row, 20) for row in selected):
      verdict, reason = "stable_rx", "관측된 legacy raw track 20 Hz 수신 (전체 슬롯/객체 유효성 검사는 아님)"
    else:
      verdict, reason = "inconclusive", "일부 신호 누락 / 주기·카운터·체크섬 불안정"
    return {"verdict": verdict, "reason": reason, "rows": rows,
            "can_age_ms": None if self.last_can is None else round((now - self.last_can) * 1000),
            "checksum_note": "SCC12 nibble sum 검사; SCC11/raw checksum 미검사",
            "limitation": "통신 수신 확인이며 배선 전체/가감속 보증 아님. 전달된 RX는 원 송신 ECU 증명이 아님."}


def read_context():
  from opendbc.car import structs
  from opendbc.car.hyundai.hyundaicanfd import CanBus
  from opendbc.car.hyundai.values import HyundaiFlags

  from openpilot.common.params import Params
  params = Params()
  raw = params.get("CarParams")
  if not raw:
    raise ValueError("CarParams 없음: 차량 인식 후 다시 검사")
  with structs.CarParams.from_bytes(raw) as cp:
    if cp.brand != "hyundai" or cp.flags & HyundaiFlags.CANFD:
      raise ValueError("현대/기아 legacy CAN 전용 검사 (CAN FD 미지원)")
    buses = CanBus(cp)
    bus = buses.CAM if cp.flags & HyundaiFlags.CAMERA_SCC else buses.ECAN
    return {"expected_bus": bus if bus in (0, 1, 2) else None, "expected_raw_bus": 1,
            "HyundaiCameraSCC": params.get_int("HyundaiCameraSCC"),
            "EnableRadarTracks": params.get_int("EnableRadarTracks"),
            "note": "설정은 예상 버스 참고값이며 실제 수신 증명이 아님"}


class NativeSource:
  """All sockets owned by one diagnostic thread; bounded reads, no CAN TX."""
  def __init__(self):
    from openpilot.cereal import messaging
    self.messaging = messaging
    self.sockets = {}
    self.state = {}
    try:
      for name in ("can", "carState", "deviceState", "pandaStates"):
        self.sockets[name] = messaging.sub_sock(name, conflate=name != "can")
    except Exception:
      self.close()
      raise

  def poll(self, now):
    for name in ("carState", "deviceState", "pandaStates"):
      msg = self.messaging.recv_one_or_none(self.sockets[name])
      if msg is None:
        continue
      # Reject old queued events as well as missing newly received updates.
      stamp = msg.logMonoTime / 1e9
      # Python car/device publishers use MONOTONIC; C++ pandad uses BOOTTIME.
      received_now = time.monotonic()
      event_now = time.clock_gettime(time.CLOCK_BOOTTIME) if name == "pandaStates" else received_now
      age = event_now - stamp
      valid = bool(msg.valid) and 0 <= age <= FRESH_STATE
      data = getattr(msg, name)
      if name == "carState":
        fields = {"standstill": bool(data.standstill), "vEgo": float(data.vEgo), "gearShifter": str(data.gearShifter)}
      elif name == "deviceState":
        fields = {"started": bool(data.started)}
      else:
        fields = {"ignition": any(p.ignitionLine or p.ignitionCan for p in data)}
      self.state[name] = {"valid": valid, "received": received_now - age, "data": fields}
    frames = []
    for _ in range(200):
      msg = self.messaging.recv_one_or_none(self.sockets["can"])
      if msg is None:
        break
      if not msg.valid:
        raise ValueError("invalid CAN RX event")
      received_now = time.monotonic()
      age = time.clock_gettime(time.CLOCK_BOOTTIME) - msg.logMonoTime / 1e9
      if 0 <= age <= 0.25:
        frames.extend((c.address, bytes(c.dat), c.src, received_now - age) for c in msg.can)
    return self.state, frames

  def close(self):
    # msgq SubSocket releases its native subscription in __dealloc__; no close API.
    self.sockets.clear()
    self.state.clear()


class RadarCheck:
  def __init__(self, source_factory=NativeSource, context_reader=read_context, clock=time.monotonic, duration=DURATION):
    self.source_factory, self.context_reader, self.clock, self.duration = source_factory, context_reader, clock, duration
    self.lock = threading.Lock()
    self.stop = threading.Event()
    self.thread = None
    self.job = None

  def snapshot(self):
    with self.lock:
      return dict(self.job) if self.job else None

  def start(self, bus="auto", mode="scc"):
    with self.lock:
      if self.thread and self.thread.is_alive():
        return dict(self.job)  # rapid clicks share one bounded job
      context = self.context_reader()
      selected = context.get("expected_raw_bus", 1) if mode == "raw" else context["expected_bus"]
      selected = selected if bus == "auto" else bus
      if selected not in (0, 1, 2) or mode not in ("scc", "raw"):
        raise ValueError("CAN RX 버스 0·1·2와 SCC/raw 모드를 선택하세요")
      self.stop.clear()
      self.job = {"id": uuid.uuid4().hex[:12], "status": "checking", "progress": 0,
                  "bus": selected, "mode": mode, "context": context, "result": None}
      self.thread = threading.Thread(target=self._run, daemon=True, name="radar-rx-check")
      self.thread.start()
      return dict(self.job)

  def _update(self, **kwargs):
    with self.lock:
      self.job.update(kwargs)

  def _run(self):
    source = None
    try:
      source = self.source_factory()
      deadline = self.clock() + 2.0
      reason = "최신 정차 상태 대기"
      while not self.stop.is_set():
        now = self.clock()
        state, _ = source.poll(now)
        now = self.clock()
        reason = safety_reason(state, now)
        if reason is None or now >= deadline:
          break
        self.stop.wait(0.02)
      if reason or self.stop.is_set():
        self._update(status="done", result={"verdict": "inconclusive", "reason": reason or "검사 중지"})
        return
      start = self.clock()
      observation = Observation(self.job["bus"], self.job["mode"], start)
      while not self.stop.is_set():
        now = self.clock()
        state, frames = source.poll(now)
        now = self.clock()
        reason = safety_reason(state, now)
        if reason:
          self._update(status="done", result={"verdict": "inconclusive", "reason": f"검사 중단: {reason}"})
          return
        observation.feed(frames, now)
        self._update(status="running", progress=min(100, int((now - start) / self.duration * 100)))
        if now - start >= self.duration:
          self._update(status="done", progress=100, result=observation.result(now))
          return
        self.stop.wait(0.01)
      self._update(status="done", result={"verdict": "inconclusive", "reason": "서버 종료로 검사 중단"})
    except Exception:  # receiver failure must terminate as inconclusive
      self._update(status="done", result={"verdict": "inconclusive", "reason": "수신 검사 실행 실패 / CAN·상태 서비스 확인 필요"})
    finally:
      if source is not None:
        source.close()

  def close(self):
    self.stop.set()
    if self.thread:
      self.thread.join(timeout=3)

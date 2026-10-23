"""SVXL v1: framed, read-only RPM telemetry over one TCP connection (see PROTOCOL.md).

Every frame, both directions, big-endian:
  0  4s  magic "SVXL"
  4  B   version (1)
  5  B   type
  6  H   payload length (fixed per type, see PAYLOAD_LEN)
  8  ..  payload
  -4 I   crc32 (zlib) of every preceding byte of the frame

The handshake has no key and is NOT authentication: it only proves the peer speaks this protocol.
Golden vectors shared with the Android app: tests/fixtures/live_frames.json.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

MAGIC = b"SVXL"
VERSION = 1
HEADER = struct.Struct(">4sBBH")
CRC = struct.Struct(">I")
OVERHEAD = HEADER.size + CRC.size  # 12

# client -> comma
T_HELLO = 0x01
T_PING = 0x02
# comma -> client
T_WELCOME = 0x81
T_RPM = 0x82
T_PONG = 0x83
T_REJECT = 0x84
T_BYE = 0x85

HELLO = struct.Struct(">IHH")        # client_nonce (non-zero), client_build, reserved(0)
PING = struct.Struct(">II")          # session_id, ping_id (strictly increasing, non-zero)
WELCOME = struct.Struct(">IIHH")     # session_id (non-zero), client_nonce echo, heartbeat_ms, client_timeout_ms
RPM = struct.Struct(">IIIHHBBH")     # session_id, seq, frame_counter, rpm_raw, frame_age_ms, status, flags, reserved(0)
PONG = struct.Struct(">II")          # session_id, ping_id echo
REJECT = struct.Struct(">BBH")       # reason, reserved(0), reserved(0)
BYE = struct.Struct(">IBBH")         # session_id, reason, reserved(0), reserved(0)

PAYLOAD = {T_HELLO: HELLO, T_PING: PING, T_WELCOME: WELCOME, T_RPM: RPM, T_PONG: PONG, T_REJECT: REJECT, T_BYE: BYE}
PAYLOAD_LEN = {t: s.size for t, s in PAYLOAD.items()}
CLIENT_TYPES = frozenset({T_HELLO, T_PING})
SERVER_TYPES = frozenset({T_WELCOME, T_RPM, T_PONG, T_REJECT, T_BYE})
MAX_FRAME = OVERHEAD + max(PAYLOAD_LEN.values())  # 32

# telemetry status: exactly one per RPM frame. FLAG_RPM_VALID <=> STATUS_OK (+ FRAME_SEEN, frame_counter >= 1).
STATUS_OK = 0
STATUS_WAIT_CAR = 1          # CarParams not (yet) available: car not identified
STATUS_UNSUPPORTED_CAR = 2   # not the supported KIA_SELTOS profile
STATUS_UNSUPPORTED_CANFD = 3
STATUS_NO_FRAME = 4          # supported car, but no EMS11 frame on bus 0 yet
STATUS_STALE = 5             # last EMS11 frame older than the stale limit
STATUS_ENGINE_OFF = 6        # EMS11 rpm 0
STATUS_INVALID = 7           # EMS11 fault bit, SNA or implausible
MAX_STATUS = 7

FLAG_RPM_VALID = 0x01
FLAG_FRAME_SEEN = 0x02
KNOWN_FLAGS = FLAG_RPM_VALID | FLAG_FRAME_SEEN

REJECT_BUSY = 1
REJECT_BAD_HELLO = 2
REJECT_VERSION = 3

BYE_STOPPING = 1             # sender turned off, offroad or process stopping
BYE_CLIENT_TIMEOUT = 2
BYE_CAR_CHANGED = 3          # car selection lost/changed: all sessions end, reconnect for a new one
BYE_REPLACED = 4             # a newer handshake from the same address replaced this session
BYE_SLOW_READER = 5
BYE_PROTOCOL_ERROR = 6
BYE_RATE_LIMIT = 7
REJECT_REASONS = frozenset({REJECT_BUSY, REJECT_BAD_HELLO, REJECT_VERSION})
BYE_REASONS = frozenset(range(BYE_STOPPING, BYE_RATE_LIMIT + 1))

U16_MAX = 0xFFFF
U32_MAX = 0xFFFFFFFF


class FrameError(ValueError):
  pass


@dataclass(frozen=True)
class Frame:
  type: int
  fields: tuple


def _encode_raw(ftype: int, payload: bytes, version: int = VERSION) -> bytes:
  head = HEADER.pack(MAGIC, version, ftype, len(payload)) + payload
  return head + CRC.pack(zlib.crc32(head) & U32_MAX)


def encode(ftype: int, *fields) -> bytes:
  if ftype not in PAYLOAD:
    raise ValueError("unknown frame type")
  return _encode_raw(ftype, PAYLOAD[ftype].pack(*fields))


def hello(nonce: int, build: int = 0) -> bytes:
  return encode(T_HELLO, nonce, build, 0)


def ping(session_id: int, ping_id: int) -> bytes:
  return encode(T_PING, session_id, ping_id)


def welcome(session_id: int, nonce: int, heartbeat_ms: int, client_timeout_ms: int) -> bytes:
  return encode(T_WELCOME, session_id, nonce, heartbeat_ms, client_timeout_ms)


def status_consistent(status: int, flags: int, frame_counter: int) -> bool:
  """FLAG_RPM_VALID <=> STATUS_OK, and OK also requires FLAG_FRAME_SEEN and a frame counter >= 1."""
  if bool(flags & FLAG_RPM_VALID) != (status == STATUS_OK):
    return False
  return status != STATUS_OK or (bool(flags & FLAG_FRAME_SEEN) and frame_counter > 0)


def rpm(session_id: int, seq: int, frame_counter: int, rpm_raw: int, frame_age_ms: int, status: int, flags: int) -> bytes:
  if not status_consistent(status, flags, frame_counter) or flags & ~KNOWN_FLAGS or not 0 <= status <= MAX_STATUS:
    raise ValueError("inconsistent status/flags")
  return encode(T_RPM, session_id, seq, frame_counter, rpm_raw, frame_age_ms, status, flags, 0)


def pong(session_id: int, ping_id: int) -> bytes:
  return encode(T_PONG, session_id, ping_id)


def reject(reason: int) -> bytes:
  return encode(T_REJECT, reason, 0, 0)


def bye(session_id: int, reason: int) -> bytes:
  return encode(T_BYE, session_id, reason, 0, 0)


def _validate(ftype: int, f: tuple) -> None:
  if ftype == T_HELLO:
    if f[0] == 0 or f[2] != 0:
      raise FrameError("hello")
  elif ftype in (T_PING, T_PONG):
    if f[0] == 0 or f[1] == 0:
      raise FrameError("ping")
  elif ftype == T_WELCOME:
    if f[0] == 0 or f[1] == 0 or f[2] == 0 or f[3] == 0:
      raise FrameError("welcome")
  elif ftype == T_RPM:
    sid, seq, fc, _raw, _age, status, flags, rsv = f
    if sid == 0 or seq == 0 or rsv != 0 or status > MAX_STATUS or flags & ~KNOWN_FLAGS:
      raise FrameError("rpm")
    if not status_consistent(status, flags, fc):
      raise FrameError("rpm_status")
  elif ftype == T_REJECT:
    if f[0] not in REJECT_REASONS or f[1] != 0 or f[2] != 0:
      raise FrameError("reject")
  elif ftype == T_BYE:
    if f[1] not in BYE_REASONS or f[2] != 0 or f[3] != 0:
      raise FrameError("bye")


class FrameReader:
  """Incremental, bounded frame parser for a byte stream (handles partial reads).

  `allowed` restricts the accepted frame types (a server only accepts client types). The buffer never
  holds more than one maximum-size frame plus one recv chunk: `feed` raises FrameError instead."""

  def __init__(self, allowed: frozenset, max_buffer: int = 4 * MAX_FRAME):
    self.allowed = allowed
    self.max_buffer = max_buffer
    self.buf = bytearray()

  def feed(self, data: bytes) -> list[Frame]:
    if len(self.buf) + len(data) > self.max_buffer:
      raise FrameError("buffer")
    self.buf += data
    out = []
    while len(self.buf) >= HEADER.size:
      magic, ver, ftype, plen = HEADER.unpack_from(self.buf, 0)
      if magic != MAGIC:
        raise FrameError("magic")
      if ver != VERSION:
        raise FrameError("version")
      if ftype not in self.allowed:
        raise FrameError("type")
      if plen != PAYLOAD_LEN[ftype]:
        raise FrameError("length")
      total = OVERHEAD + plen
      if len(self.buf) < total:
        break
      frame = bytes(self.buf[:total])
      del self.buf[:total]
      (crc,) = CRC.unpack_from(frame, total - CRC.size)
      if crc != (zlib.crc32(frame[:-CRC.size]) & U32_MAX):
        raise FrameError("crc")
      fields = PAYLOAD[ftype].unpack_from(frame, HEADER.size)
      _validate(ftype, fields)
      out.append(Frame(ftype, fields))
    return out


def decode_one(data: bytes, allowed: frozenset = CLIENT_TYPES | SERVER_TYPES) -> Frame:
  """Decode exactly one complete frame (tests and fixtures)."""
  r = FrameReader(allowed)
  frames = r.feed(data)
  if len(frames) != 1 or r.buf:
    raise FrameError("partial")
  return frames[0]

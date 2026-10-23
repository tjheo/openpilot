"""SVX1 compact RPM telemetry datagram, v1 (wire-compatible with the SVX Android app).

Ported unchanged in behaviour from the SVX project's comma-sidecar/svx/protocol.py.
Golden vectors: tests/fixtures/packets.json (byte-identical copy of the SVX project's fixtures).

Layout, big-endian, 32 bytes + 8-byte HMAC tag when authenticated (40 bytes):
  0  4s  magic "SVX1"
  4  B   version (1)
  5  B   flags
  6  H   reserved (must be 0)
  8  I   session_id (random, non-zero, per companion start)
  12 I   seq (starts at 1, +1 per datagram, never wraps within a session)
  16 I   frame_counter (+1 only per newly accepted EMS11 frame)
  20 H   rpm_raw (EMS11 N raw, 0.25 rpm/bit)
  22 H   frame_age_ms (now - frame logMonoTime, saturating; 65535 = none/unknown)
  24 I   sender_mono_ms (sender monotonic ms mod 2^32, diagnostic only)
  28 I   crc32 (zlib) of bytes 0..27
  32 8s  HMAC-SHA256(key, bytes 0..31)[:8], present iff FLAG_AUTH
"""
import hashlib
import hmac
import struct
import zlib
from dataclasses import dataclass

MAGIC = b"SVX1"
VERSION = 1
BODY = struct.Struct(">4sBBHIIIHHI")
BASE_LEN = BODY.size + 4  # 32
TAG_LEN = 8
AUTH_LEN = BASE_LEN + TAG_LEN  # 40

FLAG_RPM_VALID = 0x01
FLAG_FRAME_SEEN = 0x02
FLAG_ALERT_ACTIVE = 0x04  # never set by this companion
FLAG_ALERT_KNOWN = 0x08   # never set by this companion
FLAG_SIMULATED = 0x10     # never set by this companion
FLAG_AUTH = 0x80
KNOWN_FLAGS = FLAG_RPM_VALID | FLAG_FRAME_SEEN | FLAG_ALERT_ACTIVE | FLAG_ALERT_KNOWN | FLAG_SIMULATED | FLAG_AUTH

U16_MAX = 0xFFFF
U32_MAX = 0xFFFFFFFF


@dataclass(frozen=True)
class Packet:
  flags: int
  session_id: int
  seq: int
  frame_counter: int
  rpm_raw: int
  frame_age_ms: int
  sender_mono_ms: int


def encode(p: Packet, key: bytes = b"") -> bytes:
  flags = p.flags | (FLAG_AUTH if key else 0)
  if flags & ~KNOWN_FLAGS:
    raise ValueError("unknown flags")
  if p.session_id == 0:
    raise ValueError("session_id must be non-zero")
  body = BODY.pack(MAGIC, VERSION, flags, 0, p.session_id, p.seq & U32_MAX, p.frame_counter & U32_MAX,
                   p.rpm_raw & U16_MAX, min(max(p.frame_age_ms, 0), U16_MAX), p.sender_mono_ms & U32_MAX)
  data = body + struct.pack(">I", zlib.crc32(body) & U32_MAX)
  if key:
    data += hmac.new(key, data, hashlib.sha256).digest()[:TAG_LEN]
  return data


class DecodeError(ValueError):
  pass


def decode(data: bytes, key: bytes = b"") -> Packet:
  """Strict decoder mirroring the Android receiver (tests and dry-run only)."""
  expected = AUTH_LEN if key else BASE_LEN
  if len(data) != expected:
    raise DecodeError("length")
  magic, ver, flags, rsv, sid, seq, fc, raw, age, mono = BODY.unpack_from(data, 0)
  if magic != MAGIC:
    raise DecodeError("magic")
  if ver != VERSION:
    raise DecodeError("version")
  (crc,) = struct.unpack_from(">I", data, BODY.size)
  if crc != (zlib.crc32(data[:BODY.size]) & U32_MAX):
    raise DecodeError("crc")
  if rsv != 0 or flags & ~KNOWN_FLAGS:
    raise DecodeError("flags")
  if bool(flags & FLAG_AUTH) != bool(key):
    raise DecodeError("auth_mode")
  if key:
    tag = hmac.new(key, data[:BASE_LEN], hashlib.sha256).digest()[:TAG_LEN]
    if not hmac.compare_digest(tag, data[BASE_LEN:]):
      raise DecodeError("auth")
  if sid == 0:
    raise DecodeError("session")
  return Packet(flags, sid, seq, fc, raw, age, mono)

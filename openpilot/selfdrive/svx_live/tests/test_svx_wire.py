"""SVXL v1 framing: shared golden vectors (same file is used by the Android app tests) and strict parsing."""
import binascii
import json
import os
import struct

import pytest

from openpilot.selfdrive.svx_live import wire as W
from openpilot.selfdrive.svx_live.netpolicy import AddressPolicy

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "live_frames.json")
with open(FIXTURE, encoding="utf-8") as _f:
  FX = json.load(_f)
ENCODERS = {W.T_HELLO: lambda f: W.hello(f[0], f[1]), W.T_PING: lambda f: W.ping(*f), W.T_WELCOME: lambda f: W.welcome(*f),
            W.T_RPM: lambda f: W.rpm(*f[:7]), W.T_PONG: lambda f: W.pong(*f), W.T_REJECT: lambda f: W.reject(f[0]),
            W.T_BYE: lambda f: W.bye(f[0], f[1])}


@pytest.mark.parametrize("v", FX["valid"], ids=lambda v: v["name"])
def test_golden_vector_decode_and_byte_exact_encode(v):
  data = bytes.fromhex(v["hex"])
  f = W.decode_one(data, W.CLIENT_TYPES if v["dir"] == "c2s" else W.SERVER_TYPES)
  assert f.type == v["type"] and list(f.fields) == v["fields"]
  assert ENCODERS[f.type](f.fields) == data
  assert len(data) <= W.MAX_FRAME


def test_one_vector_by_hand():
  """Independent of wire.py: header, payload and zlib crc32 laid out manually."""
  body = b"SVXL" + bytes([1, 0x02]) + struct.pack(">H", 8) + struct.pack(">II", 0xCAFEBABE, 1)
  expect = body + struct.pack(">I", binascii.crc32(body))
  assert next(v for v in FX["valid"] if v["name"] == "ping")["hex"] == expect.hex()


@pytest.mark.parametrize("v", FX["invalid"], ids=lambda v: f"{v['name']}-{v['dir']}")
def test_invalid_vectors_rejected_with_reason(v):
  r = W.FrameReader(W.CLIENT_TYPES if v["dir"] == "c2s" else W.SERVER_TYPES)
  with pytest.raises(W.FrameError) as e:
    r.feed(bytes.fromhex(v["hex"]))
  assert str(e.value) == v["error"]


def test_partial_reads_any_split():
  stream = b"".join(bytes.fromhex(v["hex"]) for v in FX["valid"] if v["dir"] == "s2c")
  n = sum(1 for v in FX["valid"] if v["dir"] == "s2c")
  for chunk in (1, 2, 3, 7, 11, 31, 64):
    r = W.FrameReader(W.SERVER_TYPES)
    got = []
    for i in range(0, len(stream), chunk):
      got += r.feed(stream[i:i + chunk])
    assert len(got) == n and not r.buf


def test_buffer_is_bounded():
  r = W.FrameReader(W.SERVER_TYPES)
  with pytest.raises(W.FrameError, match="buffer"):
    r.feed(b"\x00" * (W.MAX_FRAME * 4 + 1))
  r = W.FrameReader(W.SERVER_TYPES)
  r.feed(bytes.fromhex(FX["valid"][3]["hex"])[:5])  # partial header waits
  assert len(r.buf) == 5


def test_encoder_refuses_inconsistent_status():
  with pytest.raises(ValueError):
    W.rpm(1, 1, 1, 3200, 0, W.STATUS_STALE, W.FLAG_RPM_VALID)
  with pytest.raises(ValueError):
    W.rpm(1, 1, 1, 3200, 0, W.STATUS_OK, 0)
  with pytest.raises(ValueError):
    W.rpm(1, 1, 1, 3200, 0, 8, 0)


def test_constants_match_fixture():
  c = FX["constants"]
  assert c["default_port"] == 47816 and c["max_frame"] == W.MAX_FRAME
  assert c["status"]["OK"] == W.STATUS_OK and c["status"]["INVALID"] == W.STATUS_INVALID == W.MAX_STATUS
  assert set(c["bye"].values()) == W.BYE_REASONS and set(c["reject"].values()) == W.REJECT_REASONS


@pytest.mark.parametrize("ip", FX["addresses"]["allowed"])
def test_address_allowed(ip):
  assert AddressPolicy().allowed(ip)


@pytest.mark.parametrize("ip", FX["addresses"]["rejected"])
def test_address_rejected(ip):
  assert not AddressPolicy().allowed(ip)


def test_loopback_only_by_explicit_test_injection():
  assert not AddressPolicy().allowed("127.0.0.1")
  assert AddressPolicy(allow_loopback=True).allowed("127.0.0.1")
  assert not AddressPolicy(allow_loopback=True).allowed("8.8.8.8")
  assert not AddressPolicy().allowed(None) and not AddressPolicy().allowed(b"10.0.0.1")

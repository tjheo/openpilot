import json
import os

import pytest

from svxrpm import protocol as P
from svxrpm.core import RpmState, Sender
from helpers import KEY, ems

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def vectors():
  with open(os.path.join(FIX, "packets.json")) as f:
    return json.load(f)["vectors"]


@pytest.mark.parametrize("v", vectors(), ids=lambda v: v["name"])
def test_fixture_parity(v):
  key = v["key"].encode()
  data = bytes.fromhex(v["hex"])
  if v["ok"]:
    p = P.decode(data, key)
    e = v["expect"]
    assert (p.flags, p.session_id, p.seq, p.frame_counter, p.rpm_raw, p.frame_age_ms, p.sender_mono_ms) == (
      e["flags"], e["session_id"], e["seq"], e["frame_counter"], e["rpm_raw"], e["frame_age_ms"], e["sender_mono_ms"])
    # byte-exact re-encode (the app consumes the same vectors)
    assert P.encode(P.Packet(e["flags"] & ~P.FLAG_AUTH, e["session_id"], e["seq"], e["frame_counter"], e["rpm_raw"],
                             e["frame_age_ms"], e["sender_mono_ms"]), key) == data
  else:
    with pytest.raises(P.DecodeError) as ei:
      P.decode(data, key)
    assert str(ei.value) == v["error"]


def test_fixture_set_is_complete():
  names = {v["name"] for v in vectors()}
  assert {"valid_auth", "auth_wrong_key", "auth_bad_tag", "auth_expected_missing", "auth_unexpected"} <= names


def test_sender_requires_strong_key():
  st = RpmState()
  for bad in (b"", b"short", b"x" * 31, "0" * 64):
    with pytest.raises(ValueError):
      Sender(st, key=bad)


def test_sender_always_authenticated_and_wrong_or_no_key_rejected():
  st = RpmState()
  s = Sender(st, key=KEY, session_id=0x1234)
  st.ingest(1_000_000_000, ems(3200), 1_010_000_000)
  data = s.build(1_010_000_000, allow_valid=True)
  assert len(data) == P.AUTH_LEN and data[5] & P.FLAG_AUTH
  p = P.decode(data, KEY)
  assert p.rpm_raw == 3200 and p.flags & P.FLAG_RPM_VALID and p.frame_age_ms == 10
  with pytest.raises(P.DecodeError, match="auth"):
    P.decode(data, b"f" * 64)
  with pytest.raises(P.DecodeError, match="length"):
    P.decode(data, b"")  # an unauthenticated receiver never accepts it
  assert "0123456789abcdef" not in repr(s)

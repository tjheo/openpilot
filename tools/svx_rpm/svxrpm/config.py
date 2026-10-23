"""Strict companion config: a small JSON file outside the repository, owner-only (0600).

{
  "version": 1,
  "profile": "kia_seltos_2021_ems11",
  "bus": 0,
  "target_ip": "<phone Wi-Fi IPv4>",
  "port": 47816,
  "key_hex": "<64 lowercase hex chars = 32 random bytes>"
}

Key: 32 random bytes of entropy, written as 64 lowercase hex characters. The app uses the UTF-8
bytes of the text typed into its pairing-key field as the HMAC-SHA256 key, so for exact
compatibility the HMAC key here is the 64 ASCII bytes of that hex text (not the 32 decoded bytes).
No key means no start; there is no unauthenticated mode.
"""
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass

from .profile import PROFILES

DEFAULT_PORT = 47816
DEFAULT_CONFIG_PATH = "/data/svx_rpm/config.json"
KEY_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_CONFIG_BYTES = 4096
FIELDS = {"version", "profile", "bus", "target_ip", "port", "key_hex"}


class ConfigError(ValueError):
  pass


@dataclass(frozen=True)
class Config:
  profile: str
  bus: int
  target_ip: str
  port: int
  key: bytes = b""

  def __repr__(self):  # never print the key
    return f"Config(profile={self.profile!r}, bus={self.bus}, target_ip={self.target_ip!r}, port={self.port}, key=<redacted>)"

  __str__ = __repr__


def validate_target_ip(ip) -> str:
  """Unicast host address inside an RFC1918 range, dotted-quad only (mirrors the app's check).
  Rejects loopback, link-local, CGNAT, public, multicast/broadcast and .0/.255 host octets."""
  if not isinstance(ip, str) or not re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", ip):
    raise ConfigError("target_ip must be a dotted IPv4 address")
  o = [int(x) for x in ip.split(".")]
  if any(x > 255 for x in o) or any(len(s) > 1 and s[0] == "0" for s in ip.split(".")):
    raise ConfigError("target_ip is not a valid IPv4 address")
  private = o[0] == 10 or (o[0] == 172 and 16 <= o[1] <= 31) or (o[0] == 192 and o[1] == 168)
  if not private or o[3] in (0, 255):
    raise ConfigError("target_ip must be a private LAN unicast host (10/8, 172.16/12, 192.168/16)")
  return ip


def _int(d, name, lo, hi) -> int:
  v = d.get(name)
  if type(v) is not int or not (lo <= v <= hi):  # rejects bool, float, str
    raise ConfigError(f"{name} must be an integer in {lo}..{hi}")
  return v


def parse_config(d) -> Config:
  if not isinstance(d, dict):
    raise ConfigError("config must be a JSON object")
  if set(d) != FIELDS:
    # unknown names are not echoed: a pasted secret could be among them
    raise ConfigError(f"config fields mismatch (missing={sorted(FIELDS - set(d))}, unknown={len(set(d) - FIELDS)} field(s))")
  if d["version"] != 1 or type(d["version"]) is not int:
    raise ConfigError("version must be 1")
  profile = d["profile"]
  if not isinstance(profile, str) or profile not in PROFILES:
    raise ConfigError(f"unsupported profile (supported: {sorted(PROFILES)})")
  bus = _int(d, "bus", 0, 127)
  if bus not in PROFILES[profile].allowed_buses:
    raise ConfigError(f"bus {bus} is not allowed for profile {profile} (allowed: {sorted(PROFILES[profile].allowed_buses)})")
  port = _int(d, "port", 1024, 65535)
  key_hex = d["key_hex"]
  if not isinstance(key_hex, str) or not KEY_RE.fullmatch(key_hex):
    raise ConfigError("key_hex must be exactly 64 lowercase hex characters (32 random bytes)")
  return Config(profile=profile, bus=bus, target_ip=validate_target_ip(d["target_ip"]), port=port,
                key=key_hex.encode("ascii"))


def _no_duplicates(pairs):
  d = {}
  for k, v in pairs:
    if k in d:
      raise ValueError("duplicate key")
    d[k] = v
  return d


def _inside(path: str, roots) -> bool:
  path = os.path.realpath(path)
  for root in ((roots,) if isinstance(roots, str) else roots):
    root = os.path.realpath(root)
    if path == root or path.startswith(root.rstrip(os.sep) + os.sep):
      return True
  return False


def load_config(path: str, no_config_roots) -> Config:
  if _inside(path, no_config_roots):
    raise ConfigError("config must live outside the openpilot checkout (it holds a secret)")
  try:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))  # FIFO cannot block
  except OSError as e:
    raise ConfigError(f"cannot open config: {e.strerror}") from None
  try:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
      raise ConfigError("config is not a regular file")
    if st.st_mode & 0o077:
      raise ConfigError(f"config permissions are {stat.S_IMODE(st.st_mode):o}; run: chmod 600 {path}")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
      raise ConfigError("config is not owned by the current user")
    if st.st_size > MAX_CONFIG_BYTES:
      raise ConfigError("config is too large")
    raw = os.read(fd, MAX_CONFIG_BYTES + 1)
  finally:
    os.close(fd)
  try:
    d = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
  except (UnicodeDecodeError, ValueError):
    raise ConfigError("config is not valid JSON (or has duplicate keys)") from None
  return parse_config(d)


def write_new_config(path: str, no_config_roots, target_ip: str, profile: str, bus: int, port: int) -> str:
  """Create a new 0600 config with a fresh random key. Never overwrites. Returns the key hex."""
  if _inside(path, no_config_roots):
    raise ConfigError("config must live outside the openpilot checkout (it holds a secret)")
  key_hex = secrets.token_hex(32)
  cfg = {"version": 1, "profile": profile, "bus": bus, "target_ip": target_ip, "port": port, "key_hex": key_hex}
  parse_config(cfg)  # validate before touching the filesystem
  parent = os.path.dirname(os.path.abspath(path))
  os.makedirs(parent, mode=0o700, exist_ok=True)
  try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
  except FileExistsError:
    raise ConfigError("config already exists; delete it first to create a new key") from None
  try:
    os.fchmod(fd, 0o600)
    os.write(fd, (json.dumps(cfg, indent=1) + "\n").encode("ascii"))
    os.fsync(fd)
  finally:
    os.close(fd)
  return key_hex

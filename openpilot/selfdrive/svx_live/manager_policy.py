"""Manager-facing constants and the should_run predicate. Pure: importing this has no side effects."""
from __future__ import annotations

ENABLED_PARAM = "SvxLiveEnabled"   # BOOL, default "0" (off)
PORT_PARAM = "SvxLivePort"         # INT, default DEFAULT_PORT
DEFAULT_PORT = 47816
MIN_PORT = 1024
MAX_PORT = 65535


def svx_live_should_run(started: bool, params, CP) -> bool:
  """Runs only while onroad (deviceState.started) AND the toggle is on. Offroad it never runs.
  Any failure to read the toggle (e.g. a params build that does not know the key yet) means off."""
  if not started:
    return False
  try:
    return bool(params.get_bool(ENABLED_PARAM))
  except Exception:
    return False


def params_has_key(params, key: str) -> bool:
  """False instead of UnknownKeyName when the compiled params do not know `key` yet (stale build):
  the settings UI then simply hides the toggle instead of crashing."""
  try:
    params.check_key(key)
    return True
  except Exception:
    return False


def parse_port(value) -> int | None:
  """Typed INT param, bytes or text -> port in MIN_PORT..MAX_PORT, else None (listener stays closed)."""
  if value is None or isinstance(value, bool):
    return None
  if isinstance(value, bytes):
    try:
      value = value.decode("ascii")
    except UnicodeDecodeError:
      return None
  if isinstance(value, str):
    value = value.strip()
    if not value.isascii() or not value.isdigit() or len(value) > 5:
      return None
    value = int(value)
  if not isinstance(value, int):
    return None
  return value if MIN_PORT <= value <= MAX_PORT else None

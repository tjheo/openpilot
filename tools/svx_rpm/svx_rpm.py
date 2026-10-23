#!/usr/bin/env python3
"""SVX read-only RPM companion: EMS11 engine speed -> authenticated SVX1 UDP to the SVX Android app.

READ-ONLY: subscribes to the existing cereal `can` service and reads the `CarParams` param.
No publishers, no sendcan, no Panda/USB/socketcan access, no diagnostic requests, no param writes.
Manual use only; nothing starts it automatically.

  svx_rpm.py init --target <phone IP> [--config PATH]   create 0600 config with a new random key
  svx_rpm.py show-key [--config PATH]                     print the key to type into the app
  svx_rpm.py check [--config PATH]                        validate config (key redacted)
  svx_rpm.py scan [--seconds N]                           count EMS11 per bus, read-only
  svx_rpm.py run [--config PATH] [--dry-run] [--duration S]
"""
import argparse
import math
import os
import signal
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def find_openpilot_root() -> str:
  """In-tree (<root>/tools/svx_rpm) or a standalone copy next to the device checkout."""
  for root in (os.path.dirname(os.path.dirname(HERE)), "/data/openpilot"):
    if os.path.isfile(os.path.join(root, "openpilot", "cereal", "__init__.py")):
      return root
  return ""  # not found: init/check still work; scan/run will fail to import openpilot and exit


OPENPILOT_ROOT = find_openpilot_root()
NO_CONFIG_ROOTS = tuple(r for r in (OPENPILOT_ROOT, HERE) if r)  # the secret config must live outside code
sys.path.insert(0, HERE)
if OPENPILOT_ROOT and OPENPILOT_ROOT not in sys.path:
  sys.path.append(OPENPILOT_ROOT)  # read-only imports of openpilot.cereal / openpilot.common.params

from svxrpm import protocol as P
from svxrpm.config import DEFAULT_CONFIG_PATH, DEFAULT_PORT, ConfigError, load_config, validate_target_ip, write_new_config
from svxrpm.ems11 import decode_ems11
from svxrpm.profile import PROFILES

DEFAULT_PROFILE = "kia_seltos_2021_ems11"
MAX_RUN_DURATION_S = 7 * 24 * 3600.0


def seconds_arg(lo: float, hi: float):
  def parse(text: str) -> float:
    try:
      v = float(text)
    except ValueError:
      raise argparse.ArgumentTypeError("not a number") from None
    if not math.isfinite(v) or not (lo <= v <= hi):  # rejects NaN/Inf/negative
      raise argparse.ArgumentTypeError(f"must be a finite number in {lo:g}..{hi:g}")
    return v
  return parse


def lower_priority() -> None:
  try:
    os.setpriority(os.PRIO_PROCESS, 0, 19)  # lowest SCHED_OTHER priority; never competes with RT processes
  except (AttributeError, OSError):
    pass


def install_stop_handlers(stop: threading.Event) -> None:
  def handler(signum, frame):
    stop.set()
  for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(s, handler)


class DryRunOut:
  """Decodes instead of sending; prints at most once per second."""

  def __init__(self, key: bytes):
    self._key = key
    self._last = 0.0

  def send(self, data: bytes) -> None:
    p = P.decode(data, self._key)
    now = time.monotonic()
    if now - self._last >= 1.0:
      self._last = now
      print(f"seq={p.seq} fc={p.frame_counter} rpm={p.rpm_raw * 0.25:.0f} age_ms={p.frame_age_ms} " +
            f"valid={bool(p.flags & P.FLAG_RPM_VALID)} seen={bool(p.flags & P.FLAG_FRAME_SEEN)}", flush=True)

  def close(self) -> None:
    pass


def cmd_init(args) -> int:
  try:
    validate_target_ip(args.target)
    key_hex = write_new_config(args.config, NO_CONFIG_ROOTS, args.target, DEFAULT_PROFILE, 0, args.port)
  except ConfigError as e:
    print(f"error: {e}", file=sys.stderr)
    return 2
  print(f"created {args.config} (mode 600)")
  print("Pairing key (type exactly these 64 characters into the app's pairing-key field):")
  print(key_hex)
  return 0


def cmd_show_key(args) -> int:
  try:
    cfg = load_config(args.config, NO_CONFIG_ROOTS)
  except ConfigError as e:
    print(f"error: {e}", file=sys.stderr)
    return 2
  print(cfg.key.decode("ascii"))
  return 0


def cmd_check(args) -> int:
  try:
    cfg = load_config(args.config, NO_CONFIG_ROOTS)
  except ConfigError as e:
    print(f"error: {e}", file=sys.stderr)
    return 2
  print(f"ok: {cfg}")
  return 0


def open_stack(address: int, bus: int):
  """Read-only cereal subscriber + CarParams reader, or None with a message if unavailable."""
  src = None
  try:
    from svxrpm.cereal_source import CerealCanSource, ParamsCarSelection
    src = CerealCanSource(address, bus)
    return src, ParamsCarSelection()
  except Exception as e:  # e.g. not on a comma device / openpilot not built
    if src is not None:
      src.close()
    print(f"error: openpilot messaging unavailable ({type(e).__name__}: {e})", file=sys.stderr)
    return None


def cmd_scan(args) -> int:
  lower_priority()
  stop = threading.Event()
  install_stop_handlers(stop)
  profile = PROFILES[DEFAULT_PROFILE]
  stack = open_stack(profile.address, bus=-1)
  if stack is None:
    return 2
  src, sel_reader = stack
  sel = sel_reader.read()
  print(f"[scan] CarParams: {sel if sel else 'unavailable'}")
  per_bus = {}
  t0 = time.monotonic()
  next_print = t0 + 1.0
  try:
    while not stop.is_set() and time.monotonic() - t0 < args.seconds:
      for evt in src.iter_events():
        for c in evt.can:
          if c.address != profile.address:
            continue
          b = per_bus.setdefault(int(c.src), {"n": 0, "badlen": 0, "reasons": {}, "last_rpm": None})
          b["n"] += 1
          dat = bytes(c.dat)
          if len(dat) != 8:
            b["badlen"] += 1
            continue
          s = decode_ems11(dat)
          b["reasons"][s.reason] = b["reasons"].get(s.reason, 0) + 1
          b["last_rpm"] = s.rpm
      if time.monotonic() >= next_print:
        next_print += 1.0
        el = time.monotonic() - t0
        line = ", ".join(f"src{k}: {v['n'] / el:.0f}Hz rpm={v['last_rpm']}" for k, v in sorted(per_bus.items()))
        print(f"[scan {el:4.0f}s] {line or 'EMS11 0x316 not seen'}", flush=True)
      stop.wait(0.05)
  finally:
    src.close()
  if not per_bus:
    print("[scan] EMS11 0x316 NOT SEEN on any bus: this companion cannot provide RPM on this car/harness.")
    return 2
  for k, v in sorted(per_bus.items()):
    note = " (TX echo, never used)" if k >= 128 else (" <- profile bus" if k in profile.allowed_buses else " (not allowed by profile)")
    print(f"  src {k}{note}: frames={v['n']} badlen={v['badlen']} reasons={v['reasons']} last_rpm={v['last_rpm']}")
  return 0


def cmd_run(args) -> int:
  from svxrpm.companion import Companion, UdpOut, log
  try:
    cfg = load_config(args.config, NO_CONFIG_ROOTS)
  except ConfigError as e:
    print(f"error: {e}", file=sys.stderr)
    return 2
  lower_priority()
  stop = threading.Event()
  install_stop_handlers(stop)
  profile = PROFILES[cfg.profile]
  stack = open_stack(profile.address, cfg.bus)
  if stack is None:
    return 2
  src, sel_reader = stack
  try:
    out = DryRunOut(cfg.key) if args.dry_run else UdpOut(cfg.target_ip, cfg.port)
  except OSError as e:
    src.close()
    print(f"error: cannot create UDP socket ({e.strerror})", file=sys.stderr)
    return 2
  comp = Companion(cfg, src, sel_reader, out)
  log(f"start: session=0x{comp.sender.session_id:08X} profile={cfg.profile} bus={cfg.bus} " +
      f"-> {'dry-run' if args.dry_run else f'{cfg.target_ip}:{cfg.port}'} auth=on")
  return comp.run(stop, duration_s=args.duration)


def main(argv=None) -> int:
  ap = argparse.ArgumentParser(description="SVX read-only RPM companion")
  sub = ap.add_subparsers(dest="cmd", required=True)
  p = sub.add_parser("init")
  p.add_argument("--target", required=True, help="phone Wi-Fi IPv4 address (private LAN)")
  p.add_argument("--port", type=int, default=DEFAULT_PORT)
  p.add_argument("--config", default=DEFAULT_CONFIG_PATH)
  for name in ("show-key", "check"):
    sub.add_parser(name).add_argument("--config", default=DEFAULT_CONFIG_PATH)
  p = sub.add_parser("scan")
  p.add_argument("--seconds", type=seconds_arg(1.0, 120.0), default=10.0)
  p = sub.add_parser("run")
  p.add_argument("--config", default=DEFAULT_CONFIG_PATH)
  p.add_argument("--dry-run", action="store_true", help="decode and print instead of sending")
  p.add_argument("--duration", type=seconds_arg(0.0, MAX_RUN_DURATION_S), default=0.0,
                 help="stop after N seconds (0 = until stopped)")
  args = ap.parse_args(argv)
  return {"init": cmd_init, "show-key": cmd_show_key, "check": cmd_check, "scan": cmd_scan, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
  sys.exit(main())

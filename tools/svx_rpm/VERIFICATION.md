# VERIFICATION — tools/svx_rpm

Baseline: tjheo/openpilot `carrot-wip` 4920f270b56ad472c2f90ddf24f2643b19e7af37. Development host only.
**Not run on a comma device, in a vehicle, or against a phone.** No CAN traffic, Panda, or network peer
other than a loopback UDP socket in one test.

## Commands and results

Run from `tools/svx_rpm/`. `pytest.ini` makes this subtree its own pytest root, so the repository's root
`conftest.py` (which imports the manager) is not loaded. `tests/conftest.py` sets `sys.modules` entries for
`msgq`, `openpilot.cereal.messaging`, `openpilot.common.params`, `panda`, `usb1` to `None`, so any in-process attempt
to reach native messaging/Params/Panda fails with ImportError even on a built checkout. Every subprocess runs with
`python -I`; the only network use is UDP to 127.0.0.1 in one test.

```sh
python3 -m pytest -q -rs                 # Python 3.9.25, pycapnp 2.2.4
# 141 passed, 1 skipped  (skip: opendbc not importable on 3.9 — it needs Python >= 3.10)

<py3.12 venv with pytest, pycapnp, numpy>/python -m pytest -q   # Python 3.12.15
# 142 passed

ruff check tools/svx_rpm                 # ruff 0.17.2, repository pyproject.toml rules
# All checks passed!
```

| File | Tests | Covers |
|---|---:|---|
| test_protocol.py | 18 | all 15 SVX1 golden vectors from the app (decode + byte-exact re-encode, auth/wrong-key/missing-tag); sender always authenticated, needs a key of at least 32 bytes, wrong key / no key rejected, key absent from repr |
| test_ems11.py | 12 | EMS11 fixture frames, little-endian sample, wrong length; layout parsed from the current `hyundai_kia_generic.dbc` (`N : 16\|16@1+ (0.25,0)`, `F_N_ENG : 1\|1@1+`, single `BO_ 790`) |
| test_core.py | 9 | correct bus only, TX echo (128) ignored, newest frame wins, newer `valid=false` event blocks older data; future / <=0 / >300 ms old / duplicate / out-of-order rejected; stale and no-frame are invalid; heartbeat keeps counter/rpm; counter wrap and seq exhaustion start a new session |
| test_config.py | 50 | strict field/type checks, 64-char lowercase hex key, private-unicast-only targets (public, loopback, link-local, CGNAT, .0/.255, multicast, broadcast, leading zeros and IPv6 all rejected); 0600 and owner checks, symlink/dir/missing/FIFO (no blocking), duplicate JSON keys, unknown field names not echoed; config inside repo or tool directory refused; `init` never overwrites; CLI init/check/show-key |
| test_cli_isolated.py | 19 | copied tool tree + STUB `openpilot` package, `python -I`, no PYTHON* env: `run`, `run --dry-run`, `scan` reach the stub (sentinel file proves it) and exit 2 with no traceback/key; `--duration` NaN/±Inf/negative/>7 days/non-numeric and `--seconds` outside 1..120 rejected before any stack import; in-process sentinel active; source closed when the Params reader fails or the UDP socket cannot be created |
| test_companion.py | 9 | unknown selection → frames never used, invalid heartbeats; unsupported car / CAN-FD → one invalid heartbeat then exit 3; selection loss → new session with no data; stale → invalid; ENETUNREACH → normal cadence (~100 sends/20 s), rate-limited log; stop event → stop <200 ms, socket/source closed; drain capped at 512, keep 8; real child process with real UDP to loopback: authenticated valid packets, SIGTERM → exit 0 within 1 s, source closed, key not printed |
| test_cereal_integration.py | 5 | REAL `log.capnp`/`car.capnp` from this tree: serialized `can` Events through `CerealCanSource.poll` (socket is a double; msgq is not built here); valid/invalid events, wrong bus, garbage bytes; real `CarParams` bytes → selection (KIA_SELTOS ok, CANFD flag, other car, empty/garbage); on 3.12, opendbc `CAR.KIA_SELTOS` is a `HyundaiPlatformConfig` without CANFD and `HyundaiFlags.CANFD == 2**13` |
| test_audit.py | 20 | AST import allowlist (stdlib + `openpilot.cereal`, `openpilot.cereal.messaging`, `openpilot.common.params`); no sendcan/pub_sock/PubMaster/Panda/usb/AF_CAN/raw/broadcast/Params writes/safety/diagnostic/subprocess/exec/bind; only `sub_sock('can')` and `get('CarParams')`; `git status` shows changes only under `tools/svx_rpm/`. A deliberately injected `sendcan`/`.put(` line was detected, then removed |

## Fixture and DBC identity
```
c883948d97d988595bf1e4f2bd2d7cb03a1fb301cb6575339cbd273b7b8334f4  tests/fixtures/packets.json        (= SVX project protocol/fixtures)
ac97cf09b7e6c62decaca981dbbe9f4192177098b27746a0dc5456b48ec10b27  tests/fixtures/ems11_frames.json   (= SVX project protocol/fixtures)
1590039abb51cbfc3dc8ab4151ab78e7f549088fec878480ab4d6f850b18898a  opendbc_repo/opendbc/dbc/hyundai_kia_generic.dbc (unchanged)
```

## Repository APIs checked (source reading)
* `openpilot/cereal/messaging/__init__.py`: `sub_sock(endpoint, ..., conflate, timeout)`, `log_from_bytes`;
  services.py `can` is 100 Hz with a BIG queue.
* `openpilot/selfdrive/pandad/pandad.cc`: `can` is published through `MessageBuilder::initEvent`, which
  stamps `logMonoTime = nanos_since_boot()` (CLOCK_BOOTTIME). The companion compares against CLOCK_BOOTTIME
  sampled after dequeuing.
* `car.capnp`: `engineRpm @46` exists, but `opendbc/car/hyundai/carstate.py` never sets it, so raw EMS11
  is used. EMS11 is not parsed by the Hyundai carstate; fingerprints for KIA_SELTOS contain FW only.
* `opendbc/car/hyundai/values.py`: `KIA_SELTOS = HyundaiPlatformConfig(...)` (dbc `hyundai_kia_generic`);
  `get_can_parsers` uses bus 0 for `Bus.pt` on non-CAN-FD platforms.
* `openpilot/common/params_keys.h`: `CarParams` is BYTES, CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION;
  `Params.get` is non-blocking by default.

## Not verified
* A real msgq subscriber, or any run on comma hardware.
* EMS11 presence, bus, rate and latency on a real Seltos (any year, engine or harness).
* End-to-end with the Android app on a phone. App compatibility is covered only by the shared golden vectors.
* App fade-to-silence timing.

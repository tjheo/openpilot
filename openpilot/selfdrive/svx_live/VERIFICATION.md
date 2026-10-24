# VERIFICATION — openpilot/selfdrive/svx_live

Baseline: tjheo/openpilot `carrot-wip` be42a0b3db354cd41e60bd161a48f30d2e00b433 (contains PR #1 merge cf41199218).
Development host only. **Not run on a comma device, in a vehicle, against a phone, or with a real msgq/raylib
build.** Network use in tests: loopback TCP (127.0.0.0/8, allowed only by explicit test injection).

## Commands and results
Run from `openpilot/selfdrive/svx_live/`. Its `pytest.ini` makes this package its own pytest root, so the repository
root `conftest.py` (which imports the manager and needs a built tree) is not loaded. `tests/conftest.py` blocks
`msgq`, `openpilot.cereal.messaging`, `openpilot.common.params(_pyx)`, `panda`, `usb1` per test (restored afterwards),
so nothing can reach native messaging/Params/Panda. Child processes run with `python -I` and only `PATH`.

```sh
<python 3.12 venv with pytest, pycapnp 2.2.4, numpy>/python -m pytest -v -W error tests   # Python 3.12.15
# 223 passed  (-W error: no ResourceWarning/unclosed sockets)
ruff check openpilot/selfdrive/svx_live openpilot/selfdrive/ui/layouts/settings/toggles.py \
  openpilot/selfdrive/ui/mici/layouts/settings/toggles.py openpilot/system/manager/process_config.py   # ruff 0.17.2
# All checks passed!
PR_BODY="Docs-Not-Needed: ..." python tools/docs/check_user_docs.py --changed-file <each changed path>
# settings-catalog rule satisfied by docs/user/{ko,en}/settings.md; driver-monitoring rule (process_config.py is
# also mapped to it) needs the PR-body reason: DM processes/inputs/alerts are unchanged
g++ 11.5 -std=c++17: real openpilot/common/params_keys.h + ParamKeyFlag/Type/Attributes from params.h
# keys=384, SvxLiveEnabled {PERSISTENT, BOOL, "0"}, SvxLivePort {PERSISTENT, INT, "47816"}  (no scons build)
```
The app project additionally runs a REAL cross-language test: this package's `tests/daemon_harness.py` (real
`LiveDaemon` + `LiveServer`, loopback-only injection, synthetic EMS11 source) ⇄ the app's JVM `LiveClient`
over loopback TCP: handshake, FRESH 3200 rpm for >2.5 s without rate limiting, toggle OFF → `BYE STOPPING`
→ process exit 0 → client shows "unreachable". Shared fixture `tests/fixtures/live_frames.json` is byte-identical
(SHA-256 compared in that test).

| File | Tests | Covers |
|---|---:|---|
| test_svx_wire.py | 108 | 24 golden frames (decode + byte-exact re-encode, one vector rebuilt by hand with binascii.crc32), 27 malformed vectors with exact reasons (magic, version, type per direction, length, crc, status/flag consistency incl. OK without FRAME_SEEN / counter 0, unknown status/flag/reason, reserved, zero ids), any-split partial reads, bounded buffer, 51 shared address cases (RFC1918 + 100.64/10 allowed; public, loopback, link-local, multicast, broadcast, range edges, leading zeros, names, IPv6, non-ASCII digits rejected) |
| test_svx_server.py | 24 | real sockets: handshake + immediate telemetry, PING/PONG, 200 ms heartbeat, immediate status change, ≤50 Hz data, byte-by-byte HELLO, malformed/garbage/version/zero-nonce → REJECT+close, handshake timeout, in-session protocol errors, PING flood → BYE RATE_LIMIT, max 2 sessions → BUSY, same address replaces, pending cap, accept-rate limit, non-LAN peer gets 0 bytes, client timeout, kernel-queue-only backlog → telemetry dropped (never queued) → slow-reader cut, bounded user-space buffer, RST, end_sessions/close send BYE, bind failure bounded + rate-limited log, selector failure → closed/reopenable, failing logger, one select round per poll under flood |
| test_svx_daemon.py | 10 | OFF → no listener, no selector, no CAN subscription; unsupported car / CAN-FD / no CarParams → status only, never subscribes; supported → real rpm; invalid evidence and source exceptions → INVALID; missing frames → STALE; selection loss → BYE CAR_CHANGED + source closed + data dropped; toggle OFF → BYE STOPPING < 0.8 s; 50 Hz wall-clock cadence holds under an always-readable flood (and CPU < wall); invalid port → idle, no socket; unexpected tick errors contained (no manager crash loop); real child process: stream, toggle OFF teardown exit 0, SIGINT → BYE + exit 0 < 1 s |
| test_svx_rpm_state.py | 10 | ported freshness rules (bus/TX echo, newest wins, future/≤0/old/duplicate/out-of-order) + new: newer invalid event or bad-length frame is INVALID (never older good data), msgq backlog never refreshes freshness, every rejected newest frame silences until the next accepted frame, status mapping and RPM_VALID only for OK, counter wrap epoch |
| test_svx_ems11.py | 12 | ported unchanged: fixture frames, little-endian sample, wrong length, layout parsed from the current `hyundai_kia_generic.dbc` |
| test_svx_cereal.py | 7 | REAL `log.capnp`/`car.capnp`: serialized `can` events through `CerealCanSource.poll` (socket double), invalid/unparseable/short newest → INVALID, older garbage + newer good → good, bounded drain keeps newest 8 of 512, real `CarParams` → selection (Seltos ok, CAN-FD, other car, other brand, empty/garbage), opendbc `CAR.KIA_SELTOS` non-CAN-FD and `HyundaiFlags.CANFD == 2**13` |
| test_svx_integration.py | 29 | `svx_live_should_run`: offroad never (toggle not even read), default off, stale params build → off; port parsing; params_keys.h entries/defaults; process_config entry exact (no restart/spawn overrides); every module import is inert in a fresh `python -I` with socket/thread creation trapped (no msgq/capnp/params/cereal/system import) — what `PythonProcess.prepare` does while OFF; big UI (`TogglesLayout`) and C4 mici UI (`TogglesLayoutMici`) instantiated from the real files with stubbed widgets/runtime: toggle visible, default OFF, writes `SvxLiveEnabled` only, mirrors external changes, hidden (no crash) on a params build without the key; both `gui_app.big_ui()` paths reach these layouts; Korean `.po` entries parse with the real multilang parser |
| test_svx_audit.py | 23 | AST import allowlist (stdlib + cereal/messaging/params/swaglog); no sendcan/publisher/Panda/usb/raw/UDP/broadcast/multicast/connect/sendto/DNS/param writes/safety/diagnostic/subprocess/exec/pickle in any module; only `sub_sock('can')` and gets of `CarParams`/`SvxLiveEnabled`/`SvxLivePort`; exactly one `bind`; server handles only HELLO/PING; working-tree changes confined to this package, the retired `tools/svx_rpm/`, the 5 integration files and the 2 user guides, with no removed lines in process_config/params_keys/big-UI toggles/.po |

### Baseline 142 tests (PR #1 `tools/svx_rpm`) → this change
| Old file | Old | Now | Why |
|---|---:|---|---|
| test_ems11.py | 12 | test_svx_ems11.py 12 | ported unchanged (decoder unchanged) |
| test_core.py | 9 | test_svx_rpm_state.py 10 | freshness rules ported; UDP `Sender` tests replaced by status tests; stricter invalid-evidence rules added |
| test_cereal_integration.py | 5 | test_svx_cereal.py 7 | ported; adapted to explicit INVALID; drain-bound test moved here |
| test_audit.py | 20 | test_svx_audit.py 23 | ported and extended to the TCP listener and integration scope |
| test_companion.py | 9 | test_svx_daemon.py 10 | same intents (unsupported, selection loss, stale, network failure, bounded stop, real child + signal) on the new daemon |
| test_protocol.py | 18 | retired | SVX1 authenticated UDP datagram replaced by SVXL TCP (test_svx_wire.py) |
| test_config.py | 50 | retired | no config file, key, or target IP any more (user request: no key, no manual config) |
| test_cli_isolated.py | 19 | retired | no CLI (`init/show-key/check/scan/run`) any more; started by the manager |

## Repository APIs checked (source reading)
* `openpilot/selfdrive/ui/ui.py`: `MainLayout` if `gui_app.big_ui()` (tici/tizi or `BIG=1`) else `MiciMainLayout` (C4).
  Settings → `layouts/settings/toggles.py` `TogglesLayout` / `mici/layouts/settings/toggles.py` `TogglesLayoutMici`.
  This fork's UI is Python/raylib (no Qt).
* `system/manager/process.py`: `PythonProcess.prepare()` imports the module when `enabled` (here always) even if
  `should_run` is false; `ensure_running` stops with SIGINT (`block=False`) and restarts exited processes whose
  `should_run` is true — hence inert imports, no exit on unsupported car, contained tick errors, bind retry.
* `system/manager/manager.py`: 1 Hz loop; `CLEAR_ON_ONROAD_TRANSITION` params (incl. `CarParams`) are cleared before
  `ensure_running` in the same iteration.
* `common/params_pyx.pyx`: `check_key` raises `UnknownKeyName`; `get_bool` on an unknown key raises.
* `cereal/services.py` `can` 100 Hz; pandad stamps `logMonoTime = nanos_since_boot()` (CLOCK_BOOTTIME).
* `opendbc/car/hyundai/values.py` `KIA_SELTOS` non-CAN-FD platform, `Bus.pt` → bus 0; `hyundai_kia_generic.dbc` EMS11.

## Diagnostics change (svxLiveDiag, 2026-10-24)
Base: tjheo/openpilot `carrot-wip` f63ab7cefb7fa39bd1c71802f512cb704569d38b (contains PR #2 merge a975560bdb; the deployed
`svx_lived.py`/`cereal_source.py` were byte-identical to this base). Development host only; **not run on a comma device or
vehicle.** Adds `diagnostics.py` (stdlib only: `json`) and hooks in `svx_lived.py`/`cereal_source.py`; the RPM path,
512/8 drain bound, freshness, 50 Hz cadence, NO_FRAME semantics, protocol and app are unchanged. New reads: `GitCommit`
(once, only after the toggle is seen ON). No new process, param, publisher or socket.

```sh
<python 3.12.15 venv: pytest 9.1.1, pycapnp 2.2.4, numpy>/python -m pytest -q -W error tests   # 246 passed
ruff check openpilot/selfdrive/svx_live   # ruff 0.17.2: All checks passed!
```
`test_svx_diagnostics.py` (21): disabled → no record/param read/subscription; start + summary cadence (3 s, then 10 s)
and stop; true no-event (empty polls); events without target; target on other bus / TX echo counted but never accepted;
match/accept/`too_old` accounting; drain saturation and `drain_discarded`; subscriber / read / parse / invalid-event
separation; census budget counts actually scanned frames across events; census never changes `poll()` results
(randomized real capnp); telemetry and freshness byte-identical with and without diagnostics over 1200 ticks;
transition logs rate-limited under 50 Hz flapping; logger failure and broken diagnostics hooks never change the daemon
or skip invalidation; bounded keys/counters/floats/UTF-8 bytes and no secret/exception text at saturation; no
`error`/`debug` key (would change the cloudlog level); commit validation; reconnect flood does not add records;
snapshot against the real `LiveServer`; existing text logs carry exception categories only, subscriber log ≤ 1 per 10 s.
Audit/integration tests now also allow `json`, the `GitCommit` read and the new module in the inert-import check.

Payload bound only: the 4096-byte limit covers the event payload, not the swaglog envelope (`ctx` device context), so
extracted lines remain private. Not verified: device CPU cost (x86 synthetic: ~0.43 ms/tick extra for 2 events × 151 frames), real msgq, real drive.

## EMS_366 fallback (2026-10-24)
Base: tjheo/openpilot `carrot-wip` 47bc5e062be2045737b141f298e83295c3f12450. Development host only; **not run on a comma
device or vehicle.** Cause of NO_FRAME on a Seltos: its bus 0 carries no EMS11 (0x316 absent on every bus in two
recorded ~61 s rlogs, one driving 0–19 km/h with throttle, one parked idle). Fix: `ems366.py` decodes EMS_366 (0x366)
`N 8|16@1+ ×0.25` (this tree's `hyundai_kia_generic.dbc`, identical in upstream commaai/opendbc
`generator/hyundai/hyundai_can.dbc`; layout from commaai/opendbc PR #319), accepted with 7 (observed) or 8 (DBC) bytes.
`CerealCanSource` uses it only while no EMS11 frame (or wrong-length EMS11) has been seen by that source; the decoder is
chosen by address (`rpm_state.DECODERS`), never by length. Protocol, `rpm_raw` unit, 300 ms freshness, bus 0 only,
TX-echo rejection, default OFF and the app (incl. its 4000 rpm cap) are unchanged.

Offline evidence (private recordings, aggregates only): 0x366 and 0x162 both at ~100 Hz with constant DLC 7/8; 0x366 `N`
agrees frame-by-frame with the TCU's separately defined `TCU_DCT13 Cluster_Engine_RPM 10|13@1+ ×0.9766` (median
difference −0.4 rpm; with 0x162 one frame later mean |Δ| 0.43 rpm; never > 31 rpm over 12,253 pairs); `VS` byte 5 tracks
`carState.vEgoRaw` (moving mean −0.89 km/h, integer km/h); park idle 684–724 rpm, rises 200–740 rpm within 1 s of
throttle, falls on lift-off, stays near an idle floor while creeping 5–11 km/h (not proportional to wheel speed); no
DBC-undefined bit varies; never 0 or > 1760 rpm. No independent ground truth (tachometer/OBD) was available; 0x162 may
be a relay of the same EMS value, so it corroborates the layout, not the sensor. Engine-off (`N` = 0) was not observed.
Replay of both recordings through the real `LiveDaemon`/`CerealCanSource`/`RpmState` (re-serialized capnp events, 50 Hz
ticks): without the fallback 100% `NO_FRAME`; with it 100% `OK`, published rpm − 0x162 within ±26 rpm, frame age ≤ 22 ms.

```sh
<python 3.12.15 venv: pytest 9.1.1, pycapnp 2.2.4, numpy>/python -m pytest -q -W error tests   # 263 passed, 1 skipped
ruff check openpilot/selfdrive/svx_live   # ruff 0.17.2: All checks passed!
```
The skip is `test_change_is_confined_to_narrow_integration` (a git worktree has a `.git` file, not a directory); the same
path check was run by hand on the diff. New: `test_svx_ems366.py` (decoder, DBC layout, address-based decoder choice,
lengths), real-capnp fallback/latch tests in `test_svx_cereal.py`, a daemon end-to-end EMS_366 test. Against the old
source the new/changed tests fail (11 failures). The diagnostics census still counts 0x316 only.

## Not verified
* EMS_366 on a live car through real msgq (recorded-CAN replay only); engine-off/ISG and ignition transitions on 0x366;
  other Seltos markets, engines or transmissions.
* Any run on comma hardware; real msgq subscription; real raylib rendering of the toggles; full scons build.
* EMS11 presence, bus, rate and latency on a real Seltos; phone Wi-Fi/hotspot/tailnet behaviour; Galaxy S25 Ultra
  (Android 16).
* `TIOCOUTQ` semantics were exercised on this Linux host only (and via injection in tests), not on the comma kernel.

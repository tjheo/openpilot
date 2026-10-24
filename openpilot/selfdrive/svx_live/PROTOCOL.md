# SVXL v1 — SVX live RPM telemetry over TCP

Read-only engine-RPM telemetry from a comma device (`openpilot/selfdrive/svx_live`, process `svx_lived`) to the
SVX virtual-exhaust Android app (`kr.svx.app` 0.3.0+, `kr.svx.core.SvxLive`/`LiveTracker`/`LiveClient`).
Golden vectors shared by both implementations: `tests/fixtures/live_frames.json` (byte-identical in the app project
as `protocol/fixtures/live_frames.json`). This document is byte-identical in both projects.

**Incompatible with SVX1** (the authenticated UDP datagram of app ≤ 0.2.2 and of the retired `tools/svx_rpm`):
an app ≤ 0.2.2 cannot talk to `svx_lived`, and app 0.3.0 no longer receives SVX1.

## Security model (read first)
* The handshake has **no key and is not authentication**. It only proves that the peer speaks this protocol.
  Anyone who can reach the port from an allowed address can read the RPM and the status codes below.
* The comma accepts TCP connections only from RFC1918 private addresses (10/8, 172.16/12, 192.168/16) and
  100.64/10 (Tailscale tailnet), excluding each range's network and broadcast address. Anything else
  (public, loopback, link-local, multicast, broadcast, IPv6) is closed immediately without sending a byte.
  Loopback is allowed only by explicit test injection.
* Clients can send exactly two frame types: one `HELLO` and `PING`s. There is no command, configuration,
  diagnostic or shell channel; anything else ends the connection. Nothing a client sends changes vehicle
  behaviour, CAN traffic, params or files.
* TCP (not UDP) so the comma never sends data to an address that did not complete a TCP handshake with it
  (no spoofed-source reflection or amplification), and every byte belongs to an accepted session.
* The app accepts only a dotted IPv4 literal for the comma (no DNS name resolution).

## Transport and lifecycle
* Comma listens on `0.0.0.0:<SvxLivePort>` (default **47816**) only while `svx_lived` runs, which the manager
  allows only when the device is onroad (`deviceState.started`, i.e. the car is powered on — moving is not
  required) **and** Params `SvxLiveEnabled` is on (default off). Offroad or toggle off: no listener.
* Toggle off: sessions get `BYE STOPPING` within ~0.5 s (the daemon re-reads the toggle every 0.5 s; the manager
  also stops the process within ~1 s with SIGINT, which also sends `BYE STOPPING`).
* The phone connects, sends `HELLO`, receives `WELCOME`, then a `RPM` frame immediately and on every update.

## Frame format (both directions, big-endian)
| Offset | Size | Field |
|---:|---:|---|
| 0 | 4 | magic `"SVXL"` |
| 4 | 1 | version = 1 |
| 5 | 1 | type |
| 6 | 2 | payload length (fixed per type; anything else is an error) |
| 8 | n | payload |
| 8+n | 4 | CRC-32 (zlib/`java.util.zip.CRC32`) of bytes 0..8+n-1 |

Maximum frame size 32 bytes. Readers buffer at most 4 frames worth of bytes and fail on anything malformed;
the connection is then closed. All 32-bit fields are unsigned.

| Type | Dir | Payload |
|---|---|---|
| `0x01 HELLO` | app → comma | `nonce u32` (≠0), `client_build u16`, `reserved u16 = 0` |
| `0x02 PING` | app → comma | `session_id u32`, `ping_id u32` (≠0, strictly increasing) |
| `0x81 WELCOME` | comma → app | `session_id u32` (≠0, random), `nonce u32` (echo), `heartbeat_ms u16` (200), `client_timeout_ms u16` (3000) |
| `0x82 RPM` | comma → app | `session_id u32`, `seq u32` (1, 2, 3 … contiguous), `frame_counter u32`, `rpm_raw u16`, `frame_age_ms u16`, `status u8`, `flags u8`, `reserved u16 = 0` |
| `0x83 PONG` | comma → app | `session_id u32`, `ping_id u32` (echo) |
| `0x84 REJECT` | comma → app | `reason u8` (1 BUSY, 2 BAD_HELLO, 3 VERSION), `0 u8`, `0 u16`; then close |
| `0x85 BYE` | comma → app | `session_id u32`, `reason u8` (1 STOPPING, 2 CLIENT_TIMEOUT, 3 CAR_CHANGED, 4 REPLACED, 5 SLOW_READER, 6 PROTOCOL_ERROR, 7 RATE_LIMIT), `0 u8`, `0 u16`; then close |

Unknown types, reasons, statuses or flag bits and non-zero reserved fields are errors.

### RPM frame
* `rpm_raw`: engine-speed `N` raw value (0.25 rpm/bit) from EMS11 (0x316) or, on cars without EMS11, EMS_366 (0x366);
  both use the same unit, so the meaning is unchanged. The app shows/uses **real** rpm = raw × 0.25; any sound
  mapping (e.g. "real 4,000 rpm sounds like the former 6,500 rpm top") is app-internal.
* `frame_counter`: +1 only for each newly accepted RPM frame. Within a session it never decreases; equal
  counter ⇒ equal `rpm_raw` (heartbeat).
* `frame_age_ms`: comma-side age of the newest RPM frame at send time (now − `logMonoTime`, CLOCK_BOOTTIME),
  saturating; 65535 = none/unknown.
* `flags`: `0x01 RPM_VALID`, `0x02 FRAME_SEEN`.
* `status` (exactly one): 0 OK, 1 WAIT_CAR (CarParams not available yet), 2 UNSUPPORTED_CAR, 3 UNSUPPORTED_CANFD,
  4 NO_FRAME (supported car, neither EMS11 nor EMS_366 on bus 0 yet), 5 STALE (>300 ms), 6 ENGINE_OFF (rpm 0), 7 INVALID
  (EMS11 fault, SNA/implausible, or the newest CAN evidence was invalid/rejected).
* Rule (validated by both sides): `RPM_VALID` ⇔ `status == OK`, and OK also requires `FRAME_SEEN` and
  `frame_counter ≥ 1`.
* Sent on every new frame or status change (≤ 50 Hz per session) and otherwise as a heartbeat every 200 ms.

## Comma-side limits
* At most 2 sessions plus 2 pending handshakes; a new HELLO from the same address replaces that address's old
  session (`BYE REPLACED`), otherwise `REJECT BUSY`. Accepts are token-bucket limited (burst 8, 4/s).
* `HELLO` must arrive within 2 s. A session with no valid client frame for 3 s gets `BYE CLIENT_TIMEOUT`.
* Client frames: token bucket burst 20, 15/s (the app pings every 100 ms); exceeding it ⇒ `BYE RATE_LIMIT`.
* No telemetry backlog accumulates: if the kernel send queue holds > 512 unacknowledged/unsent bytes or a user-space
  write is pending, a new telemetry frame is dropped. User space holds at most the rest of one partially written frame
  plus PONGs (≤ 256 bytes in total); a peer whose user-space or kernel queue stays full for 2 s is disconnected. One
  64-byte `recv` per connection per 50 Hz tick.

## App-side freshness (sound only on fresh, confirmed data)
* The newest `RPM` frame decides: any status other than OK sets the target gain to 0 at once (the audible level
  then follows the app's fade-out of at most 400 ms plus audio/transport buffers), and an earlier OK sample is
  discarded — OK plays again only after a **new** `frame_counter` advance. The first frame of a session is never
  fresh evidence.
* **Backlog gate.** TCP is ordered, so a frame that arrives after the `PONG` of a `PING` sent at local time *t* was
  sent after *t*. The app therefore uses the conservative frame time *t* − `frame_age_ms` (not arrival time) and
  requires `now − (t − frame_age_ms) ≤ 300 ms`; with no `PONG` yet in the session, or a newest confirmed `PING`
  older than 300 ms, the state is DELAYED (silent). Frames queued behind a stalled link arrive before the next
  `PONG` and can never look fresh. The conservative age is (now − send time of the newest confirmed `PING` before
  that frame) + `frame_age_ms`; with 100 ms pings that is roughly up to one ping interval + round trip + frame age.
  It is not a fixed round-trip threshold: for example, round trips approaching ~200 ms will often exceed the 300 ms
  bound and play nothing, by design.
* No complete frame for 1 s ⇒ LINK_LOST (silent); 3 s ⇒ reconnect. Partial bytes do not count as frames.
* Sequence gaps, frame-counter regression, heartbeat value changes, foreign session ids, unknown `PONG` ids or more
  than 200 frames/s ⇒ protocol error, close, retry.
* The app writes at most 16 unanswered `PING`s (20 bytes each) and then stops writing until a `PONG` arrives, so
  its output can never fill a socket buffer.

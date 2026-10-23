"""Single-threaded, non-blocking TCP listener for SVXL v1 (stdlib selectors only).

Every resource is bounded: at most MAX_PENDING handshakes plus MAX_SESSIONS sessions, a small receive
buffer per connection, a small user-space send buffer, token-bucket limits on accepts and on client
frames, and timeouts for handshakes, silent clients and readers that stop reading.

Telemetry is latest-value data: when a partial write is pending or the kernel send queue already holds more
than OUTQ_SKIP bytes, new telemetry frames are dropped instead of queued, so no backlog accumulates (at most the
remainder of one partially written frame plus bounded PONGs wait in user space). This bounds, but cannot remove,
what a stalled link may deliver late. Freshness of anything that does arrive is decided by the app: it only uses telemetry that
follows the PONG of a recent PING (see PROTOCOL.md). PONG replies are queued (bounded by OUT_MAX); a peer
whose user-space or kernel queue stays full for SLOW_S is disconnected.

The only thing a client can ask for is the handshake and heartbeat (PING). There is no command channel.
Nothing in here is created at import time; open() creates the socket.
"""
from __future__ import annotations

import errno
import fcntl
import os
import selectors
import socket
import struct
import termios
import time

from . import wire as W

MAX_SESSIONS = 2
MAX_PENDING = 2
HANDSHAKE_TIMEOUT_S = 2.0
CLIENT_TIMEOUT_S = 3.0
HEARTBEAT_S = 0.2
MIN_INTERVAL_S = 0.02            # at most 50 telemetry frames/s per session
RATE_BURST = 20                  # client frames: the app sends 1 HELLO and 10 PING/s (100 ms)
RATE_PER_S = 15.0
ACCEPT_BURST = 8
ACCEPT_PER_S = 4.0
OUT_MAX = 256                    # user-space pending bytes per connection
OUTQ_SKIP = 512                  # kernel unsent+unacked bytes above which telemetry is dropped
SLOW_S = 2.0                     # pending output without progress for this long -> disconnect
RECV_CHUNK = 64
SNDBUF = 4096
HEARTBEAT_MS = int(HEARTBEAT_S * 1000)
CLIENT_TIMEOUT_MS = int(CLIENT_TIMEOUT_S * 1000)
LOG_PERIOD_S = 5.0


def new_session_id() -> int:
  while True:
    sid = int.from_bytes(os.urandom(4), "big")
    if sid != 0:
      return sid


def kernel_outq(sock) -> int:
  """Bytes not yet acknowledged by the peer (Linux TIOCOUTQ/SIOCOUTQ); 0 if unavailable."""
  try:
    return struct.unpack("i", fcntl.ioctl(sock.fileno(), termios.TIOCOUTQ, b"\0\0\0\0"))[0]
  except (OSError, ValueError, AttributeError):
    return 0


class _Bucket:
  def __init__(self, burst: float, rate: float, now: float):
    self.burst, self.rate, self.tokens, self.at = burst, rate, burst, now

  def take(self, now: float) -> bool:
    self.tokens = min(self.burst, self.tokens + (now - self.at) * self.rate)
    self.at = now
    if self.tokens < 1.0:
      return False
    self.tokens -= 1.0
    return True


class Conn:
  def __init__(self, sock: socket.socket, ip: str, now: float):
    self.sock = sock
    self.ip = ip
    self.reader = W.FrameReader(W.CLIENT_TYPES)
    self.out = bytearray()
    self.session_id = 0           # 0 while the handshake is pending
    self.accepted_at = now
    self.last_rx = now
    self.last_progress = now
    self.bucket = _Bucket(RATE_BURST, RATE_PER_S, now)
    self.seq = 0
    self.last_sent: float | None = None
    self.last_sent_frame = -1
    self.last_sent_status = -1
    self.last_ping_id = 0
    self.writing = False
    self.outq_high_since: float | None = None


class LiveServer:
  def __init__(self, port: int, policy, host: str = "0.0.0.0", mono=time.monotonic, logger=None,
               session_id_fn=new_session_id, outq_fn=kernel_outq):
    self.port = port
    self.host = host
    self.policy = policy
    self.mono = mono
    self.log = logger or (lambda msg: None)
    self.session_id_fn = session_id_fn
    self.outq_fn = outq_fn
    self.listener: socket.socket | None = None
    self.sel: selectors.BaseSelector | None = None
    self.conns: dict[int, Conn] = {}
    self.stats: dict[str, int] = {}
    self._accept_bucket: _Bucket | None = None
    self._last_log: dict[str, float] = {}

  # ---- lifecycle ----
  def open(self) -> bool:
    """Create, bind and register the listener. Any failure leaves nothing open and returns False."""
    if self.listener is not None:
      return True
    s = sel = None
    try:
      s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
      s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
      s.setblocking(False)
      s.bind((self.host, self.port))
      s.listen(4)
      sel = selectors.DefaultSelector()
      sel.register(s, selectors.EVENT_READ, None)
    except (OSError, ValueError) as e:
      for obj in (sel, s):
        try:
          if obj is not None:
            obj.close()
        except OSError:
          pass
      self._count("bind_error")
      self._log_limited("bind", f"listen {self.host}:{self.port} failed ({errno.errorcode.get(getattr(e, 'errno', None), e)})")
      return False
    self.listener, self.sel = s, sel
    self._accept_bucket = _Bucket(ACCEPT_BURST, ACCEPT_PER_S, self.mono())
    self._log_limited("listen", f"listening on TCP {self.host}:{self.port}")
    return True

  @property
  def listening(self) -> bool:
    return self.listener is not None

  @property
  def bound_port(self) -> int | None:
    try:
      return self.listener.getsockname()[1] if self.listener is not None else None
    except OSError:
      return None

  @property
  def sessions(self) -> list[Conn]:
    return [c for c in self.conns.values() if c.session_id]

  def end_sessions(self, reason: int) -> None:
    for c in list(self.conns.values()):
      if c.session_id:
        self._drop(c, W.bye(c.session_id, reason), f"bye_{reason}")

  def close(self, reason: int = W.BYE_STOPPING) -> None:
    """Final frame to every session (best effort, never blocks), then close everything."""
    for c in list(self.conns.values()):
      self._drop(c, W.bye(c.session_id, reason) if c.session_id else None, "closed")
    sel, listener = self.sel, self.listener
    self.sel = self.listener = None
    for obj in (sel, listener):
      try:
        if obj is not None:
          obj.close()
      except OSError:
        pass

  # ---- io ----
  def poll(self, timeout: float) -> None:
    """At most ONE selector round: the caller owns the loop cadence (see LiveDaemon)."""
    if self.sel is None:
      return
    try:
      ready = self.sel.select(max(0.0, timeout))
    except (OSError, ValueError):
      self._count("select_error")
      self._log_limited("select", "selector failed: closing listener, will retry")
      self.close(W.BYE_STOPPING)
      return
    for key, events in ready:
      if key.data is None:
        self._accept()
        continue
      c = key.data
      if not self._alive(c):
        continue
      if events & selectors.EVENT_WRITE:
        self._flush(c)
      if events & selectors.EVENT_READ and self._alive(c):
        self._read(c)
    self._timers()

  def _accept(self) -> None:
    for _ in range(ACCEPT_BURST):
      try:
        sock, addr = self.listener.accept()
      except (BlockingIOError, InterruptedError):
        return
      except OSError:
        self._count("accept_error")
        return
      now = self.mono()
      ip = addr[0] if addr else ""
      if not self._accept_bucket.take(now):
        self._count("accept_rate")
        sock.close()
        continue
      if not self.policy.allowed(ip):
        self._count("address_rejected")  # closed without a single byte
        self._log_limited("addr", f"rejected connection from non-LAN address {ip}")
        sock.close()
        continue
      try:
        sock.setblocking(False)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SNDBUF)
      except OSError:
        sock.close()
        continue
      c = Conn(sock, ip, now)
      if sum(1 for x in self.conns.values() if not x.session_id) >= MAX_PENDING:
        self._count("pending_full")
        self._drop(c, W.reject(W.REJECT_BUSY), None)
        continue
      try:
        self.sel.register(sock, selectors.EVENT_READ, c)
      except (OSError, ValueError, KeyError):
        self._count("register_error")
        sock.close()
        continue
      self.conns[sock.fileno()] = c
      self._count("accepted")

  def _read(self, c: Conn) -> None:
    try:
      data = c.sock.recv(RECV_CHUNK)
    except (BlockingIOError, InterruptedError):
      return
    except OSError:
      self._drop(c, None, "recv_error")
      return
    if not data:
      self._drop(c, None, "peer_closed")
      return
    now = self.mono()
    try:
      frames = c.reader.feed(data)
    except W.FrameError as e:
      self._protocol_error(c, str(e))
      return
    for f in frames:
      if not self._alive(c):
        return
      if not c.bucket.take(now):
        self._drop(c, W.bye(c.session_id, W.BYE_RATE_LIMIT) if c.session_id else None, "rate_limit")
        return
      c.last_rx = now
      if not c.session_id:
        self._handshake(c, f, now)
      elif f.type == W.T_PING:
        sid, ping_id = f.fields
        if sid != c.session_id or ping_id <= c.last_ping_id:
          self._protocol_error(c, "ping")
          return
        c.last_ping_id = ping_id
        self._send(c, W.pong(sid, ping_id), droppable=False)
      else:
        self._protocol_error(c, "unexpected")
        return

  def _handshake(self, c: Conn, f: W.Frame, now: float) -> None:
    if f.type != W.T_HELLO:
      self._protocol_error(c, "not_hello")
      return
    for other in self.sessions:
      if other.ip == c.ip:  # the same phone reconnecting: its old session is dead or about to be
        self._drop(other, W.bye(other.session_id, W.BYE_REPLACED), "replaced")
    if len(self.sessions) >= MAX_SESSIONS:
      self._drop(c, W.reject(W.REJECT_BUSY), "busy")
      return
    nonce = f.fields[0]
    c.session_id = self.session_id_fn()
    c.last_ping_id = 0
    self._count("sessions")
    self._log_limited("session", f"session {c.session_id:08X} from {c.ip} (sessions so far: {self.stats['sessions']})")
    self._send(c, W.welcome(c.session_id, nonce, HEARTBEAT_MS, CLIENT_TIMEOUT_MS), droppable=False)

  def _protocol_error(self, c: Conn, why: str) -> None:
    self._count("protocol_error")
    if c.session_id:
      self._drop(c, W.bye(c.session_id, W.BYE_PROTOCOL_ERROR), None)
    else:
      self._drop(c, W.reject(W.REJECT_VERSION if why == "version" else W.REJECT_BAD_HELLO), None)

  def _send(self, c: Conn, data: bytes, droppable: bool) -> bool:
    if droppable and (c.out or self.outq_fn(c.sock) > OUTQ_SKIP):
      self._count("telemetry_skipped")
      return False
    if c.out:
      if len(c.out) + len(data) > OUT_MAX:
        self._drop(c, None, "slow_reader")
        return False
      c.out += data
      return True
    try:
      n = c.sock.send(data)
    except (BlockingIOError, InterruptedError):
      n = 0
    except OSError:
      self._drop(c, None, "send_error")
      return False
    if n > 0:
      c.last_progress = self.mono()
    if n < len(data):
      if len(data) - n > OUT_MAX:
        self._drop(c, None, "slow_reader")
        return False
      c.out += data[n:]
      self._want_write(c, True)
    return True

  def _flush(self, c: Conn) -> None:
    if not c.out:
      self._want_write(c, False)
      return
    try:
      n = c.sock.send(c.out)
    except (BlockingIOError, InterruptedError):
      return
    except OSError:
      self._drop(c, None, "send_error")
      return
    if n > 0:
      del c.out[:n]
      c.last_progress = self.mono()
    if not c.out:
      self._want_write(c, False)

  def _want_write(self, c: Conn, on: bool) -> None:
    if c.writing == on or self.sel is None or not self._alive(c):
      return
    try:
      self.sel.modify(c.sock, selectors.EVENT_READ | (selectors.EVENT_WRITE if on else 0), c)
      c.writing = on
    except (OSError, ValueError, KeyError):
      self._drop(c, None, "modify_error")

  def _alive(self, c: Conn) -> bool:
    return self.conns.get(c.sock.fileno()) is c

  def _drop(self, c: Conn, last: bytes | None, why: str | None) -> None:
    """Best-effort final frame (never blocks, never queued), then close immediately."""
    if why:
      self._count(why)
    fd = c.sock.fileno()
    if last is not None and not c.out:
      try:
        c.sock.send(last)
      except OSError:
        pass
    if fd >= 0 and self.conns.get(fd) is c:
      del self.conns[fd]
      if self.sel is not None:
        try:
          self.sel.unregister(c.sock)
        except (KeyError, ValueError):
          pass
    try:
      c.sock.close()
    except OSError:
      pass

  def _timers(self) -> None:
    now = self.mono()
    for c in list(self.conns.values()):
      if not c.session_id:
        if now - c.accepted_at > HANDSHAKE_TIMEOUT_S:
          self._drop(c, None, "handshake_timeout")
      elif now - c.last_rx > CLIENT_TIMEOUT_S:
        self._drop(c, W.bye(c.session_id, W.BYE_CLIENT_TIMEOUT), "client_timeout")
      elif c.out and now - c.last_progress > SLOW_S:
        self._drop(c, None, "slow_reader")
      elif self.outq_fn(c.sock) > OUTQ_SKIP:
        # kernel-queue-only backlog (user-space buffer empty): the peer stopped acknowledging
        if c.outq_high_since is None:
          c.outq_high_since = now
        elif now - c.outq_high_since > SLOW_S:
          self._drop(c, None, "slow_reader")
      else:
        c.outq_high_since = None

  # ---- telemetry ----
  def publish(self, fields: dict) -> None:
    """Send the current telemetry to every session when due: a new frame or a status change (<= 50 Hz),
    else a heartbeat every HEARTBEAT_S. The first frame follows the WELCOME immediately."""
    now = self.mono()
    for c in self.sessions:
      since = None if c.last_sent is None else now - c.last_sent
      changed = fields["frame_counter"] != c.last_sent_frame or fields["status"] != c.last_sent_status
      due = since is None or (changed and since >= MIN_INTERVAL_S) or since >= HEARTBEAT_S
      if not due:
        continue
      if c.seq >= W.U32_MAX:
        self._drop(c, W.bye(c.session_id, W.BYE_CAR_CHANGED), "seq_exhausted")
        continue
      data = W.rpm(c.session_id, c.seq + 1, fields["frame_counter"], fields["rpm_raw"], fields["frame_age_ms"],
                   fields["status"], fields["flags"])
      if self._send(c, data, droppable=True):
        c.seq += 1
        c.last_sent = now
        c.last_sent_frame = fields["frame_counter"]
        c.last_sent_status = fields["status"]

  # ---- diagnostics ----
  def _count(self, key: str) -> None:
    self.stats[key] = self.stats.get(key, 0) + 1

  def _log_limited(self, key: str, msg: str) -> None:
    """At most one line per key per LOG_PERIOD_S; a failing logger never breaks the loop."""
    now = self.mono()
    last = self._last_log.get(key)
    if last is None or now - last >= LOG_PERIOD_S:
      self._last_log[key] = now
      try:
        self.log(msg)
      except Exception:
        pass

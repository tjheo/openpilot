import socket
import time
from types import SimpleNamespace

from openpilot.selfdrive.svx_live import wire as W


def frame(address, src, dat):
  return SimpleNamespace(address=address, src=src, dat=dat)


def event(t_ns, frames, valid=True):
  return SimpleNamespace(logMonoTime=t_ns, valid=valid, can=frames)


def ems(raw, fault=False):
  return bytes([0x02 if fault else 0x00, 0x00, raw & 0xFF, raw >> 8, 0, 0, 0, 0])


class FakeClock:
  def __init__(self, t=1000.0):
    self.t = t

  def __call__(self):
    return self.t

  def adv(self, s):
    self.t += s


class Client:
  """Minimal blocking test client speaking SVXL over a real socket."""

  def __init__(self, port, host="127.0.0.1", src="127.0.0.1", timeout=2.0):
    self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
      self.sock.bind((src, 0))
      self.sock.settimeout(timeout)
      self.sock.connect((host, port))
    except OSError:
      self.sock.close()
      raise
    self.reader = W.FrameReader(W.SERVER_TYPES, max_buffer=1 << 16)
    self.pending = []

  def send(self, data):
    self.sock.sendall(data)

  def recv_frames(self, pump, until=lambda fs: bool(fs), timeout=2.0):
    """Collect frames while `pump()` drives the server, until `until(frames)` or timeout/EOF."""
    got, end = list(self.pending), time.monotonic() + timeout
    self.pending = []
    self.sock.setblocking(False)
    try:
      while not until(got) and time.monotonic() < end:
        pump()
        try:
          data = self.sock.recv(4096)
        except BlockingIOError:
          continue
        except ConnectionError:
          return got, True
        if not data:
          return got, True
        got += self.reader.feed(data)
    finally:
      self.sock.setblocking(True)
    return got, False

  def close(self):
    self.sock.close()

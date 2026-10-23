from types import SimpleNamespace

KEY = b"0123456789abcdef" * 4  # 64 ASCII bytes, test-only


def frame(address, src, dat):
  return SimpleNamespace(address=address, src=src, dat=dat)


def event(t_ns, frames, valid=True):
  return SimpleNamespace(logMonoTime=t_ns, valid=valid, can=frames)


def ems(raw, fault=False):
  return bytes([0x02 if fault else 0x00, 0x00, raw & 0xFF, raw >> 8, 0, 0, 0, 0])

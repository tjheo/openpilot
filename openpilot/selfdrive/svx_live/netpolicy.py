"""Which peer addresses may talk to the listener. Dotted IPv4 only; nothing is ever resolved via DNS.

Allowed: RFC1918 private unicast (10/8, 172.16/12, 192.168/16) and the 100.64/10 shared range
(Tailscale tailnet addresses). Rejected: public, loopback, link-local, multicast, broadcast, 0/8,
IPv6, and the all-zeros/all-ones host of each range. Loopback is allowed only when a test passes
allow_loopback=True; production code never does.
"""
from __future__ import annotations

import ipaddress

ALLOWED_NETS = tuple(ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"))
LOOPBACK = ipaddress.IPv4Network("127.0.0.0/8")


def parse_ipv4(text) -> ipaddress.IPv4Address | None:
  """Strict dotted-quad parse (no leading zeros, no hostnames, no IPv6, no IPv4-mapped forms)."""
  if not isinstance(text, str) or len(text) > 15:
    return None
  parts = text.split(".")
  if len(parts) != 4 or any(not p.isdigit() or not p.isascii() or len(p) > 3 or (len(p) > 1 and p[0] == "0") for p in parts):
    return None
  if any(int(p) > 255 for p in parts):
    return None
  return ipaddress.IPv4Address(text)


class AddressPolicy:
  def __init__(self, allow_loopback: bool = False):
    self.allow_loopback = allow_loopback

  def allowed(self, text) -> bool:
    ip = parse_ipv4(text)
    if ip is None:
      return False
    if self.allow_loopback and ip in LOOPBACK:
      return True
    for net in ALLOWED_NETS:
      if ip in net:
        return ip != net.network_address and ip != net.broadcast_address
    return False

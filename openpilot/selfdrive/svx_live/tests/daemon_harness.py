"""Child-process harness for test_svx_daemon: the real LiveDaemon + LiveServer + signal handling, with file-backed
fake params (the test flips the toggle by rewriting a file) and a fake CAN source. Loopback is allowed only here."""
import os
import sys
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")))

from openpilot.selfdrive.svx_live.netpolicy import AddressPolicy
from openpilot.selfdrive.svx_live.profile import CarSelection
from openpilot.selfdrive.svx_live.server import LiveServer
from openpilot.selfdrive.svx_live.svx_lived import LiveDaemon, clock_boottime_ns, install_stop_handlers

TOGGLE, PORTFILE = sys.argv[1], sys.argv[2]


class Sel:
  def read(self):
    return CarSelection("hyundai", "KIA_SELTOS", 0)


class Src:
  def poll(self):
    t = clock_boottime_ns()
    return t, bytes([0, 0, 0x00, 0x32, 0, 0, 0, 0])  # 0x3200 * 0.25 = 3200 rpm

  def close(self):
    print("source closed", flush=True)


def enabled():
  with open(TOGGLE) as f:
    return f.read().strip() == "1"


stop = threading.Event()
install_stop_handlers(stop)
server = LiveServer(0, AddressPolicy(allow_loopback=True), host="127.0.0.1", logger=lambda m: print(m, flush=True))
assert server.open()
with open(PORTFILE + ".tmp", "w") as f:
  f.write(str(server.bound_port))
os.replace(PORTFILE + ".tmp", PORTFILE)
sys.exit(LiveDaemon(server, enabled, Sel(), lambda prof: Src(), logger=lambda m: print(m, flush=True)).run(stop))

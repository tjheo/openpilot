import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SUBTREE = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(os.path.dirname(SUBTREE))
sys.path.insert(0, SUBTREE)
if REPO_ROOT not in sys.path:
  sys.path.append(REPO_ROOT)

# Hardware-free sentinel: any in-process attempt to reach native messaging, Params or Panda/USB fails
# with ImportError instead of touching a real device, even on a fully built checkout.
BLOCKED_MODULES = ("msgq", "openpilot.cereal.messaging", "openpilot.common.params", "panda", "usb1", "cereal.messaging")
for _name in BLOCKED_MODULES:
  sys.modules[_name] = None

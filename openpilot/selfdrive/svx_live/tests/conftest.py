import os
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
if REPO_ROOT not in sys.path:
  sys.path.insert(0, REPO_ROOT)

# Native messaging/Params/Panda must never be reached from these tests. Blocked per test (restored
# afterwards) so other packages collected in the same pytest process are unaffected.
BLOCKED_MODULES = ("msgq", "openpilot.cereal.messaging", "openpilot.common.params", "openpilot.common.params_pyx",
                   "panda", "usb1", "cereal.messaging")


@pytest.fixture(autouse=True)
def _block_native_stack(monkeypatch):
  for name in BLOCKED_MODULES:
    monkeypatch.setitem(sys.modules, name, None)
  yield

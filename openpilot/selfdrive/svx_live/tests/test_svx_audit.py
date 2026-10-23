"""Static audit: svx_live is a read-only subscriber with a telemetry-only listener, and the change outside its
package is limited to the narrow manager/params/settings/docs integration (ported from tools/svx_rpm test_audit.py)."""
import ast
import os
import re
import shutil
import subprocess

import pytest

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.abspath(os.path.join(PKG, "..", "..", ".."))
SOURCES = [os.path.join(PKG, f) for f in sorted(os.listdir(PKG)) if f.endswith(".py")]

STDLIB = {"__future__", "collections", "dataclasses", "errno", "fcntl", "ipaddress", "os", "selectors", "signal", "socket",
          "struct", "sys", "termios", "threading", "time", "zlib"}
OPENPILOT_ALLOWED = {"openpilot.cereal", "openpilot.cereal.messaging", "openpilot.common.params", "openpilot.common.swaglog"}
FORBIDDEN = [r"\bsendcan\b", r"pub_sock", r"PubMaster", r"\bPanda\b", r"panda\.", r"usb1?\b", r"AF_CAN", r"SOCK_RAW", r"SOCK_DGRAM",
             r"SO_BROADCAST", r"IP_MULTICAST", r"socketcan", r"can_send", r"\.put\(", r"put_bool", r"put_nonblocking", r"\.remove\(",
             r"safety", r"setSafety", r"\buds\b", r"isotp", r"\bobd\b", r"subprocess", r"\bexec\(", r"\beval\(", r"carControl",
             r"\.connect\(", r"sendto", r"getaddrinfo", r"gethostbyname", r"os\.system", r"popen", r"pickle", r"marshal"]
ALLOWED_CHANGES = ("openpilot/selfdrive/svx_live/", "tools/svx_rpm/")
ALLOWED_FILES = {"openpilot/system/manager/process_config.py", "openpilot/common/params_keys.h",
                 "openpilot/selfdrive/ui/layouts/settings/toggles.py", "openpilot/selfdrive/ui/mici/layouts/settings/toggles.py",
                 "openpilot/selfdrive/ui/translations/app_ko.po", "docs/user/ko/settings.md", "docs/user/en/settings.md"}


def code_only(path):
  with open(path, encoding="utf-8") as f:
    tree = ast.parse(f.read())
  for node in ast.walk(tree):  # drop docstrings so prose cannot hide/trigger matches
    if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body and isinstance(node.body[0], ast.Expr) \
       and isinstance(getattr(node.body[0], "value", None), ast.Constant) and isinstance(node.body[0].value.value, str):
      node.body = node.body[1:] or [ast.Pass()]
  return tree, ast.unparse(tree)


@pytest.mark.parametrize("path", SOURCES, ids=os.path.basename)
def test_imports_are_allowlisted(path):
  tree, _ = code_only(path)
  for node in ast.walk(tree):
    if isinstance(node, ast.Import):
      names = [a.name for a in node.names]
    elif isinstance(node, ast.ImportFrom):
      if node.level:
        continue
      names = [node.module]
    else:
      continue
    for n in names:
      assert n.split(".")[0] in STDLIB or n in OPENPILOT_ALLOWED, f"{os.path.basename(path)} imports {n}"


@pytest.mark.parametrize("path", SOURCES, ids=os.path.basename)
def test_no_write_actuation_or_outbound_apis(path):
  _, src = code_only(path)
  for pat in FORBIDDEN:
    assert not re.search(pat, src, re.I), f"{os.path.basename(path)} contains {pat}"


def test_only_subscribes_to_can_and_reads_known_params():
  srcs = "\n".join(code_only(p)[1] for p in SOURCES)
  assert re.findall(r"sub_sock\(([^,)]*)", srcs) == ["'can'"]
  gets = set(re.findall(r"params\.get(?:_bool)?\(([^)]*)\)", srcs))
  assert gets == {"'CarParams'", "PORT_PARAM", "ENABLED_PARAM"}, gets
  assert "bind((self.host, self.port))" in srcs and srcs.count(".bind(") == 1  # one listener, nothing else


def test_server_has_no_command_channel():
  """Clients may only send HELLO (once) and PING; everything else ends the connection."""
  _, src = code_only(os.path.join(PKG, "server.py"))
  handled = set(re.findall(r"f\.type [!=]= W\.(T_[A-Z]+)", src))
  assert handled == {"T_HELLO", "T_PING"}, handled
  _, wire = code_only(os.path.join(PKG, "wire.py"))
  assert "CLIENT_TYPES = frozenset({T_HELLO, T_PING})" in wire


@pytest.mark.skipif(shutil.which("git") is None or not os.path.isdir(os.path.join(REPO, ".git")), reason="no git checkout")
def test_change_is_confined_to_narrow_integration():
  out = subprocess.run(["git", "-C", REPO, "status", "--porcelain", "--untracked-files=all"],
                       capture_output=True, text=True, check=True).stdout
  paths = []
  for line in out.splitlines():
    p = line[3:]
    paths += [x.strip() for x in p.split(" -> ")] if " -> " in p else [p.strip()]
  outside = [p for p in paths if not (p.startswith(ALLOWED_CHANGES) or p in ALLOWED_FILES) or "__pycache__" in p]
  assert outside == [], outside
  # integration files only gain lines (no existing behaviour removed), except the mici widget list refactor
  for f, max_removed in (("openpilot/system/manager/process_config.py", 0), ("openpilot/common/params_keys.h", 0),
                         ("openpilot/selfdrive/ui/layouts/settings/toggles.py", 0), ("openpilot/selfdrive/ui/translations/app_ko.po", 0),
                         ("openpilot/selfdrive/ui/mici/layouts/settings/toggles.py", 3)):
    stat = subprocess.run(["git", "-C", REPO, "diff", "--numstat", "HEAD", "--", f], capture_output=True, text=True, check=True).stdout
    if stat:
      added, removed, _ = stat.split("\t")
      assert int(removed) <= max_removed, (f, stat)

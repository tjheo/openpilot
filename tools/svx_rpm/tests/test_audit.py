"""Static audit: the companion is a read-only subscriber and this change touches only tools/svx_rpm/."""
import ast
import os
import re
import shutil
import subprocess

import pytest

SUBTREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(os.path.dirname(SUBTREE))
SOURCES = [os.path.join(SUBTREE, "svx_rpm.py")] + [
  os.path.join(SUBTREE, "svxrpm", f) for f in sorted(os.listdir(os.path.join(SUBTREE, "svxrpm"))) if f.endswith(".py")]

STDLIB = {"__future__", "argparse", "collections", "dataclasses", "errno", "hashlib", "hmac", "json", "math", "os", "re", "secrets",
          "signal", "socket", "stat", "struct", "sys", "threading", "time", "typing", "zlib"}
OPENPILOT_ALLOWED = {"openpilot.cereal", "openpilot.cereal.messaging", "openpilot.common.params"}
FORBIDDEN = [r"\bsendcan\b", r"pub_sock", r"PubMaster", r"\bPanda\b", r"panda\.", r"usb1?\b", r"AF_CAN", r"SOCK_RAW",
             r"SO_BROADCAST", r"socketcan", r"can_send", r"\.put\(", r"put_bool", r"put_nonblocking", r"\.remove\(",
             r"safety", r"setSafety", r"uds", r"isotp", r"obd", r"subprocess", r"manager", r"\bexec\(", r"\beval\(",
             r"carControl", r"\bsetsockopt\b", r"\bbind\("]


def code_only(path):
  tree = ast.parse(open(path).read())
  for node in ast.walk(tree):  # drop docstrings so prose cannot hide/trigger matches
    if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body and isinstance(node.body[0], ast.Expr) \
       and isinstance(getattr(node.body[0], "value", None), ast.Constant) and isinstance(node.body[0].value.value, str):
      node.body = node.body[1:] or [ast.Pass()]
  return tree, ast.unparse(tree) if hasattr(ast, "unparse") else open(path).read()


@pytest.mark.parametrize("path", SOURCES, ids=os.path.basename)
def test_imports_are_allowlisted(path):
  tree, _ = code_only(path)
  for node in ast.walk(tree):
    if isinstance(node, ast.Import):
      names = [a.name for a in node.names]
    elif isinstance(node, ast.ImportFrom):
      if node.level:  # relative import inside svxrpm
        continue
      names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
      names = [names[0]] if names[0].startswith(("openpilot", "svxrpm")) else names[:1]
    else:
      continue
    for n in names:
      top = n.split(".")[0]
      assert top in STDLIB or n in OPENPILOT_ALLOWED or top == "svxrpm", f"{os.path.basename(path)} imports {n}"


@pytest.mark.parametrize("path", SOURCES, ids=os.path.basename)
def test_no_write_or_actuation_apis(path):
  _, src = code_only(path)
  for pat in FORBIDDEN:
    assert not re.search(pat, src, re.I), f"{os.path.basename(path)} contains {pat}"


def test_only_subscribes_to_can_and_reads_carparams():
  srcs = "\n".join(code_only(p)[1] for p in SOURCES)
  assert re.findall(r"sub_sock\(([^,)]*)", srcs) == ["'can'"]
  assert re.findall(r"_params\.get\(([^)]*)\)", srcs) == ["'CarParams'"]


@pytest.mark.skipif(shutil.which("git") is None or not os.path.isdir(os.path.join(REPO_ROOT, ".git")), reason="no git checkout")
def test_change_is_confined_to_subtree():
  out = subprocess.run(["git", "-C", REPO_ROOT, "status", "--porcelain", "--untracked-files=all"],
                       capture_output=True, text=True, check=True).stdout
  paths = [line[3:] for line in out.splitlines() if line.strip()]
  outside = [p for p in paths if not p.startswith("tools/svx_rpm/") or "__pycache__" in p]
  assert outside == [], outside

"""CLI run/scan paths, fully isolated: a copied tool tree next to a STUB `openpilot` package, run with
`python -I` (no PYTHONPATH/user site), so a real built checkout or /data/openpilot is never imported."""
import importlib.util
import os
import shutil
import subprocess
import sys
import textwrap

import pytest

from svxrpm.config import write_new_config

SUBTREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUB_MESSAGING = textwrap.dedent('''
  import os
  open(os.environ["SVX_TEST_SENTINEL"], "a").write("messaging-imported\\n")
  raise ImportError("stub: native msgq intentionally unavailable")
''')


@pytest.fixture
def tree(tmp_path):
  root = tmp_path / "root"
  app = root / "tools" / "svx_rpm"
  shutil.copytree(SUBTREE, app, ignore=shutil.ignore_patterns("tests", "__pycache__"))
  (root / "openpilot" / "cereal").mkdir(parents=True)
  (root / "openpilot" / "__init__.py").write_text("")
  (root / "openpilot" / "cereal" / "__init__.py").write_text("STUB = True\n")
  (root / "openpilot" / "cereal" / "messaging.py").write_text(STUB_MESSAGING)
  cfg = str(tmp_path / "cfg" / "config.json")
  key = write_new_config(cfg, (str(root),), "192.168.43.1", "kia_seltos_2021_ems11", 0, 47816)
  sentinel = tmp_path / "sentinel"
  env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
  env["SVX_TEST_SENTINEL"] = str(sentinel)

  def run(*args):
    return subprocess.run([sys.executable, "-I", str(app / "svx_rpm.py"), *args], capture_output=True, text=True,
                          timeout=30, env=env, cwd=str(tmp_path))
  return run, cfg, key, sentinel


@pytest.mark.parametrize("cmd", [["run", "--duration", "1"], ["run", "--dry-run"], ["scan", "--seconds", "1"]])
def test_run_and_scan_use_stub_and_fail_cleanly(tree, cmd):
  run, cfg, key, sentinel = tree
  r = run(*cmd, *(["--config", cfg] if cmd[0] == "run" else []))
  assert r.returncode == 2 and "messaging unavailable" in r.stderr and "Traceback" not in r.stderr
  assert "stub: native msgq" in r.stderr
  assert sentinel.read_text() == "messaging-imported\n"  # proves the stub, not a real msgq, was reached
  assert key not in r.stdout + r.stderr


@pytest.mark.parametrize("val", ["nan", "inf", "-inf", "-1", "1e9", "abc", ""])
def test_bad_duration_rejected_before_opening_stack(tree, val):
  run, cfg, _, sentinel = tree
  r = run("run", "--config", cfg, "--duration", val)
  assert r.returncode == 2 and not sentinel.exists()


@pytest.mark.parametrize("val", ["nan", "inf", "0", "0.5", "121", "-3"])
def test_bad_scan_seconds_rejected_before_opening_stack(tree, val):
  run, _, _, sentinel = tree
  r = run("scan", "--seconds", val)
  assert r.returncode == 2 and not sentinel.exists()


def test_in_process_sentinel_blocks_native_modules():
  for name in ("msgq", "openpilot.cereal.messaging", "openpilot.common.params", "panda"):
    with pytest.raises(ImportError):
      __import__(name)


# ---- resource cleanup on injected failures (in-process, fakes only) ----

def load_entry():
  spec = importlib.util.spec_from_file_location("svx_rpm_entry", os.path.join(SUBTREE, "svx_rpm.py"))
  mod = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(mod)
  return mod


class FakeSrc:
  def __init__(self, *a):
    self.closed = False
    FakeSrc.last = self

  def close(self):
    self.closed = True


def test_open_stack_closes_source_when_params_fails(monkeypatch, capsys):
  entry = load_entry()
  import svxrpm.cereal_source as cs

  def boom():
    raise ImportError("no params")
  monkeypatch.setattr(cs, "CerealCanSource", FakeSrc)
  monkeypatch.setattr(cs, "ParamsCarSelection", boom)
  assert entry.open_stack(0x316, 0) is None
  assert FakeSrc.last.closed and "messaging unavailable" in capsys.readouterr().err


def test_cmd_run_closes_source_when_udp_socket_fails(monkeypatch, tmp_path, capsys):
  entry = load_entry()
  import svxrpm.companion as comp
  cfg = str(tmp_path / "c" / "config.json")
  key = write_new_config(cfg, (str(tmp_path / "nothing"),), "192.168.43.1", "kia_seltos_2021_ems11", 0, 47816)
  src = FakeSrc()
  monkeypatch.setattr(entry, "open_stack", lambda a, b: (src, object()))
  monkeypatch.setattr(entry, "lower_priority", lambda: None)
  monkeypatch.setattr(entry, "install_stop_handlers", lambda stop: None)

  def no_socket(*a):
    raise OSError(24, "Too many open files")
  monkeypatch.setattr(comp, "UdpOut", no_socket)
  assert entry.main(["run", "--config", cfg]) == 2
  out = capsys.readouterr()
  assert src.closed and "cannot create UDP socket" in out.err and key not in out.out + out.err

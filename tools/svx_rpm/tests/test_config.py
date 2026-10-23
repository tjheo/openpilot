import json
import os
import stat
import subprocess
import sys
import threading

import pytest

from svxrpm.config import ConfigError, load_config, parse_config, validate_target_ip, write_new_config

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
ENTRY = os.path.join(os.path.dirname(HERE), "svx_rpm.py")
GOOD = {"version": 1, "profile": "kia_seltos_2021_ems11", "bus": 0, "target_ip": "192.168.43.20", "port": 47816,
        "key_hex": "ab" * 32}


def write(path, text, mode=0o600):
  with open(path, "w") as f:
    f.write(text)
  os.chmod(path, mode)
  return str(path)


def test_valid_and_key_is_ascii_hex_text():
  c = parse_config(dict(GOOD))
  assert c.key == ("ab" * 32).encode("ascii") and len(c.key) == 64
  assert "abab" not in repr(c) and "abab" not in str(c)


@pytest.mark.parametrize("field,value", [
  ("version", 2), ("version", True), ("profile", "kia_seltos_canfd"), ("profile", 1), ("bus", 1), ("bus", 128),
  ("bus", True), ("bus", 0.0), ("bus", "0"), ("port", 80), ("port", 70000), ("port", True),
  ("key_hex", ""), ("key_hex", "AB" * 32), ("key_hex", "ab" * 31), ("key_hex", "ab" * 33), ("key_hex", "zz" * 32),
  ("key_hex", None), ("target_ip", None),
])
def test_rejects_bad_fields(field, value):
  d = dict(GOOD)
  d[field] = value
  with pytest.raises(ConfigError):
    parse_config(d)


@pytest.mark.parametrize("ip", ["8.8.8.8", "127.0.0.1", "169.254.1.2", "100.64.0.1", "192.168.1.255", "192.168.1.0",
                                "224.0.0.1", "255.255.255.255", "0.0.0.0", "172.32.0.1", "192.168.01.2", "::1",
                                "192.168.1", "192.168.1.256", " 192.168.1.2", "phone.local"])
def test_rejects_non_private_unicast_targets(ip):
  with pytest.raises(ConfigError):
    validate_target_ip(ip)


@pytest.mark.parametrize("ip", ["10.0.0.5", "172.16.3.4", "172.31.255.254", "192.168.43.1"])
def test_accepts_private_unicast(ip):
  assert validate_target_ip(ip) == ip


def test_missing_and_unknown_fields_not_echoed():
  d = dict(GOOD)
  d["SECRET_PASTED_abcdef0123"] = "x"
  with pytest.raises(ConfigError) as ei:
    parse_config(d)
  assert "SECRET_PASTED" not in str(ei.value) and "abab" not in str(ei.value)
  d = dict(GOOD)
  del d["key_hex"]
  with pytest.raises(ConfigError, match="key_hex"):
    parse_config(d)


def test_file_checks(tmp_path):
  p = write(tmp_path / "c.json", json.dumps(GOOD))
  assert load_config(p, REPO_ROOT).port == 47816
  os.chmod(p, 0o644)
  with pytest.raises(ConfigError, match="chmod 600"):
    load_config(p, REPO_ROOT)
  os.chmod(p, 0o640)
  with pytest.raises(ConfigError):
    load_config(p, REPO_ROOT)


def test_duplicate_keys_rejected(tmp_path):
  text = json.dumps(GOOD)[:-1] + ', "bus": 0}'
  with pytest.raises(ConfigError, match="duplicate"):
    load_config(write(tmp_path / "c.json", text), REPO_ROOT)


def test_bad_json_and_symlink_and_dir_and_missing(tmp_path):
  with pytest.raises(ConfigError):
    load_config(write(tmp_path / "a.json", "{not json"), REPO_ROOT)
  real = write(tmp_path / "real.json", json.dumps(GOOD))
  os.symlink(real, tmp_path / "link.json")
  with pytest.raises(ConfigError):
    load_config(str(tmp_path / "link.json"), REPO_ROOT)
  os.mkdir(tmp_path / "d", 0o700)
  with pytest.raises(ConfigError):
    load_config(str(tmp_path / "d"), REPO_ROOT)
  with pytest.raises(ConfigError):
    load_config(str(tmp_path / "missing.json"), REPO_ROOT)


def test_fifo_does_not_block(tmp_path):
  fifo = tmp_path / "fifo"
  os.mkfifo(fifo, 0o600)
  result = []

  def target():
    with pytest.raises(ConfigError):
      load_config(str(fifo), REPO_ROOT)
    result.append(True)
  t = threading.Thread(target=target)
  t.start()
  t.join(2.0)
  assert not t.is_alive() and result


def test_config_inside_repo_rejected():
  with pytest.raises(ConfigError, match="outside"):
    load_config(os.path.join(REPO_ROOT, "svx.json"), REPO_ROOT)
  with pytest.raises(ConfigError, match="outside"):
    write_new_config(os.path.join(REPO_ROOT, "tools", "x.json"), REPO_ROOT, "192.168.43.1", "kia_seltos_2021_ems11", 0, 47816)


def test_write_new_config(tmp_path):
  p = str(tmp_path / "svx" / "config.json")
  key = write_new_config(p, REPO_ROOT, "192.168.43.1", "kia_seltos_2021_ems11", 0, 47816)
  assert len(key) == 64 and int(key, 16) >= 0
  assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
  assert stat.S_IMODE(os.stat(os.path.dirname(p)).st_mode) == 0o700
  assert load_config(p, REPO_ROOT).key == key.encode()
  with pytest.raises(ConfigError, match="already exists"):
    write_new_config(p, REPO_ROOT, "192.168.43.1", "kia_seltos_2021_ems11", 0, 47816)
  assert load_config(p, REPO_ROOT).key == key.encode()  # untouched
  key2 = write_new_config(str(tmp_path / "other.json"), REPO_ROOT, "192.168.43.1", "kia_seltos_2021_ems11", 0, 47816)
  assert key2 != key


def test_cli_init_check_show_key(tmp_path):
  p = str(tmp_path / "cfg" / "config.json")
  r = subprocess.run([sys.executable, "-I", ENTRY, "init", "--target", "192.168.43.1", "--config", p],
                     capture_output=True, text=True, timeout=30)
  assert r.returncode == 0, r.stderr
  key = r.stdout.strip().splitlines()[-1]
  assert len(key) == 64
  r = subprocess.run([sys.executable, "-I", ENTRY, "check", "--config", p], capture_output=True, text=True, timeout=30)
  assert r.returncode == 0 and key not in r.stdout + r.stderr and "redacted" in r.stdout
  r = subprocess.run([sys.executable, "-I", ENTRY, "show-key", "--config", p], capture_output=True, text=True, timeout=30)
  assert r.returncode == 0 and r.stdout.strip() == key
  r = subprocess.run([sys.executable, "-I", ENTRY, "init", "--target", "8.8.8.8", "--config", str(tmp_path / "x.json")],
                     capture_output=True, text=True, timeout=30)
  assert r.returncode == 2 and not os.path.exists(tmp_path / "x.json")


def test_config_inside_tool_dir_rejected(tmp_path):
  tool = tmp_path / "app"
  tool.mkdir()
  p = write(tool / "config.json", json.dumps(GOOD))
  with pytest.raises(ConfigError, match="outside"):
    load_config(p, (REPO_ROOT, str(tool)))
  assert load_config(write(tmp_path / "ok.json", json.dumps(GOOD)), (REPO_ROOT, str(tool))).bus == 0


def test_cli_standalone_copy_finds_device_root_layout(tmp_path):
  # a copy outside the checkout must still refuse a config inside its own directory
  import shutil
  app = tmp_path / "app"
  shutil.copytree(os.path.dirname(HERE), app, ignore=shutil.ignore_patterns("tests", "__pycache__"))
  r = subprocess.run([sys.executable, "-I", str(app / "svx_rpm.py"), "init", "--target", "192.168.43.1", "--config",
                      str(app / "config.json")], capture_output=True, text=True, timeout=30)
  assert r.returncode == 2 and "outside" in r.stderr and not (app / "config.json").exists()
  r = subprocess.run([sys.executable, "-I", str(app / "svx_rpm.py"), "init", "--target", "192.168.43.1", "--config",
                      str(tmp_path / "cfg" / "config.json")], capture_output=True, text=True, timeout=30)
  assert r.returncode == 0, r.stderr

"""Narrow integration points: manager entry, params defaults, inert imports and the raylib settings toggles."""
import ast
import importlib.util
import os
import re
import subprocess
import sys
import types

import pytest

from openpilot.selfdrive.svx_live.manager_policy import (DEFAULT_PORT, ENABLED_PARAM, PORT_PARAM, params_has_key, parse_port,
                                                         svx_live_should_run)

OP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))   # <repo>/openpilot
REPO = os.path.dirname(OP)


def read(rel):
  with open(os.path.join(OP, rel), encoding="utf-8") as f:
    return f.read()


class FakeParams:
  def __init__(self, values=None, known=(ENABLED_PARAM, PORT_PARAM), raise_get=False):
    self.values = dict(values or {})
    self.known = set(known)
    self.raise_get = raise_get
    self.puts = []
    self.get_calls = 0

  def check_key(self, key):
    if key not in self.known:
      raise KeyError(key)

  def get_bool(self, key, block=False):
    self.get_calls += 1
    if self.raise_get or key not in self.known:
      raise KeyError(key)
    return bool(self.values.get(key, False))

  def put_bool(self, key, val):
    self.check_key(key)
    self.puts.append((key, val))
    self.values[key] = val

  def get(self, key, return_default=False):
    return self.values.get(key, 0)

  def remove(self, key):
    self.values.pop(key, None)


def test_should_run_only_onroad_and_enabled():
  on, off = FakeParams({ENABLED_PARAM: True}), FakeParams()
  assert svx_live_should_run(True, on, None) is True
  assert svx_live_should_run(True, off, None) is False      # default OFF
  p = FakeParams({ENABLED_PARAM: True})
  assert svx_live_should_run(False, p, None) is False and p.get_calls == 0  # offroad: never runs
  assert svx_live_should_run(True, FakeParams(known=()), None) is False   # stale params build: off, no crash
  assert svx_live_should_run(True, FakeParams(raise_get=True), None) is False


@pytest.mark.parametrize("v,exp", [(47816, 47816), (1024, 1024), (65535, 65535), (b"47816", 47816), ("47900", 47900), (" 47816 ", 47816),
                                   (None, None), (0, None), (1023, None), (65536, None), (-1, None), (True, None), ("4e4", None),
                                   ("", None), ("0x10", None), (b"\xff", None), ("٤٧٨١٦", None), (47816.0, None)])
def test_parse_port(v, exp):
  assert parse_port(v) == exp


def test_params_keys_defaults():
  src = read("common/params_keys.h")
  assert re.search(r'\{"SvxLiveEnabled", \{PERSISTENT, BOOL, "0"\}\},', src)
  assert re.search(r'\{"SvxLivePort", \{PERSISTENT, INT, "' + str(DEFAULT_PORT) + r'"\}\},', src)
  assert src.count('"SvxLive') == 2


def test_process_config_entry():
  tree = ast.parse(read("system/manager/process_config.py"))
  calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "PythonProcess"
           and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == "svx_lived"]
  assert len(calls) == 1
  call = calls[0]
  assert [a.value if isinstance(a, ast.Constant) else a.id for a in call.args] == \
         ["svx_lived", "openpilot.selfdrive.svx_live.svx_lived", "svx_live_should_run"]
  assert call.keywords == []  # no restart_if_crash/spawn/enabled overrides: exits are never auto-restarted while off
  imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "openpilot.selfdrive.svx_live.manager_policy"]
  assert [a.name for a in imports[0].names] == ["svx_live_should_run"]
  assert importlib.util.find_spec("openpilot.selfdrive.svx_live.svx_lived") is not None


INERT_CHECK = r"""
import socket, sys, threading, importlib
sys.path.insert(0, sys.argv[1])
class NoSocket(socket.socket):
  def __init__(self, *a, **k):
    raise AssertionError("socket created at import time")
socket.socket = NoSocket
threading.Thread.start = lambda self: (_ for _ in ()).throw(AssertionError("thread at import"))
mods = ["openpilot.selfdrive.svx_live." + m for m in
        ("svx_lived", "server", "wire", "netpolicy", "manager_policy", "rpm_state", "ems11", "profile", "cereal_source",
         "diagnostics")]
for m in mods:
  importlib.import_module(m)
bad = [m for m in sys.modules if m.split(".")[0] in ("msgq", "capnp", "zmq") or m.startswith(("openpilot.common.params",
       "openpilot.cereal", "openpilot.common.swaglog", "openpilot.system"))]
assert not bad, bad
print("INERT_OK", len(mods))
"""


def test_imports_are_inert_like_manager_prepare():
  """PythonProcess.prepare() imports the module even while the toggle is OFF: nothing may happen at import."""
  out = subprocess.run([sys.executable, "-I", "-c", INERT_CHECK, REPO], capture_output=True, text=True, timeout=60,
                       env={"PATH": os.environ.get("PATH", "")})
  assert out.returncode == 0 and "INERT_OK 10" in out.stdout, out.stderr


# ---- raylib settings layouts (C3/C3X big UI and C4 mici UI) with stubbed widget/runtime modules ----

class _Item:
  def __init__(self, title, desc, state, callback, icon):
    self.title, self.description, self.icon = title, desc, icon
    self.action_item = types.SimpleNamespace(state=state, enabled=True, callback=callback)
    self.action_item.set_enabled = lambda v: setattr(self.action_item, "enabled", v)
    self.action_item.set_state = lambda v: setattr(self.action_item, "state", v)
    self.action_item.get_state = lambda: self.action_item.state
    self.action_item.set_selected_button = lambda i: None

  def set_description(self, d):
    self.description = d

  def set_icon(self, i):
    self.icon = i


class _Big:
  def __init__(self, text, param, toggle_callback=None):
    self.text, self.param, self.cb, self.visible = text, param, toggle_callback, True
    self.params = _STUB["params"]
    self.checked = self.params.get_bool(param)

  def set_checked(self, v):
    self.checked = v

  def set_enabled(self, v):
    pass

  def set_visible(self, v):
    self.visible = v

  def click(self):  # what BigParamControl._handle_mouse_release does
    self.checked = not self.checked
    self.params.put_bool(self.param, self.checked)


_STUB = {}


def _install_stubs(monkeypatch, params):
  _STUB["params"] = params
  ui_state = types.SimpleNamespace(params=params, engaged=False, CP=None, started=False, personality=0,
                                   sm=types.SimpleNamespace(updated={"selfdriveState": False}),
                                   add_engaged_transition_callback=lambda cb: None, update_params=lambda: None,
                                   has_longitudinal_control=True)

  def mod(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    monkeypatch.setitem(sys.modules, name, m)

  class Widget:
    def __init__(self):
      pass

    def show_event(self):
      pass

  class Scroller:
    def __init__(self, items=None, **kw):
      self.items = list(items or [])

    def add_widgets(self, ws):
      self.items += ws

    def show_event(self):
      pass

  class NavScroller(Widget):
    def __init__(self):
      self._scroller = Scroller()

    def _update_state(self):
      pass

  mod("openpilot.common.params", Params=lambda: params, UnknownKeyName=KeyError)
  mod("openpilot.system.ui.widgets", Widget=Widget, DialogResult=types.SimpleNamespace(CONFIRM=1))
  mod("openpilot.system.ui.widgets.list_view", toggle_item=lambda t, d, s, callback=None, icon="": _Item(t, d, s, callback, icon),
      multiple_button_item=lambda *a, **k: _Item("p", "", 0, None, ""))
  mod("openpilot.system.ui.widgets.scroller_tici", Scroller=Scroller)
  mod("openpilot.system.ui.widgets.scroller", NavScroller=NavScroller)
  mod("openpilot.system.ui.widgets.confirm_dialog", ConfirmDialog=object)
  mod("openpilot.system.ui.lib.application", gui_app=types.SimpleNamespace(set_show_touches=lambda v: None, set_show_fps=lambda v: None,
                                                                           push_widget=lambda w: None))
  mod("openpilot.system.ui.lib.multilang", tr=lambda s: s, tr_noop=lambda s: s)
  mod("openpilot.selfdrive.ui.ui_state", ui_state=ui_state)
  mod("openpilot.selfdrive.ui.mici.widgets.button", BigParamControl=_Big, BigMultiParamToggle=lambda *a, **k: _Big(a[0], "LongitudinalPersonality"))
  mod("openpilot.selfdrive.ui.layouts.settings.common", restart_needed_callback=lambda *a: None)


def _load(rel, name):
  spec = importlib.util.spec_from_file_location(name, os.path.join(OP, rel))
  m = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(m)
  return m


KNOWN = ("OpenpilotEnabledToggle", "ExperimentalMode", "DisengageOnAccelerator", "IsLdwEnabled", "AlwaysOnDM", "RecordFront",
         "RecordAudio", "IsMetric", "LongitudinalPersonality", "IsReleaseBranch", "ExperimentalModeConfirmed", "RecordFrontLock",
         "ShowDebugInfo", "OnroadCycleRequested", ENABLED_PARAM, PORT_PARAM)


def test_big_ui_toggle_visible_default_off_and_writes_param(monkeypatch):
  params = FakeParams(known=KNOWN)
  _install_stubs(monkeypatch, params)
  m = _load("selfdrive/ui/layouts/settings/toggles.py", "svx_test_big_toggles")
  layout = m.TogglesLayout()
  t = layout._toggles[ENABLED_PARAM]
  assert t in layout._scroller.items and t.title() == "SVX Live RPM to Phone"
  assert t.action_item.state is False  # default OFF
  assert "47816" in t.description() and "no key" in t.description()
  t.action_item.callback(True)
  assert params.puts == [(ENABLED_PARAM, True)] and "OnroadCycleRequested" not in params.values  # no restart needed
  params.values[ENABLED_PARAM] = False  # external change is mirrored on show
  layout._update_toggles()
  assert t.action_item.state is False


def test_big_ui_hides_toggle_on_stale_params_build(monkeypatch):
  params = FakeParams(known=[k for k in KNOWN if not k.startswith("SvxLive")])
  _install_stubs(monkeypatch, params)
  layout = _load("selfdrive/ui/layouts/settings/toggles.py", "svx_test_big_toggles2").TogglesLayout()
  assert ENABLED_PARAM not in layout._toggles


def test_mici_c4_toggle_visible_default_off_and_writes_param(monkeypatch):
  params = FakeParams(known=KNOWN)
  _install_stubs(monkeypatch, params)
  layout = _load("selfdrive/ui/mici/layouts/settings/toggles.py", "svx_test_mici_toggles").TogglesLayoutMici()
  [t] = [w for w in layout._scroller.items if getattr(w, "param", None) == ENABLED_PARAM]
  assert t.checked is False and t.text == "svx live rpm to phone"
  t.click()
  assert params.puts == [(ENABLED_PARAM, True)]
  params.values[ENABLED_PARAM] = False
  layout._update_toggles()
  assert t.checked is False and (ENABLED_PARAM, t) in layout._refresh_toggles


def test_mici_hides_toggle_on_stale_params_build(monkeypatch):
  params = FakeParams(known=[k for k in KNOWN if not k.startswith("SvxLive")])
  _install_stubs(monkeypatch, params)
  layout = _load("selfdrive/ui/mici/layouts/settings/toggles.py", "svx_test_mici_toggles2").TogglesLayoutMici()
  assert not any(getattr(w, "param", None) == ENABLED_PARAM for w in layout._scroller.items)


def test_both_c4_ui_paths_are_covered():
  """ui.py picks MainLayout when gui_app.big_ui() (tici/tizi or BIG=1) else MiciMainLayout (C4): both settings
  paths reach a toggles layout that now contains the SVX toggle."""
  ui = read("selfdrive/ui/ui.py")
  assert "MainLayout()" in ui and "MiciMainLayout()" in ui and "if BIG_UI:" in ui
  assert "TogglesLayout()" in read("selfdrive/ui/layouts/settings/settings.py")
  assert "TogglesLayoutMici()" in read("selfdrive/ui/mici/layouts/settings/settings.py")
  assert "def big_ui()" in read("system/ui/lib/application.py")


def test_korean_translation_entries_parse():
  src = read("system/ui/lib/multilang.py")
  tree = ast.parse(src)
  ns = {"re": re}
  for node in tree.body:  # only the pure .po parser helpers, without importing the runtime module
    if isinstance(node, ast.FunctionDef) and node.name in ("_parse_quoted", "load_translations"):
      exec(compile(ast.Module([node], []), "multilang", "exec"), ns)
  from pathlib import Path
  tr, _ = ns["load_translations"](Path(OP, "selfdrive/ui/translations/app_ko.po"))
  big = _load_desc()
  assert tr["SVX Live RPM to Phone"] == "SVX 실시간 RPM 폰 전송"
  assert "47816" in tr[big] and "키가 없으므로" in tr[big]


def _load_desc():
  tree = ast.parse(read("selfdrive/ui/layouts/settings/toggles.py"))
  for node in ast.walk(tree):
    if isinstance(node, ast.Dict):
      for k, v in zip(node.keys, node.values, strict=True):
        if isinstance(k, ast.Name) and k.id == "SVX_LIVE_PARAM":
          return _fold(v.args[0])
  raise AssertionError("description not found")


def _fold(node):
  """String literal or "a" + "b" concatenation only."""
  if isinstance(node, ast.Constant) and isinstance(node.value, str):
    return node.value
  assert isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)
  return _fold(node.left) + _fold(node.right)


def test_params_has_key():
  assert params_has_key(FakeParams(), ENABLED_PARAM) and not params_has_key(FakeParams(known=()), ENABLED_PARAM)

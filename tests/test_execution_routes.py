"""Execution outcomes and refusal paths, without requiring a Windows desktop."""
from types import SimpleNamespace

import pytest

from gua.actions import Action, parse_action
from gua.env.a11y import finalize, uia_raw
from gua.env.uia_execution import ObservedControl, activate_control
from gua.recovery import RecoveryPolicy
from gua.verify import Check, Verdict
from fakes import fake_pyautogui, import_platform_module


class Rect:
    left, top, right, bottom = 20, 30, 120, 70
    def width(self): return self.right - self.left
    def height(self): return self.bottom - self.top


class Control:
    Name, AutomationId, IsPassword = "Continue", "continue", False
    IsEnabled, IsOffscreen, HasKeyboardFocus = True, False, False
    BoundingRectangle = Rect()
    def __init__(self, native="Button", partial_failure=False):
        self.ControlTypeName = native + "Control"
        self.identity, self.activations, self.selected, self.checked = (1, 2, 3), 0, False, False
        self.partial_failure = partial_failure
    def GetRuntimeId(self): return self.identity
    def GetValuePattern(self): return None
    def GetInvokePattern(self): return self
    def GetTogglePattern(self): return self
    def GetSelectionItemPattern(self): return self
    @property
    def ToggleState(self): return int(self.checked)
    def Invoke(self):
        self.activations += 1
        if self.partial_failure: raise RuntimeError("connection lost after activation")
    def Toggle(self): self.checked = not self.checked
    def Select(self): self.selected = True
    def SetFocus(self): self.HasKeyboardFocus = True


def observed(ctrl):
    element = finalize([uia_raw(ctrl)], (800, 600))[0][0]
    return ObservedControl(ctrl, tuple(ctrl.GetRuntimeId()), element)


@pytest.mark.parametrize("native,route,state", [("Button", "uia_invoke", "activations"),
    ("CheckBox", "uia_toggle", "checked"), ("RadioButton", "uia_select", "selected"),
    ("Edit", "uia_focus", "HasKeyboardFocus")])
def test_native_pattern_changes_the_application_state_once(native, route, state):
    ctrl = Control(native)
    result, point = activate_control(observed(ctrl))
    assert result.ok and result.route == route and point is None
    assert getattr(ctrl, state) == 1


@pytest.mark.parametrize("change", ["identity", "name", "disabled", "password", "checked"])
def test_changed_native_target_refuses_without_activation(change):
    ctrl = Control("CheckBox")
    binding = observed(ctrl)
    if change == "identity": ctrl.identity = (4, 5, 6)
    elif change == "name": ctrl.Name = "Delete account"
    elif change == "disabled": ctrl.IsEnabled = False
    elif change == "password": ctrl.IsPassword = True
    elif change == "checked": ctrl.checked = True
    result, point = activate_control(binding)
    assert not result.ok and "stale_target" in result.error and point is None
    assert ctrl.activations == 0
    assert ctrl.checked == (change == "checked")


def test_native_error_after_activation_does_not_fall_back_to_a_second_click():
    ctrl = Control(partial_failure=True)
    result, point = activate_control(observed(ctrl))
    assert ctrl.activations == 1 and point is None and not result.ok
    check = Check(Verdict.FAILED, result.error, "L0", {"exec_error": result.error})
    assert RecoveryPolicy(fixed_retry=True).decide(check, Action("click", x=50, y=50)).actions == []


def test_native_library_false_result_is_not_an_execution_success():
    ctrl = Control()
    ctrl.Invoke = lambda: False
    result, point = activate_control(observed(ctrl))
    assert not result.ok and "native_action_error" in result.error and point is None


def test_native_overlay_does_not_get_bypassed_by_invoke():
    ctrl = Control()
    result, point = activate_control(observed(ctrl), hit_test=lambda point, runtime: False)
    assert not result.ok and point is None and ctrl.activations == 0


def test_unsupported_pattern_returns_current_geometry_for_guarded_fallback():
    ctrl = Control()
    ctrl.GetInvokePattern = lambda: None
    binding = observed(ctrl)
    ctrl.BoundingRectangle = SimpleNamespace(left=200, top=150, right=300, bottom=190,
                                            width=lambda: 100, height=lambda: 40)
    result, point = activate_control(binding)
    assert result is None and point == (250, 170) and ctrl.activations == 0


def test_snapshot_binding_cannot_be_injected_by_the_model_or_serialized():
    action = parse_action({"type": "click", "x": 50, "y": 50, "binding": {"snapshot_id": "fake"}})
    assert action.binding is None
    action.binding = {"snapshot_id": "executor-owned"}
    assert "binding" not in action.to_dict()


@pytest.mark.parametrize("error", ["stale_target", "observation_invalidated_by_pause", "native_action_error"])
def test_invalidated_actions_never_use_fixed_replay(error):
    check = Check(Verdict.FAILED, error, "L0", {"exec_error": error})
    assert not RecoveryPolicy(fixed_retry=True).decide(check, Action("click", x=10, y=10)).actions


def test_windows_foreground_change_blocks_native_and_coordinate_input(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "pyautogui", fake_pyautogui())
    win = import_platform_module(monkeypatch, "windows", "win32")
    from gua.env.desktop import PyAutoGUIInput
    env = win.WindowsEnv.__new__(win.WindowsEnv)
    env.input = PyAutoGUIInput("windows", 1)
    env._mon = {"width": 800, "height": 600, "left": 0, "top": 0}
    env._snapshot_id, env._snapshot_window = "observed", (1, 22)
    env._window_key = lambda: (2, 33)
    result = env.execute(Action("click", x=50, y=50, binding={"snapshot_id": "observed"}))
    assert not result.ok and "stale_target" in result.error
    assert env.input.pg.calls == []


def test_windows_stops_ascii_input_immediately_when_an_input_event_moves_focus(monkeypatch):
    import sys
    from gua.env.desktop import PyAutoGUIInput, clipboard_type
    pg = fake_pyautogui()
    monkeypatch.setitem(sys.modules, "pyautogui", pg)
    win = import_platform_module(monkeypatch, "windows", "win32")
    first, second = Control("Edit"), Control("Edit")
    first.HasKeyboardFocus, second.identity = True, (9, 8, 7)
    active, written = [first], []
    def write(text, **kw):
        written.append(text)
        active[0] = second
    pg.write = write
    monkeypatch.setattr(win, "auto", SimpleNamespace(GetFocusedControl=lambda: active[0]))
    env = win.WindowsEnv.__new__(win.WindowsEnv)
    env.input = PyAutoGUIInput("windows", 1, type_fn=lambda text: clipboard_type(text, "windows", check_focus=env.input.focus_check))
    env._mon = {"width": 800, "height": 600, "left": 0, "top": 0}
    env._snapshot_id, env._snapshot_window = "observed", (1, 22)
    env._window_key = lambda: (1, 22)
    element = observed(first).element
    element.attrs["uia_key"] = "0"
    env._bound_elements, env._controls = {0: element}, {"0": (first, first.identity)}
    result = env.execute(Action("type", text="PRIVATE", binding={"snapshot_id": "observed"}))
    assert not result.ok and "stale_target" in result.error and written == ["P"]

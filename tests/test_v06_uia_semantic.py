"""v0.6：Windows UIA 语义（后台）动作路由——用假 UIA 控件验证，不需要 Windows 桌面（真机未复测）。"""
import pytest

from gua.env.a11y import finalize, uia_raw
from gua.env.uia_execution import ObservedControl, semantic_control


class Rect:
    left, top, right, bottom = 20, 30, 120, 70
    def width(self): return self.right - self.left
    def height(self): return self.bottom - self.top


class Value:
    def __init__(self, ctrl, read_only=False, lossy=False):
        self.ctrl, self.IsReadOnly, self.lossy = ctrl, read_only, lossy
    @property
    def Value(self): return self.ctrl.value
    def SetValue(self, v, waitTime=0):
        self.ctrl.value = v[:-1] if self.lossy else v
        return True


class Ctrl:
    AutomationId, IsEnabled, IsOffscreen, HasKeyboardFocus = "id1", True, False, False
    BoundingRectangle = Rect()
    def __init__(self, native="Button", name="Save", password=False, patterns=("Invoke",), read_only=False,
                 lossy=False, boom=False):
        self.ControlTypeName, self.Name, self.IsPassword = native + "Control", name, password
        self.value, self.calls, self._p, self._ro, self._lossy, self._boom = "", [], set(patterns), read_only, lossy, boom
        self.checked = False
    def GetRuntimeId(self): return (7, 7)
    def GetValuePattern(self):
        return Value(self, self._ro, self._lossy) if "Value" in self._p else None
    def _pat(self, n): return self if n in self._p else None
    def GetInvokePattern(self): return self._pat("Invoke")
    def GetTogglePattern(self): return self._pat("Toggle")
    def GetExpandCollapsePattern(self): return self._pat("ExpandCollapse")
    def GetScrollItemPattern(self): return self._pat("ScrollItem")
    def GetSelectionItemPattern(self): return self._pat("SelectionItem")
    @property
    def ToggleState(self): return int(self.checked)
    def Invoke(self):
        self.calls.append("Invoke")
        if self._boom: raise RuntimeError("COM disconnected after invoke")
    def Toggle(self): self.checked = not self.checked; self.calls.append("Toggle")
    def Expand(self): self.calls.append("Expand")
    def ScrollIntoView(self): self.calls.append("ScrollIntoView")
    def SetFocus(self): self.HasKeyboardFocus = True


def bound(ctrl):
    return ObservedControl(ctrl, (7, 7), finalize([uia_raw(ctrl)], (800, 600))[0][0])


def test_invoke_toggle_expand_scroll_routes():
    c = Ctrl()
    r = semantic_control(bound(c), "invoke")
    assert r.ok and r.route == "uia_semantic:invoke" and c.calls == ["Invoke"]
    cb = Ctrl("CheckBox", "Subscribe", patterns=("Toggle",))
    assert semantic_control(bound(cb), "toggle").ok and cb.checked
    combo = Ctrl("ComboBox", "Country", patterns=("ExpandCollapse",))
    assert semantic_control(bound(combo), "expand").ok and combo.calls == ["Expand"]
    li = Ctrl("ListItem", "Row 9", patterns=("ScrollItem",))
    assert semantic_control(bound(li), "scroll_into_view").ok


def test_missing_pattern_is_background_unavailable_not_silent_success():
    r = semantic_control(bound(Ctrl(patterns=())), "invoke")
    assert not r.ok and r.error.startswith("background_unavailable")


def test_set_value_verified_and_never_on_password():
    e = Ctrl("Edit", "Name", patterns=("Value",))
    assert semantic_control(bound(e), "set_value", "张三").ok and e.value == "张三"
    lossy = Ctrl("Edit", "Name", patterns=("Value",), lossy=True)
    r = semantic_control(bound(lossy), "set_value", "abc")
    assert not r.ok and r.error.startswith("native_action_error")
    pw = Ctrl("Edit", "pw", password=True, patterns=("Value",))
    r = semantic_control(bound(pw), "set_value", "x")
    assert not r.ok and "blocked_by_safety" in r.error and pw.value == ""
    ro = Ctrl("Edit", "Name", patterns=("Value",), read_only=True)
    assert semantic_control(bound(ro), "set_value", "x").error.startswith("background_unavailable")


def test_stale_identity_and_partial_failure_are_not_replayed():
    c = Ctrl()
    b = bound(c)
    c.Name = "Delete everything"
    r = semantic_control(b, "invoke")
    assert not r.ok and r.error.startswith("stale_target") and c.calls == []
    boom = Ctrl(boom=True)
    r = semantic_control(bound(boom), "invoke")
    assert not r.ok and r.error.startswith("native_action_error") and "do not replay" in r.error
    assert boom.calls == ["Invoke"]

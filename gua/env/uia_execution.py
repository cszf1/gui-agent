"""UIA action routing, isolated from Win32 imports for deterministic regression tests.

Only an observed control with the same runtime identity may be activated. A
pattern call is never retried with a mouse click: it can fail after taking effect.
"""
from __future__ import annotations

import time
import re
from dataclasses import dataclass

from .a11y import uia_raw
from .base import ExecResult, UIElement


def runtime_id(control) -> tuple:
    try:
        return tuple(control.GetRuntimeId())
    except Exception:
        return ()


@dataclass
class ObservedControl:
    control: object
    runtime: tuple
    element: UIElement


def activate_control(bound: ObservedControl, offset: tuple[int, int] = (0, 0),
                     hit_test=None) -> tuple[ExecResult | None, tuple | None]:
    """Return a result, or a fresh center for a guarded coordinate fallback.

The caller verifies the foreground window immediately before calling this.
Unknown identity, changed semantics or state refuse without attempting input.
"""
    started = time.time()
    ctrl, expected = bound.control, bound.element
    try:
        if not bound.runtime or runtime_id(ctrl) != bound.runtime:
            return ExecResult(False, "stale_target: UIA runtime identity changed", started, time.time()), None
        raw = uia_raw(ctrl, offset)
        if raw is None or not raw["enabled"]:
            return ExecResult(False, "stale_target: control unavailable or disabled", started, time.time()), None
        if (re.sub(r"\s+", " ", raw["name"]).strip()[:100] != expected.name or raw["role"] != expected.role
                or raw["is_password"] != expected.is_password
                or raw["attrs"].get("automation_id", "") != expected.attrs.get("automation_id", "")
                or raw["checked"] != expected.checked):
            return ExecResult(False, "stale_target: control semantics changed", started, time.time()), None
        l, t, r, b = raw["rect"]
        point = ((l + r) / 2, (t + b) / 2)
        if hit_test is not None and not hit_test(point, bound.runtime):
            return ExecResult(False, "stale_target: control covered by another surface", started, time.time()), None
        route, pattern, method = "", None, ""
        if expected.role in {"button", "link", "menuitem"}:
            pattern, method, route = ctrl.GetInvokePattern(), "Invoke", "uia_invoke"
        elif expected.role in {"checkbox", "switch"}:
            pattern, method, route = ctrl.GetTogglePattern(), "Toggle", "uia_toggle"
        elif expected.role in {"radio", "tab", "listitem", "treeitem"}:
            pattern, method, route = ctrl.GetSelectionItemPattern(), "Select", "uia_select"
        elif expected.role == "textbox":
            # Focus only. Payload replacement has its own consent/focus gate.
            route = "uia_focus"
            if ctrl.SetFocus() is False:
                return ExecResult(False, "native_action_error: focus was not acknowledged; observe again",
                                  started, time.time(), route=route), None
            return ExecResult(True, "", started, time.time(), route=route), None
        if pattern is not None:
            try:
                if getattr(pattern, method)() is False:
                    return ExecResult(False, "native_action_error: action was not acknowledged; effect uncertain, do not replay",
                                      started, time.time(), route=route), None
            except Exception as exc:
                return ExecResult(False, f"native_action_error: {type(exc).__name__}; effect uncertain, do not replay",
                                  started, time.time(), route=route), None
            return ExecResult(True, "", started, time.time(), route=route), None
        return None, point
    except Exception as exc:
        return ExecResult(False, f"stale_target: control could not be verified ({type(exc).__name__})",
                          started, time.time()), None


def replace_control_text(bound: ObservedControl, text: str, check_focus,
                         offset: tuple[int, int] = (0, 0)) -> ExecResult | None:
    """Replace a consented, focused plain Edit value and verify the outcome.

    None permits keyboard fallback only when no native write was attempted.
    Passwords and document editors keep their existing input path. An uncertain
    native write must never be followed by a second keyboard write. Neither
    values nor provider exception messages appear in the returned diagnostics.
    """
    started = time.time()
    ctrl, expected = bound.control, bound.element
    attempted = False
    try:
        check_focus()
        # Never request a password's ValuePattern or read its contents.
        if expected.is_password:
            return None
        if not bound.runtime or runtime_id(ctrl) != bound.runtime:
            raise ValueError("identity changed")
        if bool(ctrl.IsPassword):
            raise ValueError("security changed")
        raw = uia_raw(ctrl, offset)
        if (raw is None or not raw["enabled"] or not raw["focused"]
                or raw["is_password"]
                or re.sub(r"\s+", " ", raw["name"]).strip()[:100] != expected.name
                or raw["role"] != expected.role
                or raw["attrs"].get("automation_id", "") != expected.attrs.get("automation_id", "")):
            raise ValueError("semantics changed")
        if expected.role != "textbox" or raw["native_role"] != "Edit":
            return None
        pattern = ctrl.GetValuePattern()
        if pattern is None:
            return None
        if pattern.IsReadOnly:
            raise ValueError("read-only input")
        check_focus()
        if runtime_id(ctrl) != bound.runtime or not ctrl.IsEnabled or bool(ctrl.IsPassword):
            raise ValueError("state changed")
        attempted = True
        # UIAutomation's default half-second sleep is unnecessary: SetValue is
        # synchronous and the real value/focus are checked before success.
        acknowledged = pattern.SetValue(text, waitTime=0)
        check_focus()
        if bool(ctrl.IsPassword):
            raise ValueError("security changed")
        if acknowledged is False or pattern.Value != text:
            return ExecResult(False, "native_action_error: text replacement not verified; observe again, do not replay",
                              started, time.time(), route="uia_value")
        return ExecResult(True, "", started, time.time(), route="uia_value")
    except Exception as exc:
        reason = "native_action_error" if attempted else "stale_target"
        return ExecResult(False, f"{reason}: text replacement could not be verified ({type(exc).__name__}); observe again",
                          started, time.time(), route="uia_value")


# ---------------------------------------------------------------------------- v0.6 语义（后台）动作
_SEMANTIC_PATTERNS = {
    "invoke": ("GetInvokePattern", "Invoke"),
    "toggle": ("GetTogglePattern", "Toggle"),
    "select": ("GetSelectionItemPattern", "Select"),
    "expand": ("GetExpandCollapsePattern", "Expand"),
    "collapse": ("GetExpandCollapsePattern", "Collapse"),
    "scroll_into_view": ("GetScrollItemPattern", "ScrollIntoView"),
}


def semantic_control(bound: ObservedControl, method: str, text: str | None = None,
                     offset: tuple[int, int] = (0, 0)) -> ExecResult:
    """按 UIA 模式执行语义动作：不移动指针、不要求窗口在前台（参考 Cua Driver 的 background dispatch）。

    与 activate_control 相同的身份 / 语义 / 状态核对；没有对应模式 → background_unavailable（由混合执行器
    决定是否改走前台真实输入）；模式调用抛异常 → native_action_error（可能已部分生效，绝不重放）。
    set_value 从不作用于密码控件，也不读取其值；写入后读回核对。
    """
    started = time.time()
    route = f"uia_semantic:{method}"
    ctrl, expected = bound.control, bound.element
    try:
        if not bound.runtime or runtime_id(ctrl) != bound.runtime:
            return ExecResult(False, "stale_target: UIA runtime identity changed", started, time.time(), route=route)
        raw = uia_raw(ctrl, offset)
        if raw is None or not raw["enabled"]:
            return ExecResult(False, "stale_target: control unavailable or disabled", started, time.time(),
                              route=route)
        if (re.sub(r"\s+", " ", raw["name"]).strip()[:100] != expected.name or raw["role"] != expected.role
                or raw["is_password"] != expected.is_password
                or raw["attrs"].get("automation_id", "") != expected.attrs.get("automation_id", "")
                or raw["checked"] != expected.checked):
            return ExecResult(False, "stale_target: control semantics changed", started, time.time(), route=route)
    except Exception as exc:  # noqa: BLE001
        return ExecResult(False, f"stale_target: control could not be verified ({type(exc).__name__})",
                          started, time.time(), route=route)
    attempted = False
    try:
        if method == "set_value":
            if expected.is_password or bool(getattr(ctrl, "IsPassword", False)):
                return ExecResult(False, "blocked_by_safety: semantic set_value never writes password controls",
                                  started, time.time(), route=route)
            pattern = ctrl.GetValuePattern()
            if pattern is None:
                return ExecResult(False, "background_unavailable: control has no ValuePattern", started,
                                  time.time(), route=route)
            if pattern.IsReadOnly:
                return ExecResult(False, "background_unavailable: value is read-only", started, time.time(),
                                  route=route)
            attempted = True
            ack = pattern.SetValue(text or "", waitTime=0)
            if ack is False or pattern.Value != (text or ""):
                return ExecResult(False, "native_action_error: value not verified; observe again, do not replay",
                                  started, time.time(), route=route)
            return ExecResult(True, "", started, time.time(), route=route)
        if method == "focus":
            attempted = True
            if ctrl.SetFocus() is False:
                return ExecResult(False, "native_action_error: focus not acknowledged", started, time.time(),
                                  route=route)
            return ExecResult(True, "", started, time.time(), route=route)
        getter, call = _SEMANTIC_PATTERNS.get(method, (None, None))
        if getter is None:
            return ExecResult(False, f"unsupported: {method}", started, time.time(), route=route)
        get = getattr(ctrl, getter, None)
        pattern = get() if callable(get) else None
        if pattern is None:
            return ExecResult(False, f"background_unavailable: control exposes no {call} pattern", started,
                              time.time(), route=route)
        attempted = True
        if getattr(pattern, call)() is False:
            return ExecResult(False, "native_action_error: action was not acknowledged; effect uncertain, do not "
                              "replay", started, time.time(), route=route)
        return ExecResult(True, "", started, time.time(), route=route)
    except Exception as exc:  # noqa: BLE001
        reason = "native_action_error" if attempted else "background_unavailable"
        return ExecResult(False, f"{reason}: {type(exc).__name__}; effect uncertain, do not replay"
                          if attempted else f"{reason}: pattern unavailable ({type(exc).__name__})",
                          started, time.time(), route=route)

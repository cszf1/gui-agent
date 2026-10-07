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
            # Do not write through ValuePattern: the existing typing/consent
            # path remains responsible for input payloads and focus checks.
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

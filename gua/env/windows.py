"""Windows 环境（v0.1 的实现迁移到统一接口）。

- 截图：mss（GDI/DXGI）
- 控件：uiautomation（UI Automation），ControlTypeName → 统一角色（env/a11y.py: UIA_ROLE）
- 输入：pyautogui（底层 SendInput），中文走剪贴板

关键工程点（来自调研报告第 9 章 / QQ 实测）：
1. 进程启动时声明 Per-Monitor DPI Aware，否则截图像素和点击坐标会错位。
2. 每次执行前检查坐标是否在屏幕内（“点击越界”）。
3. 最小化窗口要先还原再激活（“最小化窗口”）。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
import uuid
from dataclasses import replace

from PIL import Image

from ..actions import Action
from .a11y import finalize, uia_raw
from .base import Env, ExecResult, Observation
from .commands import InvalidAppName, validate_app_name, windows_open_app_argv
from .desktop import PyAutoGUIInput, clipboard_type
from .uia_execution import ObservedControl, activate_control, runtime_id


def _startfile(path: str) -> None:
    """ShellExecute（不经过 cmd.exe 解析），用于 UWP / 开始菜单里能解析但不在 PATH 的应用名。"""
    os.startfile(path)  # type: ignore[attr-defined]

if sys.platform != "win32":  # pragma: no cover
    raise ImportError("gua.env.windows 只能在 Windows 上使用；离线调试请用 gua.env.mock")

import mss  # noqa: E402

try:
    import uiautomation as auto  # noqa: E402
except ImportError:  # 允许纯视觉模式
    auto = None


def _set_dpi_aware() -> float:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        return ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100.0
    except Exception:
        return 1.0


class WindowsEnv(Env):
    platform = "windows"
    targeted_input = True
    scroll_unit_px = 100

    def __init__(self, max_elements: int = 150, uia_depth: int = 12, monitor: int = 1):
        self.dpi_scale = _set_dpi_aware()
        self.max_elements = max_elements
        self.uia_depth = uia_depth
        self._sct = mss.mss()
        self._mon = self._sct.monitors[monitor]
        self.input = PyAutoGUIInput("windows", 1.0, (self._mon["left"], self._mon["top"]),
                                    type_fn=lambda t: clipboard_type(t, "windows", check_focus=self.input.focus_check),
                                    scroll_clicks=120)
        self._snapshot_id = ""
        self._snapshot_window = None
        self._controls: dict = {}

    @staticmethod
    def _window_key():
        user32 = ctypes.windll.user32
        get_window = user32.GetForegroundWindow
        get_window.restype = ctypes.c_void_p
        hwnd = get_window()
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
        return (int(hwnd or 0), pid.value)

    def bind_action(self, action: Action, obs: Observation) -> Action:
        return replace(action, binding={"snapshot_id": obs.snapshot_id})

    def element_identity(self, element):
        rid = element.attrs.get("uia_runtime")
        return (self._snapshot_window, rid) if self._snapshot_window and rid else None

    def _grab(self) -> Image.Image:
        raw = self._sct.grab(self._mon)
        return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

    def _foreground(self):
        if auto is None:
            return None
        try:
            return auto.GetForegroundControl().GetTopLevelControl()
        except Exception:
            return None

    def _check_typed_focus(self):
        expected = next((e for e in self._bound_elements.values() if e.focused), None)
        bound = self._controls.get(expected.attrs.get("uia_key")) if expected else None
        if auto is None or bound is None or self._window_key() != self._snapshot_window:
            raise ValueError("stale_target: keyboard focus could not be verified")
        current = auto.GetFocusedControl()
        if not bound[1] or runtime_id(current) != bound[1] or bool(current.IsPassword) != expected.is_password:
            raise ValueError("stale_target: keyboard focus or security changed; stop typing")

    def _hit_test(self, point, expected):
        try:
            x, y = point
            hit = auto.ControlFromPoint(int(x + self._mon["left"]), int(y + self._mon["top"]))
            for _ in range(8):
                if hit is None:
                    break
                if runtime_id(hit) == expected:
                    return True
                hit = hit.GetParentControl()
        except Exception:
            pass
        return False

    def _raws(self, root) -> list[dict]:
        out = []
        off = (self._mon["left"], self._mon["top"])
        for ctrl, _depth in auto.WalkControl(root, includeTop=False, maxDepth=self.uia_depth):
            if len(out) >= self.max_elements * 3:
                break
            try:
                raw = uia_raw(ctrl, off)       # 含 IsPassword（审查条目 11）
            except Exception:
                continue
            if raw is not None:
                key = str(len(out))
                raw["attrs"]["uia_key"] = key
                self._controls[key] = (ctrl, runtime_id(ctrl))
                raw["attrs"]["uia_runtime"] = ".".join(str(n) for n in self._controls[key][1])
                out.append(raw)
        return out

    def observe(self, with_elements: bool = True) -> Observation:
        window_before = self._window_key()
        img = self._grab()
        fg = self._foreground()
        title, proc, wins, elems, text = "", "", [], [], ""
        if fg is not None:
            title = fg.Name or ""
            try:
                import psutil
                proc = psutil.Process(fg.ProcessId).name()
            except Exception:
                pass
            if with_elements:
                self._controls = {}
                elems, text = finalize(self._raws(fg), img.size, self.max_elements)
        if auto is not None and with_elements:
            try:
                wins = [w.Name for w in auto.GetRootControl().GetChildren() if w.Name][:30]
            except Exception:
                pass
        if with_elements:
            self._snapshot_id = uuid.uuid4().hex
            window_after = self._window_key()
            self._snapshot_window = window_after if window_before == window_after else None
            self._bound_elements = {e.id: e for e in elems}
        return Observation(screenshot=img, timestamp=time.time(), screen_size=img.size,
                           dpi_scale=self.dpi_scale, active_window=title, active_process=proc,
                           windows=wins, elements=elems, platform="windows", text=text, cursor=self.input.position(),
                           snapshot_id=self._snapshot_id if with_elements else "")

    def execute(self, a: Action) -> ExecResult:
        t0 = time.time()
        try:
            a.validate()
        except ValueError as exc:
            return ExecResult(False, f"invalid_action: {exc}", t0, time.time())
        if not self.supports(a.type):
            return ExecResult(False, f"unsupported action {a.type} on windows", t0, time.time())
        err = self._bounds_error(a, self._mon["width"], self._mon["height"])
        if err:
            return ExecResult(False, err, t0, time.time())
        import pyautogui
        old_focus_check = getattr(self.input, "focus_check", None)
        try:
            if callable(getattr(pyautogui, "failSafeCheck", None)):
                pyautogui.failSafeCheck()
            if a.binding and a.type not in {"open_app", "focus_window", "wait"}:
                if (a.binding.get("snapshot_id") != self._snapshot_id or not self._snapshot_window
                        or not self._snapshot_window[0] or self._window_key() != self._snapshot_window):
                    return ExecResult(False, "stale_target: foreground window changed; observe again", t0, time.time())
                if a.type == "click" and a.element_id is not None:
                    element = self._bound_elements.get(a.element_id)
                    native = self._controls.get(element.attrs.get("uia_key")) if element else None
                    if native is None:
                        return ExecResult(False, "stale_target: control binding unavailable", t0, time.time())
                    result, point = activate_control(ObservedControl(*native, element),
                                                     (self._mon["left"], self._mon["top"]), self._hit_test)
                    if result is not None:
                        return result
                    a = replace(a, x=point[0], y=point[1])
                    err = self._bounds_error(a, self._mon["width"], self._mon["height"])
                    if err:
                        return ExecResult(False, err, t0, time.time())
                if a.type == "type":
                    self.input.focus_check = self._check_typed_focus
            r = self.input.run(a)
            if r is not None:
                r.route = "windows_input"
                return r
            if a.type == "focus_window":
                if not self.focus_window(a.text or ""):
                    return ExecResult(False, f"window_not_found {a.text!r}", t0, time.time())
            elif a.type == "open_app":
                # v0.3：不再用 `cmd /c start`（cmd 会重新解析 & | ^ %，可被注入）；argv + 校验
                try:
                    app = validate_app_name(a.app or a.text)
                    argv = windows_open_app_argv(app)
                except InvalidAppName as e:
                    return ExecResult(False, f"invalid_argument: {e}", t0, time.time())
                if argv:
                    subprocess.Popen(argv, shell=False)
                else:
                    _startfile(app)
                time.sleep(1.5)
            return ExecResult(True, "", t0, time.time())
        except pyautogui.FailSafeException as e:   # 兜底：输入层之外触发的 fail-safe 也是用户中止
            from ..errors import UserAbort
            raise UserAbort(str(e)) from e
        except Exception as e:  # noqa: BLE001
            return ExecResult(False, f"{type(e).__name__}: {e}", t0, time.time())
        finally:
            self.input.focus_check = old_focus_check

    def wait_until_stable(self, timeout=5.0, interval=0.4, threshold=0.002, stable_frames=2):
        # Keep pixel verification and a fresh final UIA tree, but sample sooner.
        return super().wait_until_stable(timeout, min(interval, 0.12), threshold, max(2, stable_frames))

    def close(self):
        self._controls.clear()
        self._sct.close()

    def focus_window(self, title_substring: str) -> bool:
        if auto is None or not title_substring:
            return False
        for w in auto.GetRootControl().GetChildren():
            if title_substring.lower() in (w.Name or "").lower():
                try:
                    wp = w.GetWindowPattern()
                    if wp and wp.WindowVisualState == auto.WindowVisualState.Minimized:
                        wp.SetWindowVisualState(auto.WindowVisualState.Normal)
                except Exception:
                    pass
                try:
                    w.SetActive()
                    w.SetFocus()
                    time.sleep(0.3)
                    return True
                except Exception:
                    return False
        return False

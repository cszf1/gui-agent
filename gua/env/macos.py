"""macOS 环境。

- 截图：Quartz CGWindowListCreateImage（pyobjc-framework-Quartz），失败时回退 `screencapture -x`
- 元素：Accessibility API（pyobjc ApplicationServices：AXUIElementCreateApplication → 递归 AXChildren），
  节点先序列化成 dict（见 tests/fixtures/ax_textedit.json）再走 env/a11y.py 统一转换
- 输入：pyautogui（坐标单位为 point），截图是 Retina 像素 → scale = 截图宽 / 屏幕 point 宽
- 前台应用：NSWorkspace.frontmostApplication；激活：`osascript -e 'tell application "X" to activate'`

权限：终端（或 Python）需要在「系统设置 → 隐私与安全性」里同时授予“辅助功能”和“屏幕录制”。
本仓库在沙箱里只做了 AX 树转换的 fixture 测试，真实 macOS 上未验证。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from typing import Any, Optional

from PIL import Image

from ..actions import Action
from .a11y import ax_tree_to_elements
from .base import Env, ExecResult, Observation
from .desktop import PyAutoGUIInput, clipboard_type

if sys.platform != "darwin":  # pragma: no cover
    raise ImportError("gua.env.macos 只能在 macOS 上使用")

AX_ATTRS = ["AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXValue", "AXHelp", "AXEnabled", "AXFocused"]


def _ax_get(el, attr):
    from ApplicationServices import AXUIElementCopyAttributeValue
    err, val = AXUIElementCopyAttributeValue(el, attr, None)
    return None if err else val


def _ax_point(val) -> Optional[list[float]]:
    from ApplicationServices import AXValueGetValue, kAXValueCGPointType, kAXValueCGSizeType
    for kind in (kAXValueCGPointType, kAXValueCGSizeType):
        ok, v = AXValueGetValue(val, kind, None)
        if ok:
            return [v.x, v.y] if hasattr(v, "x") else [v.width, v.height]
    return None


def ax_node_to_dict(el, depth: int = 0, max_depth: int = 20, budget: list | None = None) -> dict[str, Any]:
    budget = budget if budget is not None else [2500]
    budget[0] -= 1
    d: dict[str, Any] = {}
    for a in AX_ATTRS:
        v = _ax_get(el, a)
        if v is not None:
            d[a] = v if isinstance(v, (str, int, float, bool)) else str(v)
    pos, size = _ax_get(el, "AXPosition"), _ax_get(el, "AXSize")
    if pos is not None and size is not None:
        d["AXPosition"], d["AXSize"] = _ax_point(pos), _ax_point(size)
    kids = _ax_get(el, "AXChildren") or []
    if depth < max_depth and budget[0] > 0:
        d["children"] = [ax_node_to_dict(k, depth + 1, max_depth, budget) for k in list(kids)[:150]]
    return d


class MacOSEnv(Env):
    platform = "macos"
    scroll_unit_px = 40

    def __init__(self, max_elements: int = 150, use_ax: bool = True):
        import pyautogui
        self.max_elements = max_elements
        self.use_ax = use_ax
        self.points = pyautogui.size()
        img = self._grab()
        self.scale = img.width / self.points[0]
        self.input = PyAutoGUIInput("macos", self.scale, (0, 0), type_fn=lambda t: clipboard_type(t, "macos"))

    def _grab(self) -> Image.Image:
        try:
            import Quartz
            cg = Quartz.CGWindowListCreateImage(Quartz.CGRectInfinite, Quartz.kCGWindowListOptionOnScreenOnly,
                                                Quartz.kCGNullWindowID, Quartz.kCGWindowImageDefault)
            w, h = Quartz.CGImageGetWidth(cg), Quartz.CGImageGetHeight(cg)
            bpr = Quartz.CGImageGetBytesPerRow(cg)
            data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(cg))
            return Image.frombuffer("RGBA", (w, h), bytes(data), "raw", "BGRA", bpr, 1).convert("RGB")
        except Exception:
            fd, path = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            subprocess.run(["screencapture", "-x", path], check=True)
            img = Image.open(path).convert("RGB")
            os.unlink(path)
            return img

    def _frontmost(self):
        from AppKit import NSWorkspace
        return NSWorkspace.sharedWorkspace().frontmostApplication()

    def observe(self, with_elements: bool = True) -> Observation:
        img = self._grab()
        app_name, title, elems, text, wins = "", "", [], "", []
        try:
            app = self._frontmost()
            app_name = str(app.localizedName())
            if with_elements and self.use_ax:
                from ApplicationServices import AXUIElementCreateApplication
                root = AXUIElementCreateApplication(app.processIdentifier())
                win = _ax_get(root, "AXFocusedWindow")
                title = str(_ax_get(win, "AXTitle") or "") if win is not None else ""
                tree = ax_node_to_dict(win if win is not None else root)
                elems, text = ax_tree_to_elements(tree, img.size, self.scale, self.max_elements)
                from AppKit import NSWorkspace
                wins = [str(a.localizedName()) for a in NSWorkspace.sharedWorkspace().runningApplications()
                        if a.activationPolicy() == 0][:30]
        except Exception as e:
            text = f"(AX failed: {e}; 检查“辅助功能”权限)"
        return Observation(screenshot=img, timestamp=time.time(), screen_size=img.size, dpi_scale=self.scale,
                           active_window=f"{app_name} - {title}" if title else app_name, active_process=app_name,
                           windows=wins, elements=elems, platform="macos", text=text)

    def execute(self, a: Action) -> ExecResult:
        t0 = time.time()
        if not self.supports(a.type):
            return ExecResult(False, f"unsupported action {a.type} on macos", t0, time.time())
        img_w, img_h = int(self.points[0] * self.scale), int(self.points[1] * self.scale)
        err = self._bounds_error(a, img_w, img_h)
        if err:
            return ExecResult(False, err, t0, time.time())
        try:
            r = self.input.run(a)
            if r is not None:
                return r
            if a.type == "focus_window":
                if not self.focus_window(a.text or ""):
                    return ExecResult(False, f"window_not_found {a.text!r}", t0, time.time())
            elif a.type == "open_app":
                subprocess.run(["open", "-a", a.app or a.text or ""], check=True, timeout=15)
                time.sleep(1.5)
            return ExecResult(True, "", t0, time.time())
        except Exception as e:  # noqa: BLE001
            return ExecResult(False, f"{type(e).__name__}: {e}", t0, time.time())

    def focus_window(self, title_substring: str) -> bool:
        """macOS 以“应用”为单位激活（也会把最小化窗口还原）。title_substring 取 'App - 窗口' 的 App 部分。"""
        if not title_substring:
            return False
        app = title_substring.split(" - ")[0]
        r = subprocess.run(["osascript", "-e", f'tell application "{app}" to activate'], capture_output=True)
        time.sleep(0.4)
        return r.returncode == 0

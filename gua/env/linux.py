"""Linux（X11）环境。

- 截图：mss（X11 XGetImage）
- 元素：AT-SPI（pyatspi，可选；系统包 python3-pyatspi / gir1.2-atspi-2.0），节点先序列化成 dict
  再走 env/a11y.py 的统一转换；没有 AT-SPI 时降级为纯视觉
- 输入：pyautogui；前台窗口 / 激活窗口用 xdotool（与 Anthropic computer-use 参考实现相同的工具链）
- Wayland：mss/xdotool 无法工作，需在 X11 会话（或 Xvfb）下运行

只在 X11 + 真实桌面上才能端到端验证；本仓库在沙箱里只做了 AT-SPI 树转换的 fixture 测试。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Any, Optional

from PIL import Image

from ..actions import Action
from .a11y import atspi_tree_to_elements
from .base import Env, ExecResult, Observation
from .desktop import PyAutoGUIInput, clipboard_type


def _sh(args: list[str], timeout: float = 5) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""


def atspi_node_to_dict(acc, depth: int = 0, max_depth: int = 25, budget: list | None = None) -> dict[str, Any]:
    """pyatspi.Accessible → 可 JSON 化的 dict（与 tests/fixtures/atspi_gedit.json 同结构）。"""
    import pyatspi
    budget = budget if budget is not None else [3000]
    budget[0] -= 1
    d: dict[str, Any] = {"role": acc.getRoleName(), "name": acc.name or ""}
    try:
        st = acc.getState()
        d["states"] = [pyatspi.stateToString(s) for s in st.getStates()]
    except Exception:
        d["states"] = []
    try:
        comp = acc.queryComponent()
        e = comp.getExtents(pyatspi.DESKTOP_COORDS)
        d["extents"] = [e.x, e.y, e.width, e.height]
    except Exception:
        pass
    try:
        t = acc.queryText()
        d["text"] = t.getText(0, min(t.characterCount, 200))
    except Exception:
        pass
    if depth < max_depth and budget[0] > 0 and "showing" in d["states"]:
        kids = []
        for i in range(min(acc.childCount, 200)):
            try:
                kids.append(atspi_node_to_dict(acc.getChildAtIndex(i), depth + 1, max_depth, budget))
            except Exception:
                continue
        d["children"] = kids
    return d


class LinuxEnv(Env):
    platform = "linux"
    scroll_unit_px = 60

    def __init__(self, max_elements: int = 150, use_atspi: bool = True, monitor: int = 1):
        if not os.environ.get("DISPLAY"):
            raise RuntimeError("LinuxEnv 需要 X11 DISPLAY（Wayland 下请改用 X11 会话或 Xvfb）")
        import mss
        self._sct = mss.mss()
        self._mon = self._sct.monitors[monitor]
        self.max_elements = max_elements
        self.atspi = None
        if use_atspi:
            try:
                import pyatspi  # noqa: F401
                self.atspi = pyatspi
            except ImportError:
                self.atspi = None
        self.has_xdotool = shutil.which("xdotool") is not None
        self.input = PyAutoGUIInput("linux", 1.0, (self._mon["left"], self._mon["top"]),
                                    type_fn=lambda t: clipboard_type(t, "linux"))

    def _grab(self) -> Image.Image:
        raw = self._sct.grab(self._mon)
        return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

    def _active_title(self) -> tuple[str, str]:
        if not self.has_xdotool:
            return "", ""
        wid = _sh(["xdotool", "getactivewindow"])
        if not wid:
            return "", ""
        title = _sh(["xdotool", "getwindowname", wid])
        pid = _sh(["xdotool", "getwindowpid", wid])
        proc = ""
        if pid.isdigit():
            try:
                proc = open(f"/proc/{pid}/comm").read().strip()
            except Exception:
                pass
        return title, proc

    def _atspi_tree(self, title: str) -> Optional[dict]:
        if self.atspi is None:
            return None
        desktop = self.atspi.Registry.getDesktop(0)
        best = None
        for app in desktop:
            try:
                for win in app:
                    st = win.getState()
                    if st.contains(self.atspi.STATE_ACTIVE) or (title and title in (win.name or "")):
                        best = win
                        break
            except Exception:
                continue
            if best is not None:
                break
        return atspi_node_to_dict(best) if best is not None else None

    def observe(self, with_elements: bool = True) -> Observation:
        img = self._grab()
        title, proc = self._active_title()
        elems, text = [], ""
        if with_elements:
            try:
                tree = self._atspi_tree(title)
                if tree:
                    # AT-SPI 坐标为桌面坐标，减去显示器偏移即截图坐标
                    elems, text = atspi_tree_to_elements(_shift(tree, -self._mon["left"], -self._mon["top"]),
                                                         img.size, 1.0, self.max_elements)
            except Exception as e:
                text = f"(atspi failed: {e})"
        wins = []
        if with_elements and shutil.which("wmctrl"):
            wins = [ln.split(None, 3)[-1] for ln in _sh(["wmctrl", "-l"]).splitlines() if ln.strip()][:30]
        return Observation(screenshot=img, timestamp=time.time(), screen_size=img.size, dpi_scale=1.0,
                           active_window=title, active_process=proc, windows=wins, elements=elems,
                           platform="linux", text=text)

    def execute(self, a: Action) -> ExecResult:
        t0 = time.time()
        if not self.supports(a.type):
            return ExecResult(False, f"unsupported action {a.type} on linux", t0, time.time())
        err = self._bounds_error(a, self._mon["width"], self._mon["height"])
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
                subprocess.Popen([a.app or a.text or ""], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
                time.sleep(1.5)
            return ExecResult(True, "", t0, time.time())
        except Exception as e:  # noqa: BLE001
            return ExecResult(False, f"{type(e).__name__}: {e}", t0, time.time())

    def focus_window(self, title_substring: str) -> bool:
        if not title_substring or not self.has_xdotool:
            return False
        ids = _sh(["xdotool", "search", "--name", title_substring]).split()
        if not ids:
            return False
        _sh(["xdotool", "windowmap", ids[-1]])          # 还原最小化窗口
        _sh(["xdotool", "windowactivate", "--sync", ids[-1]])
        time.sleep(0.3)
        return True


def _shift(node: dict, dx: int, dy: int) -> dict:
    n = dict(node)
    if n.get("extents"):
        x, y, w, h = n["extents"]
        n["extents"] = [x + dx, y + dy, w, h]
    n["children"] = [_shift(c, dx, dy) for c in node.get("children", []) or []]
    return n

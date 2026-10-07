"""桌面平台共用的 pyautogui 输入层（Windows / macOS / Linux）。

截图像素与 pyautogui 坐标之间可能有缩放（macOS Retina：截图像素 = 2 × point），
scale = 截图宽 / pyautogui.size() 宽；执行前统一除以 scale。
"""
from __future__ import annotations

import time
from typing import Optional

from ..actions import Action
from .base import ExecResult

MAC_KEYS = {"ctrl": "ctrl", "cmd": "command", "command": "command", "meta": "command", "win": "command",
            "option": "option", "alt": "option"}


def norm_key(k: str, platform: str) -> str:
    k = k.strip().lower()
    alias = {"control": "ctrl", "escape": "esc", "return": "enter", "del": "delete", "windows": "win",
             "super": "win", "pgup": "pageup", "pgdn": "pagedown"}
    k = alias.get(k, k)
    if platform == "macos":
        return MAC_KEYS.get(k, k)
    if platform == "linux" and k in {"cmd", "meta", "command"}:
        return "win"
    if platform == "windows" and k in {"cmd", "meta", "command"}:
        return "win"
    return k


class PyAutoGUIInput:
    def __init__(self, platform: str, scale: float = 1.0, offset: tuple[int, int] = (0, 0),
                 type_fn=None, scroll_clicks: int = 1):
        import pyautogui
        pyautogui.FAILSAFE = True   # 鼠标甩到屏幕角落可紧急停止（人工接管）
        pyautogui.PAUSE = 0.05
        self.pg = pyautogui
        self.platform = platform
        self.scale = scale
        self.offset = offset
        self.type_fn = type_fn
        self.scroll_clicks = scroll_clicks  # 一个“格”对应 pyautogui.scroll 的单位数（Windows 120/格）

    def _xy(self, x: float, y: float) -> tuple[int, int]:
        return int(x / self.scale) + self.offset[0], int(y / self.scale) + self.offset[1]

    def run(self, a: Action, focus=None) -> Optional[ExecResult]:
        """执行通用输入动作；返回 None 表示该动作不归输入层管（交给平台后端）。"""
        pg = self.pg
        t0 = time.time()
        if a.type in {"click", "double_click", "right_click", "move", "long_press"}:
            x, y = self._xy(a.x, a.y)
            if a.type == "click":
                pg.click(x, y)
            elif a.type == "double_click":
                pg.doubleClick(x, y)
            elif a.type == "right_click":
                pg.rightClick(x, y)
            elif a.type == "move":
                pg.moveTo(x, y, duration=0.1)
            else:
                pg.mouseDown(x, y)
                time.sleep(a.seconds or 0.8)
                pg.mouseUp(x, y)
        elif a.type == "drag":
            x, y = self._xy(a.x, a.y)
            x2, y2 = self._xy(a.x2, a.y2)
            pg.moveTo(x, y)
            pg.dragTo(x2, y2, duration=0.4, button="left")
        elif a.type == "scroll":
            if a.x is not None:
                pg.moveTo(*self._xy(a.x, a.y))
            n = a.amount * self.scroll_clicks
            if a.direction in {"up", "down"}:
                pg.scroll(n if a.direction == "up" else -n)
            else:
                pg.hscroll(n if a.direction == "right" else -n)
        elif a.type == "type":
            mod = "command" if self.platform == "macos" else "ctrl"
            if a.clear:
                pg.hotkey(mod, "a")
                pg.press("backspace")
            if self.type_fn:
                self.type_fn(a.text or "")
            else:
                pg.write(a.text or "", interval=0.01)
            if a.submit:
                pg.press("enter")
        elif a.type == "hotkey":
            pg.hotkey(*[norm_key(k, self.platform) for k in a.keys])
        elif a.type == "key_down":
            for k in a.keys:
                pg.keyDown(norm_key(k, self.platform))
        elif a.type == "key_up":
            for k in a.keys:
                pg.keyUp(norm_key(k, self.platform))
        elif a.type == "wait":
            time.sleep(min(a.seconds or 1.0, 10.0))
        elif a.type == "back":
            pg.hotkey("command", "[") if self.platform == "macos" else pg.hotkey("alt", "left")
        else:
            return None
        return ExecResult(True, "", t0, time.time())


def clipboard_type(text: str, platform: str) -> None:
    """非 ASCII（中文）走剪贴板粘贴，避开输入法干扰（v0.1 在 Windows 上的做法，推广到三平台）。"""
    import pyautogui
    if text.isascii():
        pyautogui.write(text, interval=0.01)
        return
    import pyperclip
    old = None
    try:
        old = pyperclip.paste()
    except Exception:
        pass
    pyperclip.copy(text)
    pyautogui.hotkey("command" if platform == "macos" else "ctrl", "v")
    time.sleep(0.15)
    if old is not None:
        try:
            pyperclip.copy(old)
        except Exception:
            pass

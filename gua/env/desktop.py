"""桌面平台共用的 pyautogui 输入层（Windows / macOS / Linux）。

截图像素与 pyautogui 坐标之间可能有缩放（macOS Retina：截图像素 = 2 × point），
scale = 截图宽 / pyautogui.size() 宽；执行前统一除以 scale。

v0.3：
- pyautogui 的 FailSafeException（鼠标甩到屏幕角落 = 人工紧急停止）统一转换为 gua.errors.UserAbort 向上传播，
  三个桌面平台都不再把它当作普通执行失败吞掉（审查条目 10）。
- 键名先经 gua.keys.canonical_key 规范化，再映射到各平台名字（审查条目 8）。
"""
from __future__ import annotations

import time
from typing import Optional

from ..actions import Action, ActionParseError
from ..errors import UserAbort
from ..keys import KeySequenceError, canonical_key, validate_sequence
from .base import ExecResult

MAC_KEYS = {"ctrl": "ctrl", "cmd": "command", "command": "command", "meta": "command", "win": "command",
            "option": "option", "alt": "option"}


def norm_key(k: str, platform: str) -> str:
    """规范键名 → pyautogui 键名。meta（cmd/win/super）在 macOS 是 command，在 Windows/Linux 是 win。"""
    k = canonical_key(k)
    if platform == "macos":
        return MAC_KEYS.get(k, k)
    if k == "meta":
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
        """执行通用输入动作；返回 None 表示该动作不归输入层管（交给平台后端）。

        第三轮条目 1：直接执行入口也要校验按键序列（tab+enter / 多字符 secret+enter 等非法组合
        在执行前就抛 ActionParseError，平台 execute 会把它变成失败的 ExecResult，绝不真的按键）。
        FailSafeException → UserAbort（终止整次运行，不是可恢复的执行失败）。
        """
        if a.type in {"hotkey", "key_down", "key_up"}:
            try:
                validate_sequence(a.keys, kind=a.type)
            except KeySequenceError as e:
                raise ActionParseError("bad_value", str(e), "keys") from None
        try:
            return self._run(a)
        except self.pg.FailSafeException as e:
            raise UserAbort(f"pyautogui fail-safe triggered (mouse moved to a screen corner): {e}") from e

    def position(self) -> Optional[tuple[int, int]]:
        """当前鼠标位置（截图像素）。"""
        try:
            x, y = self.pg.position()
            return int((x - self.offset[0]) * self.scale), int((y - self.offset[1]) * self.scale)
        except Exception:
            return None

    def _run(self, a: Action) -> Optional[ExecResult]:
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
    try:
        pyautogui.hotkey("command" if platform == "macos" else "ctrl", "v")
        time.sleep(0.15)
    finally:
        # v0.3.1：无论如何都不把输入的文字（可能是密码）留在系统剪贴板上：恢复旧内容，读不到旧内容就清空
        try:
            pyperclip.copy(old if old is not None else "")
        except Exception:  # noqa: BLE001
            pass

"""按键名规范化：所有平台别名先归一到一套规范名，再做安全判定与平台映射。

审查条目 8：v0.2 只按字面比较，`shift+delete` 被拦但 `shift+del` 放行、`key_down` 序列完全不查。
规范名：ctrl / alt / shift / meta（cmd / win / super / command）/ delete（向前删除）/ backspace / esc / enter / …
"""
from __future__ import annotations

from typing import Iterable

_ALIAS = {
    # 修饰键
    "control": "ctrl", "ctl": "ctrl", "ctrl_l": "ctrl", "ctrl_r": "ctrl", "control_l": "ctrl", "control_r": "ctrl",
    "lctrl": "ctrl", "rctrl": "ctrl", "⌃": "ctrl", "strg": "ctrl",
    "cmd": "meta", "command": "meta", "win": "meta", "windows": "meta", "super": "meta", "super_l": "meta",
    "super_r": "meta", "meta_l": "meta", "meta_r": "meta", "lwin": "meta", "rwin": "meta", "os": "meta", "⌘": "meta",
    "winleft": "meta", "winright": "meta", "cmdleft": "meta", "cmdright": "meta", "hyper": "meta",
    "option": "alt", "opt": "alt", "alt_l": "alt", "alt_r": "alt", "altleft": "alt", "altright": "alt",
    "lalt": "alt", "ralt": "alt", "altgr": "alt", "⌥": "alt", "optionleft": "alt", "optionright": "alt",
    "shift_l": "shift", "shift_r": "shift", "lshift": "shift", "rshift": "shift", "shiftleft": "shift",
    "shiftright": "shift", "⇧": "shift",
    # 编辑键
    "del": "delete", "forward_delete": "delete", "forwarddelete": "delete", "kp_delete": "delete",
    "⌦": "delete", "entf": "delete",
    "bksp": "backspace", "back_space": "backspace", "bs": "backspace", "⌫": "backspace",
    "escape": "esc", "⎋": "esc",
    "return": "enter", "kp_enter": "enter", "⏎": "enter", "ret": "enter",
    "pgup": "pageup", "page_up": "pageup", "prior": "pageup", "pgdn": "pagedown", "page_down": "pagedown",
    "next": "pagedown", "ins": "insert", "spacebar": "space", " ": "space",
    # v0.3.1：激活键的平台别名（Android keycode 名 / 小键盘回车）
    "keycode_enter": "enter", "keycode_numpad_enter": "enter", "numpad_enter": "enter", "numpadenter": "enter",
    "kpenter": "enter", "keycode_space": "space", "keycode_dpad_center": "dpad_center", "center": "dpad_center",
    "dpad_centre": "dpad_center",
    "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
}
MODIFIERS = {"ctrl", "alt", "shift", "meta"}
# 按下即“激活焦点元素”的键（按钮 / 链接 / 表单提交）；Android 数字 keycode：66 ENTER、23 DPAD_CENTER、62 SPACE、160 NUMPAD_ENTER
ACTIVATION_KEYS = {"enter", "space", "dpad_center", "66", "23", "62", "160"}


def canonical_key(k: str) -> str:
    k = str(k).strip()
    if k == " ":
        return "space"
    low = k.lower()
    return _ALIAS.get(low, low)


def split_keys(keys: Iterable[str]) -> list[str]:
    """['ctrl+shift', 'del'] / ['Shift+Del'] → ['ctrl', 'shift', 'delete']（规范名，保持顺序、去重）。"""
    out: list[str] = []
    for k in keys or []:
        s = str(k)
        parts = [s] if s in {"+", " "} else [p for p in s.replace(" + ", "+").split("+") if p]
        for p in parts:
            c = canonical_key(p)
            if c and c not in out:
                out.append(c)
    return out


def canonical_set(keys: Iterable[str]) -> frozenset[str]:
    return frozenset(split_keys(keys))

"""按键名规范化：所有平台别名先归一到一套规范名，再做安全判定与平台映射。

审查条目 8（v0.3）：v0.2 只按字面比较，`shift+delete` 被拦但 `shift+del` 放行、`key_down` 序列完全不查。
规范名：ctrl / alt / shift / meta（cmd / win / super / command）/ delete（向前删除）/ backspace / esc / enter / …

审查条目 1（第三轮）：**按键序列必须是一个“组合”（0 个或多个修饰键 + 恰好一个按键）**，而不是一串动作。
`["tab", "enter"]`、`["hunter2", "enter"]`（把一段文字当成按键）这类序列会把“先移动、再观察、再过闸”的
多个动作压进一个动作里绕过闸门，因此在这里判定为非法（`KeySequenceError`），由 Action.validate / 安全闸门 /
各平台执行入口统一拒绝，要求模型拆成独立动作。
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
    # 打印 / 切换键的常见别名
    "prtsc": "printscreen", "print_screen": "printscreen", "printscreen": "printscreen",
    "caps_lock": "capslock", "num_lock": "numlock", "scroll_lock": "scrolllock",
}
MODIFIERS = {"ctrl", "alt", "shift", "meta"}
# 按下即“激活焦点元素”的键（按钮 / 链接 / 表单提交）；Android 数字 keycode：66 ENTER、23 DPAD_CENTER、62 SPACE、160 NUMPAD_ENTER
ACTIVATION_KEYS = {"enter", "space", "dpad_center", "66", "23", "62", "160"}
# 规范的“命名键”（非单字符的合法键名）；不在此集合、又不是单字符的键名一律视为把文字当按键
NAMED_KEYS = frozenset({
    "tab", "enter", "esc", "space", "delete", "backspace", "insert",
    "home", "end", "pageup", "pagedown", "printscreen",
    "up", "down", "left", "right", "dpad_center",
    "back", "forward", "search", "menu", "power",
    "volume_up", "volume_down", "volume_mute", "app_switch", "recent",
    "capslock", "numlock", "scrolllock", "pause", "break",
}) | frozenset(f"f{i}" for i in range(1, 25))
# 会产生字符 / 控制字符的命名键（判断按键动作是否“携带文字”时用；f4 / esc / 方向键不携带字符）
CHAR_KEYS = frozenset({"space", "enter", "tab", "backspace", "delete"})


class KeySequenceError(ValueError):
    """非法按键序列（不是“修饰键 + 恰好一个按键”）。"""


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


def is_named_or_char_key(k: str) -> bool:
    """单个按键是否合法：单字符，或者是规范的命名键，或数字按键代码（Android 等）。"""
    return len(k) == 1 or k in NAMED_KEYS or (k.isdigit() and int(k) < 1000)


def validate_sequence(keys: Iterable[str], kind: str = "hotkey") -> list[str]:
    """严格校验按键序列，返回规范名列表；不合法抛 KeySequenceError。

    - `hotkey`：0 个或多个修饰键 + 恰好一个按键（快捷键总要有“按下的那个键”）。
    - `key_down` / `key_up`：修饰键可单独按下（`key_down ["shift"]` 合法），但至多一个非修饰键。
    - 每个非修饰键必须是单字符或规范的命名键；多字符又不是键名的（例如整段密码）非法，
      必须拆成独立动作，重新观察 / 过闸。
    """
    if not isinstance(keys, list) or not all(isinstance(k, str) and k.strip() for k in keys):
        raise KeySequenceError("keys must be a non-empty list of key names")
    ks = split_keys(keys)
    if not ks:
        raise KeySequenceError("empty key sequence")
    rest = [k for k in ks if k not in MODIFIERS]
    for k in rest:
        if not is_named_or_char_key(k):
            raise KeySequenceError(
                f"a {len(k)}-character key string is not a valid key name; a multi-character key string (e.g. text plus Enter) "
                f"must be split into separate actions")
    if kind == "hotkey":
        if len(rest) != 1:
            raise KeySequenceError(
                f"hotkey must be modifiers plus exactly one key, got {len(rest)} keys; "
                f"press a sequence of keys as separate actions")
    elif len(rest) > 1:
        raise KeySequenceError(
            f"{kind} may hold at most one non-modifier key, got {len(rest)} keys")
    return ks

"""Android 环境（adb，无需在手机上装任何 App）。

- 截图：adb exec-out screencap -p
- 元素：adb shell uiautomator dump /sdcard/gua_ui.xml && cat（解析见 env/a11y.py）
- 输入：input tap / swipe / text / keyevent；long_press = 原地 swipe 持续 N 毫秒
- 前台：dumpsys window | grep mCurrentFocus（作为 active_window，用于焦点 / 应用被切走检测）
- open_app：monkey -p <package> -c android.intent.category.LAUNCHER 1

截图像素 == input 坐标（物理像素），无需缩放。
非 ASCII 文本：`input text` 不支持中文，若安装了 ADBKeyboard 则用广播输入，否则返回 unsupported。

所有 adb 调用都经过可注入的 runner(args:list[str], binary:bool) → bytes|str，
因此可以在没有设备的机器上用假 runner 做单元测试（tests/test_android_env.py）。

v0.3（审查条目 7）：`adb shell <字符串>` 会被设备上的 sh 解析，所以每条命令都先构造成 argv，
每个参数 shlex.quote 后再拼接；不再用 `&&` 串联（多条命令逐条发送）；包名 / Activity 与键名先做白名单校验。
v0.2 的反斜杠转义漏掉了换行（`a\nreboot` 会执行 reboot），包名与键名完全未转义。
"""
from __future__ import annotations

import io
import re
import shlex
import subprocess
import time
from typing import Callable, Optional

from PIL import Image

from ..actions import Action
from ..keys import KeySequenceError, canonical_key, validate_sequence
from .a11y import android_xml_to_elements
from .base import Env, ExecResult, Observation
from .commands import ANDROID_PACKAGE

# 规范键名（gua.keys）→ Android keycode。delete = 向前删除 KEYCODE_FORWARD_DEL(112)，backspace = KEYCODE_DEL(67)
KEYCODES = {"back": 4, "home": 3, "enter": 66, "return": 66, "delete": 112, "backspace": 67, "del": 112,
            "tab": 61, "esc": 111, "escape": 111, "menu": 82, "search": 84, "up": 19, "down": 20,
            "left": 21, "right": 22, "space": 62, "power": 26, "volume_up": 24, "volume_down": 25,
            "app_switch": 187, "recent": 187, "pageup": 92, "pagedown": 93, "ctrl": 113, "alt": 57,
            "shift": 59, "meta": 117, "dpad_center": 23, "insert": 124, "f1": 131, "f4": 134}
_KEYCODE_NAME = re.compile(r"^KEYCODE_[A-Z0-9_]+$")


class InvalidCommand(ValueError):
    pass


def keycode(k: str) -> str:
    c = canonical_key(k)
    if c in KEYCODES:
        return str(KEYCODES[c])
    ks = str(k).strip()
    if ks.isdigit() and int(ks) < 1000:
        return ks
    if _KEYCODE_NAME.match(ks):
        return ks
    raise InvalidCommand(f"invalid key {k!r}")


def check_package(app: str) -> str:
    a = (app or "").strip()
    if not ANDROID_PACKAGE.match(a):
        raise InvalidCommand(f"invalid app/package name {app!r}")
    return a
Runner = Callable[[list, bool], object]


def escape_input_text(s: str) -> str:
    """（v0.2 遗留，已不再使用：漏掉了换行等字符）`adb shell input text` 的反斜杠转义。v0.3 改为 argv + shlex.quote。"""
    out = []
    for ch in s:
        if ch == " ":
            out.append("%s")
        elif ch in "\\'\"`$&|;<>()*?~#!{}[]%":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


class AndroidEnv(Env):
    platform = "android"
    scroll_unit_px = 300

    def __init__(self, serial: Optional[str] = None, adb: str = "adb", max_elements: int = 150,
                 runner: Optional[Runner] = None, adb_keyboard: bool = False, dump_retries: int = 2):
        self.serial = serial
        self.adb = adb
        self.max_elements = max_elements
        self.adb_keyboard = adb_keyboard
        self.dump_retries = dump_retries
        self._run = runner or self._subprocess_runner
        self._size: Optional[tuple[int, int]] = None

    # ---------------------------------------------------------------- adb
    def _base(self) -> list[str]:
        return [self.adb] + (["-s", self.serial] if self.serial else [])

    def _subprocess_runner(self, args: list, binary: bool = False):
        r = subprocess.run(self._base() + args, capture_output=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode(errors="ignore")[:300])
        return r.stdout if binary else r.stdout.decode(errors="ignore")

    def shell(self, cmd: str) -> str:
        return str(self._run(["shell", cmd], False))

    # ---------------------------------------------------------------- 观察
    def screen_size(self) -> tuple[int, int]:
        if self._size is None:
            out = self.shell("wm size")
            m = re.findall(r"(\d+)x(\d+)", out)
            self._size = (int(m[-1][0]), int(m[-1][1])) if m else (1080, 2400)  # Override size 优先（最后一个）
        return self._size

    def screenshot(self) -> Image.Image:
        data = self._run(["exec-out", "screencap", "-p"], True)
        img = Image.open(io.BytesIO(data)).convert("RGB")
        self._size = img.size
        return img

    def dump_ui(self) -> str:
        last = ""
        for _ in range(self.dump_retries + 1):
            out = self.shell("uiautomator dump /sdcard/gua_ui.xml >/dev/null && cat /sdcard/gua_ui.xml")
            if "<hierarchy" in out:
                return out
            last = out
            time.sleep(0.5)  # 动画中 dump 常失败："could not get idle state"
        raise RuntimeError(f"uiautomator dump failed: {last[:200]}")

    def foreground(self) -> tuple[str, str]:
        out = self.shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'")
        m = re.search(r"mCurrentFocus=Window\{[^ ]+ [^ ]+ ([^}\s]+)\}", out) or re.search(r"(\S+/\S+)", out)
        comp = m.group(1) if m else ""
        return comp, comp.split("/")[0] if "/" in comp else comp

    def observe(self, with_elements: bool = True) -> Observation:
        img = self.screenshot()
        elems, text = [], ""
        if with_elements:
            try:
                elems, text = android_xml_to_elements(self.dump_ui(), img.size, self.max_elements)
            except Exception as e:  # 纯视觉降级
                text = f"(ui dump failed: {e})"
        comp, pkg = ("", "")
        try:
            comp, pkg = self.foreground()
        except Exception:
            pass
        return Observation(screenshot=img, timestamp=time.time(), screen_size=img.size, dpi_scale=1.0,
                           active_window=comp, active_process=pkg, windows=[comp] if comp else [],
                           elements=elems, platform="android", text=text)

    # ---------------------------------------------------------------- 执行
    def command_for(self, a: Action) -> Optional[str]:
        """日志 / 测试用：多条命令以 ' && ' 显示（执行时逐条发送，见 commands_for）。None 表示不支持。"""
        cmds = self.commands_for(a)
        return None if cmds is None else " && ".join(shlex.join(c) for c in cmds)

    def commands_for(self, a: Action) -> Optional[list[list[str]]]:
        """把动作翻译成若干条 argv（每个参数之后会被 shlex.quote）。返回 None 表示不支持；参数非法抛 InvalidCommand。"""
        x, y = (str(int(a.x)), str(int(a.y))) if a.x is not None else (None, None)
        if a.type == "click":
            return [["input", "tap", x, y]]
        if a.type == "double_click":
            return [["input", "tap", x, y], ["input", "tap", x, y]]
        if a.type == "long_press":
            ms = str(int((a.seconds or 0.8) * 1000))
            return [["input", "swipe", x, y, x, y, ms]]
        if a.type == "drag":
            return [["input", "swipe", x, y, str(int(a.x2)), str(int(a.y2)), "400"]]
        if a.type == "scroll":
            w, h = self.screen_size()
            cx, cy = (int(a.x), int(a.y)) if a.x is not None else (w // 2, h // 2)
            d = min(a.amount * self.scroll_unit_px, int(h * 0.4))
            # 手指方向与内容滚动方向相反：内容向下滚 = 手指向上滑
            ex, ey = {"down": (cx, cy - d), "up": (cx, cy + d), "left": (cx + d, cy), "right": (cx - d, cy)}[a.direction]
            return [["input", "swipe", str(cx), str(cy), str(max(0, ex)), str(max(0, ey)), "300"]]
        if a.type == "type":
            parts: list[list[str]] = []
            if a.clear:
                parts.append(["input", "keyevent", "KEYCODE_MOVE_END"])
                parts.append(["input", "keyevent"] + ["67"] * 40)
            txt = a.text or ""
            if txt.isascii():
                if txt:
                    # `input text` 把 %s 解释为空格；参数本身由 shlex.quote 保护，换行 / ; / $() 都无法逃逸
                    parts.append(["input", "text", txt.replace(" ", "%s")])
            elif self.adb_keyboard:
                parts.append(["am", "broadcast", "-a", "ADB_INPUT_TEXT", "--es", "msg", txt])
            else:
                return None
            if a.submit:
                parts.append(["input", "keyevent", "66"])
            return parts
        if a.type == "hotkey":
            # 第三轮条目 1：直接执行入口也要拒绝非法序列（tab+enter / 多字符 secret+enter），
            # 不能把一串按键当成一个动作顺次 keyevent 出去。
            try:
                ks = validate_sequence(a.keys, kind=a.type)
            except KeySequenceError as e:
                raise InvalidCommand(str(e)) from None
            return [["input", "keyevent"] + [keycode(k) for k in ks]]
        if a.type == "back":
            return [["input", "keyevent", "4"]]
        if a.type == "home":
            return [["input", "keyevent", "3"]]
        if a.type == "open_app":
            app = check_package(a.app or a.text or "")
            if "/" in app:
                return [["am", "start", "-n", app]]
            return [["monkey", "-p", app, "-c", "android.intent.category.LAUNCHER", "1"]]
        return None

    def execute(self, a: Action) -> ExecResult:
        t0 = time.time()
        if not self.supports(a.type):
            return ExecResult(False, f"unsupported action {a.type} on android", t0, time.time())
        if a.type == "wait":
            time.sleep(min(a.seconds or 1.0, 10.0))
            return ExecResult(True, "", t0, time.time())
        w, h = self.screen_size()
        err = self._bounds_error(a, w, h)
        if err:
            return ExecResult(False, err, t0, time.time())
        try:
            cmds = self.commands_for(a)
        except InvalidCommand as e:
            return ExecResult(False, f"invalid_argument: {e}", t0, time.time())
        if cmds is None:
            if a.type == "type":
                return ExecResult(False, "unsupported non-ASCII text without ADBKeyboard", t0, time.time())
            return ExecResult(False, f"unsupported action {a.type}", t0, time.time())
        try:
            out = ""
            for argv in cmds:
                out += self.shell(shlex.join(argv))
            if a.type == "open_app" and ("No activities found" in out or "Error" in out):
                return ExecResult(False, f"app_not_found {a.app}", t0, time.time())
            return ExecResult(True, "", t0, time.time(), output=out[:200])
        except Exception as e:  # noqa: BLE001
            return ExecResult(False, f"{type(e).__name__}: {e}", t0, time.time())

    def focus_window(self, title_substring: str) -> bool:
        """Android 上“恢复焦点”= 把任务 App 拉回前台。title_substring 视作包名。"""
        if not title_substring:
            return False
        r = self.execute(Action("open_app", app=title_substring))
        return r.ok

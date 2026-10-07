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
from .a11y import android_xml_to_elements
from .base import Env, ExecResult, Observation

KEYCODES = {"back": 4, "home": 3, "enter": 66, "return": 66, "delete": 67, "backspace": 67, "del": 112,
            "tab": 61, "esc": 111, "escape": 111, "menu": 82, "search": 84, "up": 19, "down": 20,
            "left": 21, "right": 22, "space": 62, "power": 26, "volume_up": 24, "volume_down": 25,
            "app_switch": 187, "recent": 187}
Runner = Callable[[list, bool], object]


def escape_input_text(s: str) -> str:
    """`adb shell input text` 的转义：空格→%s，shell 元字符加反斜杠。"""
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
        """把动作翻译成一条 adb shell 命令（便于测试与日志）。返回 None 表示无需 shell。"""
        x, y = (int(a.x), int(a.y)) if a.x is not None else (None, None)
        if a.type == "click":
            return f"input tap {x} {y}"
        if a.type == "double_click":
            return f"input tap {x} {y} && input tap {x} {y}"
        if a.type == "long_press":
            ms = int((a.seconds or 0.8) * 1000)
            return f"input swipe {x} {y} {x} {y} {ms}"
        if a.type == "drag":
            return f"input swipe {x} {y} {int(a.x2)} {int(a.y2)} 400"
        if a.type == "scroll":
            w, h = self.screen_size()
            cx, cy = (x, y) if x is not None else (w // 2, h // 2)
            d = min(a.amount * self.scroll_unit_px, int(h * 0.4))
            # 手指方向与内容滚动方向相反：内容向下滚 = 手指向上滑
            ex, ey = {"down": (cx, cy - d), "up": (cx, cy + d), "left": (cx + d, cy), "right": (cx - d, cy)}[a.direction]
            return f"input swipe {cx} {cy} {max(0, ex)} {max(0, ey)} 300"
        if a.type == "type":
            parts = []
            if a.clear:
                parts.append("input keyevent KEYCODE_MOVE_END && input keyevent " + " ".join(["67"] * 40))
            txt = a.text or ""
            if txt.isascii():
                parts.append(f"input text {escape_input_text(txt)}" if txt else "true")
            elif self.adb_keyboard:
                parts.append(f"am broadcast -a ADB_INPUT_TEXT --es msg {shlex.quote(txt)}")
            else:
                return None
            if a.submit:
                parts.append("input keyevent 66")
            return " && ".join(parts)
        if a.type == "hotkey":
            codes = [str(KEYCODES.get(k.lower(), k)) for k in a.keys]
            return "input keyevent " + " ".join(codes)
        if a.type == "back":
            return "input keyevent 4"
        if a.type == "home":
            return "input keyevent 3"
        if a.type == "open_app":
            app = a.app or a.text or ""
            if "/" in app:
                return f"am start -n {app}"
            return f"monkey -p {app} -c android.intent.category.LAUNCHER 1"
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
        cmd = self.command_for(a)
        if cmd is None:
            if a.type == "type":
                return ExecResult(False, "unsupported non-ASCII text without ADBKeyboard", t0, time.time())
            return ExecResult(False, f"unsupported action {a.type}", t0, time.time())
        try:
            out = self.shell(cmd)
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

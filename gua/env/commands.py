"""启动应用 / 激活窗口的命令构造（审查条目 7：防命令注入）。纯函数，不执行，可在任何平台离线测试。

原则：
- 绝不使用 shell=True，也不经过 `cmd /c start`（cmd 会重新解析 & | ^ % 等元字符）；
- 一律构造 argv 列表；应用名先校验（拒绝 shell 元字符、换行、以 '-' 开头的“伪选项”）；
- macOS AppleScript：应用名通过 `on run argv` 作为参数传入，而不是拼进脚本源码；
- Android：见 env/android.py（每个参数 shlex.quote，包名 / 键名白名单校验）。
"""
from __future__ import annotations

import re
import shutil
from typing import Callable, Optional

# 允许：字母数字、空格、. _ - + ( ) [ ] , 以及路径分隔符 / \ :（Windows 盘符）
_APP_OK = re.compile(r"^[\w .+\-()\[\],/\\:]+$", re.UNICODE)
_APP_BAD = re.compile(r"[&|;<>^%$`\"'\n\r\x00*?!{}~#=]")
ANDROID_PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+(/[A-Za-z0-9_.$]+)?$")


class InvalidAppName(ValueError):
    pass


def validate_app_name(app: Optional[str]) -> str:
    a = (app or "").strip()
    if not a:
        raise InvalidAppName("invalid app name: empty")
    if a.startswith("-") or _APP_BAD.search(a) or not _APP_OK.match(a) or ".." in a.replace("\\", "/").split("/"):
        raise InvalidAppName(f"invalid app name {app!r}: only letters, digits, spaces, . _ - + ( ) and paths allowed")
    return a


def windows_open_app_argv(app: str, which: Callable[[str], Optional[str]] = shutil.which) -> Optional[list[str]]:
    """可执行文件（notepad / calc / C:\\...\\x.exe）→ [绝对路径]；找不到返回 None（调用方改用 os.startfile）。"""
    a = validate_app_name(app)
    exe = which(a) or (which(a + ".exe") if not a.lower().endswith(".exe") else None)
    return [exe] if exe else None


def macos_open_app_argv(app: str) -> list[str]:
    return ["open", "-a", validate_app_name(app)]


APPLESCRIPT_ACTIVATE = "on run argv\n  tell application (item 1 of argv) to activate\nend run"


def macos_activate_argv(app: str) -> list[str]:
    """应用名作为 argv 传给 `on run argv`，不进入 AppleScript 源码，引号 / 换行都无法逃逸。"""
    return ["osascript", "-e", APPLESCRIPT_ACTIVATE, app]


def linux_open_app_argv(app: str) -> list[str]:
    """Linux：单个可执行文件名或路径，不接受附带参数（参数里可能是 `bash -c ...`）。"""
    a = validate_app_name(app)
    if " " in a and not shutil.which(a):
        raise InvalidAppName(f"invalid app name {app!r}: arguments are not allowed, give an executable name")
    return [a]

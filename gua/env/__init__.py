"""平台后端：统一 Env 接口 + 自动检测。

各后端按需懒加载，所以在任意机器上 `import gua` 都不会因为缺少 pyobjc / uiautomation / playwright 失败。
"""
from __future__ import annotations

import os
import shutil
import sys
from typing import Any

from .base import Env, ExecResult, Observation, UIElement

PLATFORMS = ("windows", "macos", "linux", "android", "web", "mock")


def detect_platform() -> str:
    """auto 检测：本机桌面平台。Android / Web 需要显式指定（它们不是“本机”）。"""
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux" if os.environ.get("DISPLAY") else "mock"
    return "mock"


def platform_status() -> dict[str, str]:
    """`gua doctor` 用：每个后端的依赖是否就绪（不实际连接设备）。"""
    import importlib.util as u

    def has(m: str) -> bool:
        try:
            return u.find_spec(m) is not None
        except (ImportError, ValueError):
            return False
    st = {
        "windows": "ok" if sys.platform == "win32" and has("mss") and has("pyautogui") and has("uiautomation")
        else "missing deps: pip install 'gui-agent[windows]'" if sys.platform == "win32" else "not this OS",
        "macos": "ok" if sys.platform == "darwin" and has("pyautogui") and has("ApplicationServices")
        else "missing deps: pip install 'gui-agent[macos]'" if sys.platform == "darwin" else "not this OS",
        "linux": ("ok" if has("mss") and has("pyautogui") and os.environ.get("DISPLAY") else
                  "needs X11 DISPLAY + pip install 'gui-agent[linux]'") + ("" if has("pyatspi") else " (no AT-SPI: vision only)")
        if sys.platform.startswith("linux") else "not this OS",
        "android": "ok" if shutil.which("adb") else "adb not found in PATH",
        "web": "ok" if has("playwright") else "pip install 'gui-agent[web]' && playwright install chromium",
        "mock": "ok",
    }
    return st


def make_env(platform: str = "auto", **kw: Any) -> Env:
    p = detect_platform() if platform in (None, "", "auto") else platform
    if p == "windows":
        from .windows import WindowsEnv
        return WindowsEnv(**_pick(kw, "max_elements", "uia_depth", "monitor"))
    if p == "macos":
        from .macos import MacOSEnv
        return MacOSEnv(**_pick(kw, "max_elements", "use_ax"))
    if p == "linux":
        from .linux import LinuxEnv
        return LinuxEnv(**_pick(kw, "max_elements", "use_atspi", "monitor"))
    if p == "android":
        from .android import AndroidEnv
        return AndroidEnv(**_pick(kw, "serial", "adb", "max_elements", "adb_keyboard"))
    if p == "web":
        from .web import WebEnv
        kw2 = _pick(kw, "start_url", "headless", "browser", "allowed_domains", "max_elements", "slow_mo",
                    "block_subresources")
        if "viewport" in kw:
            kw2["viewport"] = tuple(kw["viewport"])
        return WebEnv(**kw2)
    if p == "mock":
        from .mock import MockEnv
        return MockEnv()
    raise ValueError(f"unknown platform {p!r}; choose from {PLATFORMS}")


def _pick(d: dict, *keys: str) -> dict:
    return {k: d[k] for k in keys if k in d and d[k] is not None}


__all__ = ["Env", "ExecResult", "Observation", "UIElement", "detect_platform", "make_env", "platform_status",
           "PLATFORMS"]

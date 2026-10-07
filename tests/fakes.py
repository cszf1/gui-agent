"""测试替身：假 pyautogui / mss，以及在 Linux 上导入 Windows / macOS 后端模块（只测命令构造与异常传播，不执行）。"""
from __future__ import annotations

import importlib
import sys
import types


def fake_pyautogui(raise_on: tuple[str, ...] = ()) -> types.ModuleType:
    m = types.ModuleType("pyautogui")

    class FailSafeException(Exception):
        pass

    m.FailSafeException = FailSafeException
    m.FAILSAFE, m.PAUSE = True, 0.0
    m.calls = []

    def mk(name):
        def f(*a, **k):
            m.calls.append((name, a, k))
            if name in raise_on:
                raise FailSafeException("mouse moved to a screen corner")
        return f
    for n in ["click", "doubleClick", "rightClick", "moveTo", "mouseDown", "mouseUp", "dragTo", "scroll",
              "hscroll", "hotkey", "keyDown", "keyUp", "press", "write"]:
        setattr(m, n, mk(n))
    m.size = lambda: (1440, 900)
    m.position = lambda: (10, 10)
    return m


def import_platform_module(monkeypatch, name: str, fake_platform: str):
    """在当前机器上导入 gua.env.<name>（伪装 sys.platform，注入假 mss），用完由 monkeypatch 还原。"""
    monkeypatch.setitem(sys.modules, "mss", types.ModuleType("mss"))
    real = sys.platform
    sys.modules.pop(f"gua.env.{name}", None)
    sys.platform = fake_platform
    try:
        mod = importlib.import_module(f"gua.env.{name}")
    finally:
        sys.platform = real          # 只在导入期间伪装
        sys.modules.pop(f"gua.env.{name}", None)
    return mod

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIX = Path(__file__).resolve().parent / "fixtures"


def fast(env, timeout=0.5, interval=0.01):
    """让 mock 环境的 wait_until_stable 快一点。"""
    orig = env.wait_until_stable
    env.wait_until_stable = lambda timeout_=timeout, **kw: orig(timeout=timeout, interval=interval)
    return env


def hide_playwright() -> None:
    """模拟没有安装 Playwright 的环境（例如审查者的 Windows 机器）：让 `import playwright` 抛 ImportError，
    Web 测试模块的 pytest.importorskip 因此整体跳过。设置环境变量 GUA_TEST_NO_PLAYWRIGHT=1 时自动启用：

        GUA_TEST_NO_PLAYWRIGHT=1 python -B -m pytest -q -p no:cacheprovider
    """
    for name in ("playwright", "playwright.sync_api", "playwright.async_api"):
        sys.modules[name] = None      # type: ignore[assignment]  # None 在 sys.modules 中表示“导入失败”


if os.environ.get("GUA_TEST_NO_PLAYWRIGHT", "").strip() not in {"", "0", "false", "no"}:
    hide_playwright()

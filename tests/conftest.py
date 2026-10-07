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

"""Freeze Python + platform backends + Chromium for a self-contained installer.

Run this on the target OS, using a Python environment with that OS's extras.
The Windows CI workflow does this before electron-builder creates the installer.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

DESKTOP = Path(__file__).resolve().parents[1]
ROOT = DESKTOP.parent
RESOURCES = DESKTOP / "resources"


def main():
    import importlib.util
    from gua.env import platform_status

    if importlib.util.find_spec("PyInstaller") is None:
        raise SystemExit("Install pyinstaller into this Python environment first.")
    platform = "windows" if sys.platform == "win32" else "macos" if sys.platform == "darwin" else "linux"
    if platform_status()[platform].startswith("missing"):
        raise SystemExit(f"Install gui-agent[{platform},web] before building the worker.")
    RESOURCES.mkdir(exist_ok=True)
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
               "--name", "gua-worker", "--distpath", str(RESOURCES),
               "--workpath", str(DESKTOP / "build/worker"),
               "--specpath", str(DESKTOP / "build"),
               "--paths", str(ROOT), "--collect-submodules", "gua",
               "--collect-all", "playwright", "--collect-all", "openai",
               "--add-data", f"{ROOT / 'configs'}{os.pathsep}configs",
               "--add-data", f"{ROOT / 'tasks'}{os.pathsep}tasks"]
    if sys.platform == "win32":
        command += ["--collect-all", "uiautomation", "--collect-all", "mss"]
    elif sys.platform == "darwin":
        command += ["--collect-all", "Quartz", "--collect-all", "ApplicationServices"]
    else:
        command += ["--collect-all", "mss"]
    subprocess.run([*command, str(DESKTOP / "scripts/worker_entry.py")], cwd=ROOT, check=True)
    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(RESOURCES / "browsers")}
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], env=env, check=True)
    print(f"Bundled worker and browser are ready in {RESOURCES}")


if __name__ == "__main__":
    main()

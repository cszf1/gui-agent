"""Actual portable launcher/Edge/worker/DPAPI, on a disposable Windows CI user."""
from __future__ import annotations

import ctypes
import json
import os
import runpy
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

import psutil

_helpers = runpy.run_path(str(Path(__file__).with_name("smoke-windows-installer.py")))
KEYS, registry_exists, wait_for = (_helpers[name] for name in ("KEYS", "registry_exists", "wait_for"))


def main():
    if sys.platform != "win32" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Run only on a disposable Windows GitHub Actions runner")
    from gua.app_native import EdgeWindow
    from gua.windows_app import read_connection
    archive = next((Path(__file__).resolve().parents[1] / "release").glob("*-Portable.zip"))
    shared_data = Path(os.environ["APPDATA"]) / "GUI Agent"
    assert not shared_data.exists() and not any(registry_exists(k) for k in KEYS)
    with tempfile.TemporaryDirectory(prefix="gua-portable-smoke-") as temporary:
        base = Path(temporary) / "便携 GUI Agent"
        with zipfile.ZipFile(archive) as z:
            z.extractall(base)
        root, backend = base / "GUI-Agent", None
        data = root / "data"
        original = {str(p.relative_to(root)): p.stat().st_size for p in root.rglob("*") if p.is_file()}
        opener = build_opener(ProxyHandler({}))
        def api(name, body=None):
            origin, token = read_connection(data)
            req = Request(origin + "/api/" + name, data=json.dumps(body or {}).encode(),
                          headers={"Content-Type": "application/json", "X-Gua-Control": token})
            with opener.open(req, timeout=20) as r:
                return json.load(r)["value"]
        def launch():
            subprocess.run([str(root / "GUIAgent.exe")], check=True, timeout=10)
            def live():
                record = json.loads((data / "cache/instance.json").read_text())
                process = psutil.Process(record["pid"])
                if process.create_time() == record["created"] and Path(process.exe()).resolve() == (root / "runtime/pythonw.exe").resolve():
                    return process
            process = wait_for(live, "portable embedded backend")
            state = wait_for(lambda: (s if (s := api("load"))["runtime"]["uiConnected"] else None),
                             "portable real Edge UI", 90)
            return process, state
        try:
            backend, state = launch()
            assert data.is_dir() and (root / "portable.flag").is_file()
            storage = api("storage")
            assert storage["portable"] is True and Path(storage["dataPath"]).resolve() == data.resolve()
            settings = {k: v for k, v in state["settings"].items() if k not in {"hasApiKey", "keyPersisted"}}
            fake_key = "portable-smoke-key-not-a-real-credential"
            assert api("settings", {**settings, "model": "portable-test", "apiKey": fake_key})["keyPersisted"]
            assert fake_key not in (data / "state.json").read_text(encoding="utf-8")
            run = api("start", dict(sessionId=state["sessions"][-1]["id"], task="portable form demo", demo=True))
            def finished():
                rows = [r for s in api("load")["sessions"] for r in s["runs"] if r["id"] == run["id"]]
                return rows[0] if rows and rows[0]["status"] in {"done", "fail", "error", "uncertain"} else None
            assert wait_for(finished, "portable embedded demo task", 90)["status"] == "done"
            class Directories:
                cache, temp = data / "cache", data / "tmp"
            ui = EdgeWindow(Directories())
            hwnd = wait_for(ui.hwnd, "portable owned Edge app")
            from ctypes import wintypes
            user = ctypes.WinDLL("user32")
            user.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
            assert user.PostMessageW(hwnd, 0x10, 0, 0)
            backend.wait(timeout=35); backend = None
            assert not ui.owned_processes() and not list((data / "tmp").iterdir()) and not list((data / "cache").iterdir())
            backend, restored = launch()
            assert restored["settings"]["keyPersisted"] and restored["settings"]["model"] == "portable-test"
            api("shutdown"); backend.wait(timeout=35); backend = None
            remaining = {str(p.relative_to(root)): p.stat().st_size for p in root.rglob("*")
                         if p.is_file() and data not in p.parents}
            assert remaining == original, "Portable execution modified program files"
            assert not shared_data.exists() and not any(registry_exists(k) for k in KEYS)
            print(json.dumps(dict(portableLauncher=True, systemEdgeUI=True, embeddedDemo=True, dpapiRestart=True,
                                  adjacentDataOnly=True, noInstallRegistry=True, cleanExit=True, unicodePath=True)))
        finally:
            if backend is not None and backend.is_running():
                try: api("shutdown"); backend.wait(timeout=30)
                except (OSError, psutil.Error): backend.kill(); backend.wait(timeout=10)


if __name__ == "__main__":
    main()

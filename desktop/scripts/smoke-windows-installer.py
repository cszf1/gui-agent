"""Real install/UI/Edge/DPAPI/upgrade/uninstall checks on a disposable CI user."""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
import winreg
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

import psutil

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = Path(os.environ["LOCALAPPDATA"]) / "Programs/GUI Agent"
DATA = Path(os.environ["APPDATA"]) / "GUI Agent"
KEYS = [r"Software\cszf1\GUI Agent", r"Software\Microsoft\Windows\CurrentVersion\Uninstall\GUIAgent"]


def registry_exists(name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, name): return True
    except FileNotFoundError: return False


def wait_for(fn, label, timeout=45):
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            value = fn()
            if value: return value
        except (OSError, ValueError, KeyError, psutil.Error) as exc: last = type(exc).__name__
        time.sleep(0.2)
    raise AssertionError(f"Timeout: {label}; last error: {last}")


def shortcuts():
    from ctypes import wintypes
    shell = ctypes.WinDLL("shell32")
    shell.SHGetFolderPathW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR]
    result = []
    for identifier in (0x10, 0x02):  # actual current-user Desktop and Programs
        value = ctypes.create_unicode_buffer(260)
        assert shell.SHGetFolderPathW(None, identifier, None, 0, value) == 0
        result.append(Path(value.value) / "GUI Agent.lnk")
    return result


def main():
    if sys.platform != "win32" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Run only on a disposable Windows GitHub Actions runner")
    from gua.app_native import EdgeWindow
    from gua.windows_app import read_connection
    from playwright.sync_api import sync_playwright
    setup = next((ROOT / "release").glob("*-Setup.exe"))
    links = shortcuts()
    assert not PROGRAM.exists() and not DATA.exists() and not any(registry_exists(k) for k in KEYS)
    assert not any(p.exists() for p in links), "Refusing to overwrite an existing user's shortcuts"
    installed, backend = False, None
    opener = build_opener(ProxyHandler({}))
    def api(name, body=None):
        origin, token = read_connection(DATA)
        req = Request(origin + "/api/" + name, data=json.dumps(body or {}).encode(),
                      headers={"Content-Type": "application/json", "X-Gua-Control": token})
        with opener.open(req, timeout=20) as response: return json.load(response)["value"]
    def launch():
        subprocess.run([str(PROGRAM / "GUIAgent.exe")], check=True, timeout=10)
        record = wait_for(lambda: json.loads((DATA / "cache/instance.json").read_text()), "backend startup")
        process = psutil.Process(record["pid"])
        assert process.create_time() == record["created"]
        assert Path(process.exe()).resolve() == (PROGRAM / "runtime/pythonw.exe").resolve()
        state = wait_for(lambda: (s if (s := api("load"))["runtime"]["uiConnected"] else None), "real Edge React UI connection", 90)
        return process, state
    def inventory():
        return {str(p.relative_to(PROGRAM)): p.stat().st_size for p in PROGRAM.rglob("*") if p.is_file()}
    browser_cache = Path(os.environ["LOCALAPPDATA"]) / "ms-playwright"
    before_cache = set(browser_cache.iterdir()) if browser_cache.exists() else set()
    with tempfile.TemporaryDirectory(prefix="gua-uninstall-external-") as outside, sync_playwright() as pw:
        document = Path(outside) / "user-document.txt"
        document.write_text("user document must survive", encoding="utf-8")
        # A separate, unrelated Edge must survive both app close and uninstall.
        other_edge = pw.chromium.launch(channel="msedge", headless=True)
        page = other_edge.new_page(); page.set_content("<title>Other Edge</title><p>keep alive</p>")
        try:
            subprocess.run([str(setup), "/S"], check=True, timeout=120)
            installed = True
            assert (PROGRAM / "GUIAgent.exe").is_file() and (PROGRAM / "runtime/python313._pth").is_file()
            assert all(p.is_file() for p in links) and all(registry_exists(k) for k in KEYS)
            baseline = inventory()
            backend, state = launch()
            class Directories:
                cache, temp = DATA / "cache", DATA / "tmp"
            ui = EdgeWindow(Directories())
            hwnd = wait_for(ui.hwnd, "owned system Edge app window")
            own_processes = ui.owned_processes()
            assert own_processes and all(p.name().lower() == "msedge.exe" for p in own_processes)
            assert state["runtime"]["engine"] == "embedded Python + system Edge"
            assert state["runtime"]["shortcutAvailable"], "Emergency stop hotkey unavailable"
            assert state["settings"]["saveScreenshots"] is False
            settings = {k: v for k, v in state["settings"].items() if k not in {"hasApiKey", "keyPersisted"}}
            fake_key = "installer-smoke-key-not-a-real-credential"
            saved = api("settings", {**settings, "model": "test-model", "apiKey": fake_key})
            assert saved["hasApiKey"] and saved["keyPersisted"]
            assert fake_key not in (DATA / "state.json").read_text(encoding="utf-8")
            run = api("start", dict(sessionId=state["sessions"][-1]["id"], task="CI offline form", demo=True))
            def finished():
                rows = [r for s in api("load")["sessions"] for r in s["runs"] if r["id"] == run["id"]]
                if rows and rows[0]["status"] in {"done", "fail", "error", "uncertain"}: return rows[0]
            result = wait_for(finished, "embedded Python task using installed system Edge", 90)
            assert result["status"] == "done", {k: result.get(k) for k in ("status", "result", "events")}
            assert result["result"]["claimed_done"] and result["reportReady"]
            assert not (DATA / "runs" / run["id"] / "shots").exists()
            # Closing the real UI window must end the backend and owned Edge.
            from ctypes import wintypes
            user = ctypes.WinDLL("user32")
            user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            assert user.PostMessageW(hwnd, 0x10, 0, 0)
            backend.wait(timeout=35); backend = None
            assert not ui.owned_processes(), "Owned Edge remained after window close"
            assert not list((DATA / "tmp").iterdir()) and not list((DATA / "cache").iterdir())
            assert (DATA / "state.json").is_file()
            assert inventory() == baseline, "Running the app wrote into its program directory"
            assert (set(browser_cache.iterdir()) if browser_cache.exists() else set()) == before_cache
            assert page.title() == "Other Edge", "App close disturbed another Edge instance"
            # Upgrade/restart preserves settings, DPAPI key and history.
            subprocess.run([str(setup), "/S"], check=True, timeout=120)
            backend, restored = launch()
            assert restored["settings"]["model"] == "test-model" and restored["settings"]["keyPersisted"]
            assert any(r["id"] == run["id"] for s in restored["sessions"] for r in s["runs"])
            # A forced backend crash must close owned worker/Edge trees too.
            run = api("start", dict(sessionId=restored["sessions"][-1]["id"], task="CI crash cleanup", demo=True))
            assert wait_for(finished, "task before crash cleanup", 90)["status"] == "done"
            wait_for(ui.hwnd, "restarted owned Edge window")
            ui.owned_processes()
            descendants = backend.children(recursive=True)
            backend.kill(); backend.wait(timeout=10); backend = None
            wait_for(lambda: not ui.owned_processes(), "owned Edge ended after backend crash", 15)
            wait_for(lambda: not any(p.is_running() for p in descendants), "worker descendants ended after crash", 15)
            backend, restored = launch()
            assert not any((DATA / "tmp").glob("worker-*")), "Crash temp was not cleaned on restart"
            # Junctions on either cleanup surface must never delete user docs.
            for parent in (DATA / "cache", PROGRAM):
                subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(parent / "external-link"), outside],
                               check=True, stdout=subprocess.DEVNULL)
            subprocess.run([str(PROGRAM / "Uninstall.exe"), "/S"], check=True, timeout=120)
            backend.wait(timeout=25); backend = None
            wait_for(lambda: not PROGRAM.exists(), "program directory deletion", 30)
            assert not DATA.exists(), "User data remained after uninstall"
            assert not any(registry_exists(k) for k in KEYS), "Registry keys remained"
            assert not any(p.exists() for p in links), "Shortcuts remained"
            assert document.read_text(encoding="utf-8") == "user document must survive"
            assert page.title() == "Other Edge", "Uninstall disturbed another Edge instance"
            installed = False
            print(json.dumps(dict(systemEdgeUI=True, embeddedPythonTask=True, dpapiKey=True, noDefaultScreenshots=True,
                cleanExit=True, crashChildrenStopped=True, upgradePreservedData=True, cleanUninstall=True, externalDocumentsPreserved=True,
                unrelatedEdgePreserved=True, noBrowserDownload=True, programDirectoryUnchanged=True)))
        finally:
            if installed and (PROGRAM / "Uninstall.exe").exists():
                subprocess.run([str(PROGRAM / "Uninstall.exe"), "/S"], timeout=120)
            other_edge.close()


if __name__ == "__main__": main()

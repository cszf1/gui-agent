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
        def live_backend():
            # A crash leaves the old instance file until the new app cleans it.
            # Wait for a live process with the recorded creation time, not just
            # for the existence of that stale file (or a reused PID).
            record = json.loads((DATA / "cache/instance.json").read_text())
            candidate = psutil.Process(record["pid"])
            if (candidate.create_time() == record["created"]
                    and Path(candidate.exe()).resolve() == (PROGRAM / "runtime/pythonw.exe").resolve()
                    and "gua.windows_app" in candidate.cmdline()):
                return candidate
        process = wait_for(live_backend, "live backend startup")
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
            assert not links[0].exists() and links[1].is_file(), "Desktop shortcut must be opt-in"
            assert not registry_exists(KEYS[0]) and registry_exists(KEYS[1])
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEYS[1]) as key:
                command = '"' + str(PROGRAM / "Uninstall.exe") + '"'
                assert winreg.QueryValueEx(key, "UninstallString")[0] == command
                assert winreg.QueryValueEx(key, "QuietUninstallString")[0] == command + " /S"
            baseline = inventory()
            backend, state = launch()
            class Directories:
                cache, temp = DATA / "cache", DATA / "tmp"
            ui = EdgeWindow(Directories())
            hwnd = wait_for(ui.hwnd, "owned system Edge app window")
            own_processes = ui.owned_processes()
            assert own_processes and any(p.name().lower() == "msedge.exe" for p in own_processes), "System Edge root is missing"
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
            (PROGRAM / "runtime/obsolete-library.txt").write_text("old version", encoding="utf-8")
            (PROGRAM / "ui/obsolete-asset.txt").write_text("old version", encoding="utf-8")
            subprocess.run([str(setup), "/S"], check=True, timeout=120)
            assert not (PROGRAM / "runtime/obsolete-library.txt").exists()
            assert not (PROGRAM / "ui/obsolete-asset.txt").exists()
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
            vanished = Path(outside) / "vanished"; vanished.mkdir()
            subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(PROGRAM / "dangling-link"), str(vanished)],
                           check=True, stdout=subprocess.DEVNULL)
            vanished.rmdir()
            foreign = PROGRAM / "user-added-document.txt"
            foreign.write_text("user-added file must survive", encoding="utf-8")
            subprocess.run([str(PROGRAM / "Uninstall.exe"), "/S"], check=True, timeout=120)
            backend.wait(timeout=25); backend = None
            try:
                wait_for(lambda: not (PROGRAM / "runtime").exists() and not (PROGRAM / "Uninstall.exe").exists(),
                         "manifest uninstall completion", 30)
            except AssertionError:
                print("Remaining program entries:", json.dumps(sorted(str(p.relative_to(PROGRAM))
                      for p in PROGRAM.rglob("*")), ensure_ascii=True))
                raise
            assert foreign.read_text(encoding="utf-8") == "user-added file must survive"
            assert list(PROGRAM.iterdir()) == [foreign], "Application files remained after manifest uninstall"
            # The only remaining file belongs to this harness, not the app.
            foreign.unlink(); PROGRAM.rmdir()
            wait_for(lambda: not PROGRAM.exists(), "program directory deletion", 30)
            assert not DATA.exists(), "User data remained after uninstall"
            assert not any(registry_exists(k) for k in KEYS), "Registry keys remained"
            assert not any(p.exists() for p in links), "Shortcuts remained"
            assert document.read_text(encoding="utf-8") == "user document must survive"
            assert page.title() == "Other Edge", "Uninstall disturbed another Edge instance"
            installed = False
            print(json.dumps(dict(systemEdgeUI=True, embeddedPythonTask=True, dpapiKey=True, noDefaultScreenshots=True,
                cleanExit=True, crashChildrenStopped=True, upgradePreservedData=True, cleanUninstall=True, externalDocumentsPreserved=True,
                unrelatedEdgePreserved=True, userAddedFilesPreserved=True, noBrowserDownload=True, programDirectoryUnchanged=True)))
        finally:
            if installed and (PROGRAM / "Uninstall.exe").exists():
                subprocess.run([str(PROGRAM / "Uninstall.exe"), "/S"], timeout=120)
            other_edge.close()


if __name__ == "__main__": main()

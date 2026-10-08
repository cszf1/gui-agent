"""Build a per-user NSIS installer: embedded CPython, no Electron or browser."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

DESKTOP = Path(__file__).resolve().parents[1]
ROOT = DESKTOP.parent
PYTHON_VERSION = "3.13.9"
PYTHON_SHA256 = "91d828c2da3a029b41699e918674a0cb379c02cf20dab9c501306885f837402a"


def main():
    if sys.platform != "win32" or sys.version_info[:2] != (3, 13):
        raise SystemExit("Build on Windows x64 using Python 3.13")
    version = json.loads((DESKTOP / "package.json").read_text())["version"]
    output = DESKTOP / "release"
    payload = output / "GUI-Agent"
    if payload.exists(): shutil.rmtree(payload)
    runtime = payload / "runtime"
    runtime.mkdir(parents=True)
    cache = DESKTOP / "build/downloads"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"python-{PYTHON_VERSION}-embed-amd64.zip"
    if not archive.exists():
        urllib.request.urlretrieve(f"https://www.python.org/ftp/python/{PYTHON_VERSION}/{archive.name}", archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != PYTHON_SHA256: raise SystemExit("Embedded Python checksum mismatch")
    with zipfile.ZipFile(archive) as z: z.extractall(runtime)
    site = runtime / "Lib/site-packages"
    subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-compile",
                    "--target", str(site), str(ROOT) + "[windows,web]"], check=True)
    for path in list(site.rglob("__pycache__")):
        if path.exists(): shutil.rmtree(path)
    # Keep library licenses and metadata, remove test suites from the runtime.
    for package in ("numpy", "PIL", "psutil", "comtypes"):
        for path in list((site / package).rglob("tests")):
            if path.is_dir(): shutil.rmtree(path)
    (runtime / "python313._pth").write_text("python313.zip\n.\nLib/site-packages\nimport site\n", encoding="utf-8")
    for folder in ("configs", "tasks"): shutil.copytree(ROOT / folder, site / folder)
    shutil.copytree(DESKTOP / "dist-ui", payload / "ui")
    (payload / ".gui-agent-program").write_text("gui-agent-program-v1\n", encoding="utf-8")
    shutil.copy2(ROOT / "LICENSE", payload / "LICENSE.txt") if (ROOT / "LICENSE").exists() else None
    shutil.copy2(DESKTOP / "assets/icon.ico", payload / "app.ico")
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    subprocess.run([str(compiler), "/nologo", "/target:winexe", "/platform:x64", "/reference:System.Windows.Forms.dll",
                    "/win32icon:" + str(payload / "app.ico"), "/out:" + str(payload / "GUIAgent.exe"),
                    str(DESKTOP / "installer/launcher.cs")], check=True)
    files = [p for p in payload.rglob("*") if p.is_file()]
    forbidden = {"electron.exe", "chrome.exe", "chromium.exe", "msedge.exe"}
    if any(p.name.lower() in forbidden for p in files): raise SystemExit("A browser or Electron was bundled")
    if any(p.name.startswith(("chromium-", "chrome-headless-shell-")) for p in payload.rglob("*")):
        raise SystemExit("Bundled browser directory detected")
    makensis = shutil.which("makensis") or str(Path(os.environ["PROGRAMFILES(X86)"]) / "NSIS/makensis.exe")
    installer = output / f"GUI-Agent-{version}-x64-Setup.exe"
    subprocess.run([makensis, "/V2", "/DVERSION=" + version, "/DPAYLOAD=" + str(payload),
                    "/DOUTPUT=" + str(installer), "/DICON=" + str(payload / "app.ico"),
                    str(DESKTOP / "installer/installer.nsi")], check=True)
    if installer.stat().st_size >= 150 * 1024 * 1024: raise SystemExit("Installer exceeds the slim-build size gate")
    manifest = dict(version=version, embeddedPython=PYTHON_VERSION, installerBytes=installer.stat().st_size,
                    unpackedBytes=sum(p.stat().st_size for p in files), fileCount=len(files),
                    shipsElectron=False, shipsBrowser=False, browser="system Microsoft Edge", userScope=True)
    (output / "build-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest))


if __name__ == "__main__": main()

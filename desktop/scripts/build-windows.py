"""Build a per-user NSIS installer: embedded CPython, no Electron or browser."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

import payload_manifest

DESKTOP = Path(__file__).resolve().parents[1]
ROOT = DESKTOP.parent
PYTHON_VERSION = "3.13.9"
PYTHON_SHA256 = "91d828c2da3a029b41699e918674a0cb379c02cf20dab9c501306885f837402a"
LOCK = DESKTOP / "installer/requirements-windows.lock"
GUA_DROP = {"eval", "sandbox/apps", "sandbox/daemon.py", "sandbox/local.py", "mcp_server.py", "cli.py",
            "env/android.py", "env/linux.py", "env/macos.py", "env/remote.py"}
LIB_DROP = (
    "playwright/driver/package/lib/vite/traceViewer",
    "playwright/driver/package/lib/vite/dashboard",
    "playwright/driver/package/lib/vite/recorder",
    "playwright/driver/package/lib/vite/htmlReport",
    "playwright/driver/package/types", "playwright/driver/package/lib/tools/skills",
    "playwright/async_api", "comtypes/test",
    "uiautomation/bin/UIAutomationClient_VC140_X86.dll",
    "pyautogui/_pyautogui_osx.py", "pyautogui/_pyautogui_x11.py",
)


def validate_dependencies(site: Path):
    """A frozen target must be complete; never borrow a build-host package."""
    try:
        from packaging.requirements import Requirement
        from packaging.utils import canonicalize_name
    except ImportError:
        from pip._vendor.packaging.requirements import Requirement
        from pip._vendor.packaging.utils import canonicalize_name
    packages = list(importlib.metadata.distributions(path=[str(site)]))
    versions = {canonicalize_name(d.metadata["Name"]): d.version for d in packages}
    for distribution in packages:
        for raw in distribution.requires or []:
            requirement = Requirement(raw)
            if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
                continue
            version = versions.get(canonicalize_name(requirement.name))
            if version is None or not requirement.specifier.contains(version, prereleases=True):
                raise SystemExit(f"Incomplete frozen runtime: {distribution.metadata['Name']} requires {requirement}")


def prune_payload(site: Path):
    for rel in LIB_DROP:
        target = site / rel
        if target.is_dir(): shutil.rmtree(target)
        elif target.is_file(): target.unlink()
    for name in ("PIL", "psutil", "comtypes"):
        for target in list((site / name).rglob("tests")):
            if target.is_dir(): shutil.rmtree(target)
    for target in list((site / "PIL").glob("_avif*.pyd")): target.unlink()
    for target in list(site.rglob("__pycache__")):
        if target.exists(): shutil.rmtree(target)
    for target in list(site.rglob("*")):
        if target.is_file() and (target.name == "py.typed" or target.name.endswith((".pyi", ".pyc", ".d.ts", ".map"))):
            target.unlink()


def copy_engine(site: Path):
    def ignore(directory, names):
        rel = Path(directory).relative_to(ROOT / "gua")
        return {n for n in names if n == "__pycache__" or n.endswith((".pyc", ".pyo"))
                or (rel / n).as_posix() in GUA_DROP}
    shutil.copytree(ROOT / "gua", site / "gua", ignore=ignore)
    (site / "configs").mkdir()
    shutil.copy2(ROOT / "configs/default.yaml", site / "configs/default.yaml")
    for rel in ("tasks/web/form_submit.json", "tasks/web_assets/form.html"):
        (site / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, site / rel)


def portable_zip(payload: Path, target: Path):
    files, _ = payload_manifest.collect(payload)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for rel in files:
            path = payload.joinpath(*rel.split("\\"))
            archive.write(path, f"GUI-Agent/{path.relative_to(payload).as_posix()}")
        archive.writestr("GUI-Agent/portable.flag", "gui-agent-portable-v1\n")


def main():
    if sys.platform != "win32" or sys.version_info[:2] != (3, 13):
        raise SystemExit("Build on Windows x64 using Python 3.13")
    version = json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))["version"]
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
    for name in ("_sqlite3.pyd", "sqlite3.dll", "_msi.pyd", "winsound.pyd", "python.cat"):
        (runtime / name).unlink(missing_ok=True)
    site = runtime / "Lib/site-packages"
    subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-compile",
                    "--no-deps", "--target", str(site), "-r", str(LOCK)], check=True)
    validate_dependencies(site)
    prune_payload(site)
    copy_engine(site)
    (runtime / "python313._pth").write_text("python313.zip\n.\nLib/site-packages\nimport site\n", encoding="utf-8")
    shutil.copytree(DESKTOP / "dist-ui", payload / "ui")
    (payload / ".gui-agent-program").write_text("gui-agent-program-v1\n", encoding="utf-8")
    shutil.copy2(ROOT / "LICENSE", payload / "LICENSE.txt") if (ROOT / "LICENSE").exists() else None
    shutil.copy2(DESKTOP / "assets/icon.ico", payload / "app.ico")
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    subprocess.run([str(compiler), "/nologo", "/target:winexe", "/platform:x64", "/reference:System.Windows.Forms.dll",
                    "/win32icon:" + str(payload / "app.ico"), "/out:" + str(payload / "GUIAgent.exe"),
                    str(DESKTOP / "installer/launcher.cs")], check=True)
    owned_files, owned_dirs = payload_manifest.collect(payload)
    includes = DESKTOP / "build/installer"
    includes.mkdir(parents=True, exist_ok=True)
    (includes / "files_uninstall.nsh").write_text(payload_manifest.nsis_uninstall(owned_files, owned_dirs), encoding="utf-8")
    files = [p for p in payload.rglob("*") if p.is_file()]
    if any(site.glob("numpy*")): raise SystemExit("NumPy must not be shipped in the desktop runtime")
    forbidden = {"electron.exe", "chrome.exe", "chromium.exe", "msedge.exe"}
    if any(p.name.lower() in forbidden for p in files): raise SystemExit("A browser or Electron was bundled")
    if any(p.name.startswith(("chromium-", "chrome-headless-shell-")) for p in payload.rglob("*")):
        raise SystemExit("Bundled browser directory detected")
    makensis = shutil.which("makensis") or str(Path(os.environ["PROGRAMFILES(X86)"]) / "NSIS/makensis.exe")
    installer = output / f"GUI-Agent-{version}-x64-Setup.exe"
    subprocess.run([makensis, "/V2", "/DVERSION=" + version, "/DPAYLOAD=" + str(payload),
                    "/DOUTPUT=" + str(installer), "/DICON=" + str(payload / "app.ico"),
                    "/DMANIFEST_DIR=" + str(includes),
                    str(DESKTOP / "installer/installer.nsi")], check=True)
    unpacked = sum(p.stat().st_size for p in files)
    if installer.stat().st_size >= 50 * 1024 * 1024 or unpacked >= 180 * 1024 * 1024:
        raise SystemExit("Slim-build size gate exceeded: installer <50 MiB, payload <180 MiB")
    portable = output / f"GUI-Agent-{version}-x64-Portable.zip"
    portable_zip(payload, portable)
    manifest = dict(version=version, embeddedPython=PYTHON_VERSION, installerBytes=installer.stat().st_size,
                    unpackedBytes=unpacked, fileCount=len(files), portableBytes=portable.stat().st_size,
                    installerSHA256=hashlib.sha256(installer.read_bytes()).hexdigest(),
                    portableSHA256=hashlib.sha256(portable.read_bytes()).hexdigest(),
                    shipsElectron=False, shipsBrowser=False, shipsNumpy=False,
                    browser="system Microsoft Edge", userScope=True,
                    packages=[line.strip() for line in LOCK.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")],
                    largestFiles=sorted([(p.stat().st_size, str(p.relative_to(payload))) for p in files], reverse=True)[:8])
    (output / "build-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest))


if __name__ == "__main__": main()

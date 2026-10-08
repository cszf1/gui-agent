"""Desktop payload pruning preserves runtime features and safe deletion rules."""
import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "desktop/scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("slim_builder", SCRIPTS / "build-windows.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def test_pruned_engine_keeps_demo_and_shared_tool_process_helpers(tmp_path):
    builder.copy_engine(tmp_path)
    for rel in ("gua/desktop.py", "gua/scripted.py", "gua/sandbox/process.py", "gua/env/windows.py",
                "gua/env/web.py", "configs/default.yaml", "tasks/web/form_submit.json", "tasks/web_assets/form.html"):
        assert (tmp_path / rel).is_file(), rel
    for rel in builder.GUA_DROP:
        assert not (tmp_path / "gua" / rel).exists(), rel
    # A fresh interpreter must import only the copied engine, not pytest's
    # already-loaded source modules. Host packages supply the development deps.
    code = "import sys;sys.path.insert(0,sys.argv[1]);from gua.config import build_agent;from gua.desktop import desktop_config;from gua.scripted import ScriptedPolicy"
    subprocess.run([sys.executable, "-B", "-c", code, str(tmp_path)], cwd=tmp_path, check=True)


def test_prune_keeps_driver_and_licenses_removes_unused_files(tmp_path):
    for rel in ("playwright/driver/node.exe", "playwright/sync_api/__init__.py", "PIL/_imaging.pyd",
                "openai-3.26.0.dist-info/licenses/LICENSE", "playwright/async_api/__init__.py",
                "PIL/_avif.cp313-win_amd64.pyd", "comtypes/test/test.py", "playwright/package.d.ts"):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture")
    builder.prune_payload(tmp_path)
    for rel in ("playwright/driver/node.exe", "playwright/sync_api/__init__.py", "PIL/_imaging.pyd",
                "openai-3.26.0.dist-info/licenses/LICENSE"):
        assert (tmp_path / rel).is_file()
    assert not (tmp_path / "PIL/_avif.cp313-win_amd64.pyd").exists()
    assert not (tmp_path / "playwright/async_api").exists()


def test_manifest_and_portable_only_include_owned_files(tmp_path):
    stage = tmp_path / "payload"
    (stage / "runtime").mkdir(parents=True)
    (stage / "runtime/python.exe").write_text("fixture")
    files, dirs = builder.payload_manifest.collect(stage)
    uninstall = builder.payload_manifest.nsis_uninstall(files, dirs)
    assert 'Delete "$INSTDIR\\runtime\\python.exe"' in uninstall and 'RMDir /r' not in uninstall
    archive = tmp_path / "portable.zip"
    builder.portable_zip(stage, archive)
    with zipfile.ZipFile(archive) as z:
        assert sorted(z.namelist()) == ["GUI-Agent/portable.flag", "GUI-Agent/runtime/python.exe"]


def test_build_stage_rejects_symbolic_links(tmp_path):
    target = tmp_path / "real"
    target.write_text("fixture")
    try:
        (tmp_path / "link").symlink_to(target)
    except OSError:
        pytest.skip("symbolic links unavailable")
    with pytest.raises(ValueError):
        builder.payload_manifest.collect(tmp_path)


def test_frozen_payload_cannot_borrow_missing_dependencies_from_build_host(tmp_path):
    distribution = tmp_path / "fake_runtime-1.0.dist-info"
    distribution.mkdir()
    (distribution / "METADATA").write_text("Name: fake-runtime\nVersion: 1.0\nRequires-Dist: missing-runtime>=1\n")
    with pytest.raises(SystemExit, match="Incomplete frozen runtime"):
        builder.validate_dependencies(tmp_path)


def test_installer_runs_ownership_and_junction_guard_before_manifest_deletion():
    source = (ROOT / "desktop/installer/installer.nsi").read_text(encoding="utf-8")
    uninstall = source.split('Section "Uninstall"', 1)[1]
    assert uninstall.index('gua.app_cleanup --uninstall') < uninstall.index('files_uninstall.nsh')
    assert 'RMDir /r "$INSTDIR"' not in uninstall
    assert 'Section /o "桌面快捷方式"' in source
    assert 'WriteRegStr HKCU "Software\\cszf1\\GUI Agent"' not in source


def test_versions_and_runtime_lock_agree_and_numpy_is_not_shipped():
    import gua
    package = json.loads((ROOT / "desktop/package.json").read_text(encoding="utf-8"))
    assert package["version"] == gua.__version__
    lock = builder.LOCK.read_text(encoding="utf-8")
    assert "numpy" not in lock.lower() and "playwright==" in lock and "comtypes==" in lock

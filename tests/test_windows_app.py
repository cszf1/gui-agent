"""Desktop ownership, quotas, credentials and real loopback HTTP boundaries."""
import json
import os
import time
import threading
import urllib.error
import urllib.request
import uuid

import pytest
from PIL import Image

from gua.app_storage import AppStorage, RUN_MARKER, MAX_STATE_BYTES
from gua.windows_app import AppController, AppServer
from gua.desktop import StreamingLogger


def make_run(store, age=0, size=10):
    p = store.runs / str(uuid.uuid4()); p.mkdir()
    (p / ".gui-agent-run").write_text(RUN_MARKER)
    (p / "report.html").write_bytes(b"x" * size)
    t = time.time() - age * 86400
    os.utime(p, (t, t))
    return p


def test_default_desktop_logs_show_previews_without_persisting_screenshots(tmp_path):
    events = []
    log = StreamingLogger(tmp_path, str(uuid.uuid4()), lambda kind, **body: events.append((kind, body)))
    from gua.env.base import Observation
    obs = Observation(Image.new("RGB", (10, 10)), time.time(), (10, 10), focus_state="none")
    log.preview(obs)
    assert any(kind == "preview" for kind, _ in events)
    assert log.shot(1, "before", obs.screenshot) is None
    assert not (log.dir / "shots").exists()
    assert (log.dir / ".gui-agent-run").read_text().strip() == RUN_MARKER
    log.close(report=False)


def test_credentials_are_sealed_and_service_changes_require_a_new_key(tmp_path):
    encrypt = lambda v: bytes(b ^ 0xA5 for b in v)
    store = AppStorage(tmp_path, encrypt=encrypt, decrypt=encrypt)
    settings = {**store.settings, "apiKey": "private-api-key", "model": "vision"}
    assert store.save_settings(settings)["keyPersisted"]
    assert "private-api-key" not in store.file.read_text()
    restored = AppStorage(tmp_path, encrypt=encrypt, decrypt=encrypt)
    assert restored.key == "private-api-key"
    assert "apiKey" not in restored.public_settings()
    restored.save_settings({**restored.settings, "baseUrl": "https://another.example/v1", "apiKey": ""})
    assert restored.key == ""


def test_unavailable_secure_storage_never_falls_back_to_plaintext(tmp_path):
    def unavailable(_): raise RuntimeError("no secure storage")
    store = AppStorage(tmp_path, encrypt=unavailable)
    result = store.save_settings({**store.settings, "apiKey": "memory-key"})
    assert result["hasApiKey"] and not result["keyPersisted"]
    assert "memory-key" not in store.file.read_text()


def test_clean_history_retains_configuration_and_does_not_delete_user_files(tmp_path):
    store = AppStorage(tmp_path / "data")
    store.save_settings({**store.settings, "model": "my-model", "apiKey": "memory-key"})
    store.new_session()
    report = make_run(store)
    document = tmp_path / "user-document.txt"; document.write_text("keep")
    foreign = store.runs / "user-document.txt"; foreign.write_text("keep")
    store.clear_history()
    assert not report.exists() and document.read_text() == foreign.read_text() == "keep"
    assert store.key == "memory-key" and store.settings["model"] == "my-model"
    assert len(store.sessions) == 1 and not store.sessions[0]["runs"]


def test_run_retention_enforces_age_count_bytes_and_protects_active_work(tmp_path):
    store = AppStorage(tmp_path)
    expired = make_run(store, age=31)
    active = make_run(store, age=32)
    first = make_run(store, age=1, size=30)
    second = make_run(store, age=2, size=30)
    store.prune_runs(active.name, count=2, max_bytes=100)
    assert active.exists() and first.exists()
    assert not expired.exists() and not second.exists()
    store.prune_runs(max_bytes=10)
    assert not first.exists() and not active.exists()


def test_uuid_named_foreign_directory_is_not_assumed_to_be_a_run(tmp_path):
    store = AppStorage(tmp_path)
    foreign = store.runs / str(uuid.uuid4()); foreign.mkdir()
    (foreign / "document").write_text("keep")
    store.clear_history()
    assert (foreign / "document").read_text() == "keep"


@pytest.mark.skipif(os.name == "nt", reason="real Windows junctions are verified by installer smoke")
def test_cleanup_never_follows_directory_links(tmp_path):
    store = AppStorage(tmp_path / "data")
    external = tmp_path / "documents"; external.mkdir(); (external / "keep").write_text("keep")
    (store.cache / "link").symlink_to(external, target_is_directory=True)
    store.clean_transient()
    assert not (store.cache / "link").exists()
    assert (external / "keep").read_text() == "keep"
    root_link = tmp_path / "fake-data"; root_link.symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError): AppStorage(root_link)


def test_history_is_limited_globally_and_storage_file_has_a_size_ceiling(tmp_path):
    store = AppStorage(tmp_path)
    session = store.new_session()
    now = int(time.time()*1000)
    session["runs"] = [dict(id=str(uuid.uuid4()), createdAt=now+i, status="done", task="task", events=[]) for i in range(70)]
    session["runs"][-1]["events"] = [dict(type="log", record={"text": "x" * (MAX_STATE_BYTES+100)})]
    store.persist()
    assert store.file.stat().st_size <= MAX_STATE_BYTES
    assert len(session["runs"]) <= 50


@pytest.fixture
def app_http(tmp_path):
    assets = tmp_path / "ui"; assets.mkdir(); (assets / "index.html").write_text("<!doctype html>app")
    controller = AppController(AppStorage(tmp_path / "data"), headless=True)
    server = AppServer(controller, assets, token="test-control-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def call(path, body=None, *, token=server.token, origin=None, host=None, site=None, method="POST"):
        headers = {"Content-Type": "application/json", "X-Gua-Control": token}
        if origin: headers["Origin"] = origin
        if host: headers["Host"] = host
        if site: headers["Sec-Fetch-Site"] = site
        req = urllib.request.Request(server.origin + path, data=json.dumps(body or {}).encode() if method == "POST" else None,
                                     headers=headers, method=method)
        try:
            with opener.open(req, timeout=5) as r: return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e: return e.code, e.read(), e.headers
    call.controller, call.server = controller, server
    yield call
    server.shutdown(); server.server_close(); controller.close(); thread.join(timeout=5)


def test_loopback_control_rejects_missing_tokens_cross_site_requests_and_rebinding(app_http):
    assert app_http("/api/load", token="")[0] == 403
    assert app_http("/api/load", origin="https://attacker.example")[0] == 403
    assert app_http("/api/load", host="attacker.example")[0] == 403
    assert app_http("/api/load", origin=app_http.server.origin)[0] == 200
    assert app_http("/api/load", site="cross-site")[0] == 403
    assert app_http("/api/load", site="same-site")[0] == 403
    assert app_http("/api/load", site="same-origin")[0] == 200


def test_static_ui_is_csp_protected_and_paths_cannot_escape_the_bundle(app_http):
    status, _, headers = app_http("/", method="GET")
    assert status == 200 and "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store"
    assert app_http("/%2e%2e/state.json", method="GET")[0] == 404
    assert app_http("/api/load", method="GET")[0] == 405


def test_clear_requests_are_blocked_during_a_task_and_preserve_credentials_when_idle(app_http):
    controller = app_http.controller
    controller.storage.save_settings({**controller.storage.settings, "apiKey": "memory-key", "model": "vision"})
    controller.active = dict(id="busy")
    assert app_http("/api/clear-history")[0] == 400
    controller.active = None
    status, body, _ = app_http("/api/clear-history")
    assert status == 200 and json.loads(body)["value"]["settings"]["hasApiKey"]
    assert controller.storage.key == "memory-key"


def test_windows_desktop_config_uses_system_edge(monkeypatch):
    import gua.desktop as desktop
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    cfg = desktop.desktop_config({}, demo=True)
    assert cfg["env"]["web"]["channel"] == "msedge"


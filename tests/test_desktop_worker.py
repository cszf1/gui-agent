"""Desktop controls, provider configuration and preview privacy boundaries."""
import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

from gua.actions import Action
from gua.desktop import Control, ControlledEnv, StreamingLogger, Worker, desktop_config, model_spec, model_credentials
from gua.env.base import Observation, UIElement
from gua.env.mock import MockButton, MockEnv
from gua.errors import UserAbort


SETTINGS = {"provider": "openai", "baseUrl": "https://provider.example/v1", "model": "my-vision-model", "target": "browser"}


def test_one_model_config_overrides_local_grounder():
    cfg = desktop_config(SETTINGS)
    assert cfg["models"]["planner"]["base_url"] == SETTINGS["baseUrl"]
    assert cfg["models"]["grounder"]["model"] == SETTINGS["model"]
    assert cfg["models"]["grounder"]["base_url"] == SETTINGS["baseUrl"]
    assert cfg["models"]["actor"] is None
    assert cfg["safety"]["mode"] == "confirm"


def test_anthropic_accepts_a_v1_api_root():
    spec = model_spec({**SETTINGS, "provider": "anthropic", "baseUrl": "https://gateway.example/v1/"})
    assert spec["base_url"] == "https://gateway.example"


@pytest.mark.parametrize("base", ["file:///tmp/key", "https://key:secret@example.com/v1", "https://example.com?key=x", "https://example.com#key", "example.com/v1"])
def test_reject_api_url_credentials_and_non_http(base):
    with pytest.raises(ValueError):
        model_spec({**SETTINGS, "baseUrl": base})


def test_desktop_does_not_silently_run_a_mock(monkeypatch):
    monkeypatch.setattr("gua.desktop.detect_platform", lambda: "mock")
    with pytest.raises(ValueError, match="桌面"):
        desktop_config({**SETTINGS, "target": "desktop"})


def test_controls_invalidate_an_action_decided_before_pause(tmp_path):
    clicked = []
    env = MockEnv(buttons=[MockButton("Save", (10, 10, 100, 70), on_click=lambda _: clicked.append(True))])
    control = Control(lambda *args, **kw: None)
    log = StreamingLogger(tmp_path, "run", lambda *args, **kw: None)
    wrapped = ControlledEnv(env, control, log)
    wrapped.observe()
    control.command({"command": "pause"})
    control.command({"command": "resume"})
    assert not wrapped.execute(Action("click", x=40, y=40)).ok
    assert clicked == []
    wrapped.observe()
    assert wrapped.execute(Action("click", x=40, y=40)).ok
    assert clicked == [True]
    log.close(report=False)


def test_stop_is_a_terminal_signal_even_while_paused():
    control = Control(lambda *args, **kw: None)
    control.command({"command": "pause"})
    control.command({"command": "stop"})
    with pytest.raises(UserAbort):
        control.checkpoint()


def test_confirmation_binds_to_request_id_and_requires_boolean_true():
    seen = threading.Event()
    events = []
    result = []
    def emit(event_type, **payload):
        events.append((event_type, payload))
        if event_type == "request":
            seen.set()
    control = Control(emit)
    thread = threading.Thread(target=lambda: result.append(control.request("confirm", "Send?")))
    thread.start()
    assert seen.wait(2)
    request_id = events[0][1]["requestId"]
    control.command({"command": "respond", "requestId": "stale", "approved": True})
    assert thread.is_alive()
    control.command({"command": "respond", "requestId": request_id, "approved": "true"})
    thread.join(2)
    assert result == [False]


def test_password_preview_is_blocked_before_entering_ui_pipe(tmp_path):
    events = []
    log = StreamingLogger(tmp_path, "run", lambda kind, **data: events.append((kind, data)), save_images=True)
    obs = Observation(Image.new("RGB", (100, 80)), 1, (100, 80), focus_state="known",
                      elements=[UIElement(1, "password", "textbox", (0, 0, 90, 30), focused=True, is_password=True)])
    log.preview(obs)
    log.preview(Observation(Image.new("RGB", (100, 80)), 2, (100, 80), focus_state="none"))
    assert [kind for kind, _ in events] == ["privacy"]
    assert list((log.dir / "shots").iterdir()) == []
    log.close(report=False)


def test_worker_credential_scrubber_does_not_change_preview_policy():
    output = io.StringIO()
    worker = Worker(output)
    worker.credentials.add("a-private-model-key", explicit=True)
    worker.emit("error", message="service echoed a-private-model-key")
    event = json.loads(output.getvalue())
    assert "a-private-model-key" not in event["message"]


def test_worker_entrypoint_reads_and_writes_chinese_under_windows_code_page(monkeypatch):
    import sys
    from gua.desktop import main

    message = {"command": "test", "runId": "中文请求", "settings": {"provider": "未知接口"}}
    source = io.TextIOWrapper(io.BytesIO((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")),
                              encoding="cp1252")
    raw = io.BytesIO()
    output = io.TextIOWrapper(raw, encoding="cp1252")
    diagnostics = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdin", source)
        patch.setattr(sys, "stdout", output)
        patch.setattr(sys, "stderr", diagnostics)
        main()
        events = [json.loads(line) for line in raw.getvalue().decode("utf-8").splitlines()]
    assert events[0]["type"] == "ready"
    assert events[1]["type"] == "error"
    assert events[1]["runId"] == "中文请求"
    assert events[1]["message"] == "请选择 OpenAI 兼容接口或 Anthropic Messages 接口"


@pytest.mark.parametrize("original", [None, "an-existing-value"])
def test_configured_key_does_not_remain_in_the_environment_of_launched_apps(monkeypatch, original):
    name = "GUI_AGENT_DESKTOP_API_KEY"
    if original is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, original)
    with pytest.raises(RuntimeError):
        with model_credentials("a-private-model-key"):
            assert os.environ[name] == "a-private-model-key"
            raise RuntimeError("client construction failed")
    assert os.environ.get(name) == original


@pytest.mark.parametrize("mode", ["allow", "unsafe", None])
def test_desktop_settings_cannot_disable_confirmations(mode):
    with pytest.raises(ValueError):
        desktop_config({**SETTINGS, "safetyMode": mode})


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_connection_test_uses_configured_endpoint_key_and_real_image_request(provider, monkeypatch):
    monkeypatch.delenv("GUI_AGENT_DESKTOP_API_KEY", raising=False)
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, dict(self.headers), payload))
            response = ({"choices": [{"message": {"content": "OK"}}], "usage": {}}
                        if provider == "openai" else {"content": [{"type": "text", "text": "OK"}], "usage": {}})
            raw = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output = io.StringIO()
    worker = Worker(output)
    fake_key = "desktop-owned-test-key"
    try:
        worker.test({**SETTINGS, "provider": provider, "apiKey": fake_key,
                     "baseUrl": f"http://127.0.0.1:{server.server_address[1]}/v1"})
        assert json.loads(output.getvalue())["type"] == "tested"
        assert fake_key not in output.getvalue()
        assert "GUI_AGENT_DESKTOP_API_KEY" not in os.environ
        assert len(requests) == 1
        path, headers, payload = requests[0]
        assert payload["model"] == SETTINGS["model"]
        if provider == "openai":
            assert path == "/v1/chat/completions"
            assert next(v for k, v in headers.items() if k.lower() == "authorization") == f"Bearer {fake_key}"
            assert any(part["type"] == "image_url" for part in payload["messages"][1]["content"])
        else:
            assert path == "/v1/messages"
            assert next(v for k, v in headers.items() if k.lower() == "x-api-key") == fake_key
            assert any(part["type"] == "image" for part in payload["messages"][0]["content"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

"""第四轮审查回归：跨源导航文档隔离与观察到的输入目标身份。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pw = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.web

try:
    with pw.sync_playwright() as p:
        p.chromium.launch().close()
except Exception:
    pytest.skip("chromium not installed (run: playwright install chromium)", allow_module_level=True)

from gua.actions import Action  # noqa: E402
from gua.env.web import WebEnv  # noqa: E402
from gua.safety import SafetyGuard  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, body="", headers=()):
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def handle_request(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.records.append((self.command, self.path, body))
        if self.path.startswith("/start-"):
            status = self.path.rsplit("-", 1)[1]
            self.send(200, '<script>localStorage.setItem("review_token", "SOURCE_ONLY_SENTINEL")</script>'
                      f'<form method="post" action="/redirect-{status}"><input name="x" value="1">'
                      '<button id="submit">Continue</button></form>')
        elif self.path.startswith("/redirect-"):
            status = int(self.path.rsplit("-", 1)[1])
            port = self.server.server_address[1]
            self.send(status, headers=[("Location", f"http://localhost:{port}/landing")])
        elif self.path == "/landing":
            self.send(200, '<!doctype html><body><script>window.FOREIGN_SCRIPT_RAN = true;'
                      'document.body.textContent = JSON.stringify({origin:location.origin,'
                      'token:localStorage.getItem("review_token")});</script>')
        elif self.path == "/focus":
            self.send(200, '<input id="a" type="password" autofocus><input id="b" type="password">')
        else:
            self.send(404)

    do_GET = do_POST = handle_request


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.records = []
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join()


@pytest.fixture
def web():
    envs = []

    def make(**kw):
        env = WebEnv(viewport=(800, 600), **kw)
        env._ensure()
        envs.append(env)
        return env

    yield make
    for env in envs:
        env.close()


@pytest.mark.parametrize("status", [307, 308])
def test_cross_origin_post_navigation_is_blocked_before_target_request(server, web, status):
    server.records.clear()
    port = server.server_address[1]
    env = web(start_url=f"http://127.0.0.1:{port}/start-{status}",
              allowed_domains=["127.0.0.1", "localhost"])
    button = next(e for e in env.observe().elements if e.name == "Continue")
    result = env.execute(Action("click", x=button.center[0], y=button.center[1]))
    assert not result.ok and "blocked_by_safety" in result.error, result
    assert "document origin" in result.error
    assert sum(path == f"/redirect-{status}" for _, path, _ in server.records) == 1
    assert not any(path == "/landing" for _, path, _ in server.records)
    assert env.page.evaluate("window.FOREIGN_SCRIPT_RAN === true") is False


@pytest.mark.parametrize("use_allowlist,status", [(False, 307), (True, 303), (True, 302)])
def test_supported_cross_origin_navigation_keeps_target_origin(server, web, use_allowlist, status):
    port = server.server_address[1]
    env = web(start_url=f"http://127.0.0.1:{port}/start-{status}",
              allowed_domains=["127.0.0.1", "localhost"] if use_allowlist else [])
    env.page.locator("#submit").click()
    env.page.wait_for_function("window.FOREIGN_SCRIPT_RAN === true")
    assert json.loads(env.page.locator("body").inner_text()) == {
        "origin": f"http://localhost:{port}", "token": None}


@pytest.mark.parametrize("clear,submit", [(False, False), (True, False), (False, True)])
def test_focus_changed_during_confirmation_does_not_type_clear_or_submit(web, clear, submit):
    env = web()
    env.page.set_content('<input id="a" type="password"><form onsubmit="window.SUBMITTED=true;return false">'
                         '<input id="b" type="password" value="KEEP"><button>Continue</button></form>')
    env.page.locator("#a").focus()
    obs = env.observe()

    def confirm(action, reason):
        env.page.locator("#b").focus()
        return True

    action = Action("type", text="REVIEW_SECRET", clear=clear, submit=submit)
    assert SafetyGuard(confirm_fn=confirm).gate(action, obs)[0]
    result = env.execute(action)
    assert not result.ok and "changed since observation" in result.error
    assert env.page.locator("#b").input_value() == "KEEP"
    assert env.page.locator("#a").input_value() == ""
    assert env.page.evaluate("window.SUBMITTED === true") is False


def test_document_reload_cannot_reuse_observed_dom_number(server, web):
    port = server.server_address[1]
    env = web(start_url=f"http://127.0.0.1:{port}/focus")
    env.page.locator("#a").focus()
    obs = env.observe()
    old_id = next(e.attrs["dom_id"] for e in obs.elements if e.focused)
    env.page.reload()
    env.page.locator("#a").focus()
    info, state = env._probe_focus(env.page)
    assert state == "known" and info["dom_id"] == old_id
    result = env.execute(Action("type", text="REVIEW_SECRET"))
    assert not result.ok and "blocked_by_safety" in result.error
    assert env.page.locator("#a").input_value() == ""


def test_unchanged_observed_input_still_types(web):
    env = web()
    env.page.set_content('<input id="a" type="password">')
    env.page.locator("#a").focus()
    env.observe()
    assert env.execute(Action("type", text="REVIEW_SECRET")).ok
    assert env.page.locator("#a").input_value() == "REVIEW_SECRET"


def test_same_element_becoming_secure_requires_a_new_observation(web):
    env = web()
    env.page.set_content('<input id="a">')
    env.page.locator("#a").focus()
    env.observe()
    env.page.locator("#a").evaluate("el => el.type = 'password'")
    result = env.execute(Action("type", text="REVIEW_SECRET"))
    assert not result.ok and "security changed since observation" in result.error
    assert env.page.locator("#a").input_value() == ""

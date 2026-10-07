"""审查条目 9：Web 域名白名单必须在浏览器层强制执行，而不只是检查显式 navigate。

本地 HTTP 服务器同时用两个主机名访问：127.0.0.1（白名单内）和 localhost（白名单外）。
覆盖：普通链接点击、target=_blank 新标签页、服务器 302 重定向、JS location 跳转、window.open 弹窗。
"""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pw = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.web


def _chromium_ok() -> bool:
    try:
        with pw.sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


if not _chromium_ok():
    pytest.skip("chromium not installed", allow_module_level=True)

from gua.actions import Action  # noqa: E402
from gua.env.web import WebEnv  # noqa: E402

HITS: list[str] = []


def _page(port):
    evil = f"http://localhost:{port}"
    return f"""<!doctype html><title>Start</title>
<a id="l1" href="{evil}/evil-link" style="display:block;height:40px">plain link</a>
<a id="l2" href="{evil}/evil-blank" target="_blank" style="display:block;height:40px">new tab link</a>
<a id="l3" href="/redirect-evil" style="display:block;height:40px">redirect link</a>
<button id="b1" style="display:block;height:40px" onclick="location.href='{evil}/evil-js'">js nav</button>
<button id="b2" style="display:block;height:40px" onclick="window.open('{evil}/evil-popup')">js popup</button>
<a id="l4" href="/redirect-ok" style="display:block;height:40px">allowed redirect</a>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        HITS.append(f"{self.headers.get('Host', '').split(':')[0]}{self.path}")
        port = self.server.server_address[1]
        if self.path == "/redirect-evil":
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{port}/evil-redirect")
            self.end_headers()
            return
        if self.path == "/redirect-ok":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{port}/ok")
            self.end_headers()
            return
        body = (_page(port) if self.path.startswith("/start") else f"<title>{self.path}</title>{self.path}").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()


def _click(env, sel):
    box = env.page.locator(sel).bounding_box()
    return env.execute(Action("click", x=box["x"] + 5, y=box["y"] + 5))


@pytest.mark.parametrize("sel,path", [("#l1", "/evil-link"), ("#l2", "/evil-blank"), ("#l3", "/evil-redirect"),
                                      ("#b1", "/evil-js"), ("#b2", "/evil-popup")])
def test_i09_off_allowlist_navigation_is_blocked(server, sel, path):
    env = WebEnv(start_url=f"http://127.0.0.1:{server}/start", allowed_domains=["127.0.0.1"])
    try:
        env.observe()
        HITS.clear()
        r = _click(env, sel)
        env.page.wait_for_timeout(300)
        r2 = env.execute(Action("wait", seconds=0.2))   # 迟到的跳转/新标签页也要被发现
        assert not (r.ok and r2.ok), (r, r2)
        assert "blocked_by_safety" in (r.error + r2.error)
        assert f"localhost{path}" not in HITS, HITS     # 请求根本没有发到白名单外的主机
        for p in env._ctx.pages:
            assert "localhost" not in p.url, [pp.url for pp in env._ctx.pages]
        o = env.observe()
        assert "127.0.0.1" in o.url
        assert any(path in u for u in env.blocked_navigations)
    finally:
        env.close()


def test_i09_allowed_redirect_still_works(server):
    env = WebEnv(start_url=f"http://127.0.0.1:{server}/start", allowed_domains=["127.0.0.1"])
    try:
        env.observe()
        r = _click(env, "#l4")
        env.page.wait_for_load_state()
        assert r.ok, r
        assert env.observe().url.endswith("/ok")
    finally:
        env.close()

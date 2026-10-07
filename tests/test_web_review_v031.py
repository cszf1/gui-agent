"""v0.3.1 第二轮审查的浏览器回归测试（真实 Chromium）：r01 键盘激活、r04 嵌套密码框、r05 白名单 fail-closed。

本地 HTTP 服务器用两个主机名访问：127.0.0.1（白名单内 / 同源）和 localhost（白名单外 / 跨源）。
没有 Playwright 或 Chromium 时整文件自动跳过（Windows 审查环境即如此）。
"""
import threading
import time
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
    pytest.skip("chromium not installed (run: playwright install chromium)", allow_module_level=True)

from gua.actions import Action  # noqa: E402
from gua.env.web import WebEnv  # noqa: E402
from gua.safety import SafetyGuard  # noqa: E402

SECRET = "S3cr3t-Pa55!"
HITS: list[str] = []
LOCK = threading.Lock()


def _pages(port):
    evil = f"http://localhost:{port}"
    return {
        "/shadow": """<title>Shadow</title><div id=host></div><script>
const r = document.getElementById('host').attachShadow({mode:'open'});
r.innerHTML = '<label>User <input id=u></label><input id=pw type=password placeholder="Shadow password">' +
              '<button id=ok onclick="document.title=\\'SHADOW-OK\\'">Shadow OK</button>';
r.getElementById('pw').focus();</script>""",
        "/iframe-same": """<title>Same</title><p>outer</p><iframe id=f style="position:absolute;left:50px;top:80px;width:400px;height:200px;border:5px solid #000"
srcdoc="<input id=pw type=password placeholder='Frame password'><button onclick=&quot;document.body.dataset.ok=1;parent.document.title='FRAME-OK'&quot;>Frame OK</button>"></iframe>
<script>document.getElementById('f').addEventListener('load', () => {
  document.getElementById('f').contentDocument.getElementById('pw').focus(); });</script>""",
        "/iframe-cross": f"""<title>Cross</title><p>outer</p>
<iframe id=f src="{evil}/inner" style="position:absolute;left:30px;top:60px;width:400px;height:200px"></iframe>""",
        "/inner": "<title>inner</title><input id=pw type=password placeholder='Cross password'>",
        "/many": "<title>Many</title>" + "".join(f"<button>B{i}</button>" for i in range(40)) +
                 "<br><input id=pw type=password placeholder='Late password' autofocus>",
        "/danger": """<title>Danger</title><button id=d autofocus onclick="document.title='DELETED'">Delete account</button>""",
        "/danger-input": """<title>Danger2</title><form onsubmit="document.title='DELETED';return false">
<input id=n placeholder="Type your name" autofocus><input type=submit value="Delete account"></form>""",
        "/form": """<title>Form</title><form method=post action="/post"><input name=a value=1>
<button id=s type=submit>Go</button></form>""",
        "/start5": """<title>Start5</title><a id=slow href="/slow" style="display:block;height:40px">slow</a>
<a id=hop href="/hop1" style="display:block;height:40px">hop</a>""",
    }


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", headers=()):
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_POST(self):
        with LOCK:
            HITS.append(f"POST {self.headers.get('Host', '').split(':')[0]}{self.path}")
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        self._send(200, b"<title>posted</title>posted", [("Content-Type", "text/html")])

    def do_GET(self):
        host = self.headers.get("Host", "").split(":")[0]
        with LOCK:
            HITS.append(f"{host}{self.path}")
            nslow = sum(1 for h in HITS if h.endswith("/slow"))
        port = self.server.server_address[1]
        if self.path == "/slow":
            if nslow == 1:
                time.sleep(2.0)            # 第一次：比预取超时长
                return self._send(200, b"<title>slow</title>slow", [("Content-Type", "text/html")])
            return self._send(302, headers=[("Location", f"http://localhost:{port}/evil-after-timeout")])
        if self.path == "/hop1":
            return self._send(302, headers=[("Location", "/hop2")])
        if self.path == "/hop2":
            return self._send(302, headers=[("Location", f"http://localhost:{port}/evil-hop")])
        body = _pages(port).get(self.path, f"<title>{self.path}</title>{self.path}").encode()
        self._send(200, body, [("Content-Type", "text/html")])


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()


@pytest.fixture
def web():
    envs = []

    def make(url, **kw):
        while envs:                    # 同步 API 同一时间只能有一个 Playwright 实例
            envs.pop().close()
        e = WebEnv(start_url=url, viewport=(800, 600), **kw)
        e._ensure()
        envs.append(e)
        return e
    yield make
    for e in envs:
        e.close()


def _focused(obs):
    return next((e for e in obs.elements if e.focused), None)


def _assert_password_focus(obs):
    from gua.sensitive import safe_short
    f = _focused(obs)
    assert f is not None and f.is_password, [e.brief() for e in obs.elements]
    assert obs.focus_state == "known"
    a = Action("type", text=SECRET)
    d = SafetyGuard(mode="deny").assess(a, obs)
    assert d.verdict == "confirm" and SECRET not in safe_short(a, obs)


# =====================================================================  r04 嵌套密码框
def test_r04_shadow_dom_password_focus_detected(server, web):
    env = web(f"http://127.0.0.1:{server}/shadow")
    _assert_password_focus(env.observe())


def test_r04_same_origin_iframe_password_focus_detected(server, web):
    env = web(f"http://127.0.0.1:{server}/iframe-same")
    env.page.wait_for_timeout(200)
    _assert_password_focus(env.observe())


def test_r04_cross_origin_iframe_password_focus_detected(server, web):
    env = web(f"http://127.0.0.1:{server}/iframe-cross")
    env.page.frame_locator("#f").locator("#pw").click()
    obs = env.observe()
    _assert_password_focus(obs)
    f = _focused(obs)
    assert 30 <= f.rect[0] <= 60 and 60 <= f.rect[1] <= 100, f.rect      # 矩形已换算到主视口坐标


def test_r04_focus_probe_independent_of_candidate_limit(server, web):
    env = web(f"http://127.0.0.1:{server}/many", max_elements=5)
    _assert_password_focus(env.observe())


def test_r04_undeterminable_focus_is_unknown_and_conservative(server, web, monkeypatch):
    from gua.sensitive import safe_short
    env = web(f"http://127.0.0.1:{server}/iframe-cross")
    env.page.frame_locator("#f").locator("#pw").click()
    monkeypatch.setattr(env, "_probe_frame", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no access")))
    obs = env.observe()
    assert obs.focus_state == "unknown"
    a = Action("type", text="hello")
    assert SafetyGuard(mode="deny").assess(a, obs).verdict == "confirm"
    assert "hello" not in safe_short(a, obs)


def test_r04_nested_elements_collected_and_clickable(server, web):
    env = web(f"http://127.0.0.1:{server}/shadow")
    obs = env.observe()
    b = next(e for e in obs.elements if e.name == "Shadow OK")
    assert env.execute(Action("click", x=b.center[0], y=b.center[1])).ok
    assert env.page.title() == "SHADOW-OK"
    env2 = web(f"http://127.0.0.1:{server}/iframe-same")
    env2.page.wait_for_timeout(200)
    obs2 = env2.observe()
    b2 = next(e for e in obs2.elements if e.name == "Frame OK")
    assert b2.rect[0] >= 50 and b2.rect[1] >= 80
    env2.execute(Action("click", x=b2.center[0], y=b2.center[1]))
    assert env2.page.title() == "FRAME-OK"


# =====================================================================  r01 键盘激活危险按钮（真实页面）
@pytest.mark.parametrize("a", [Action("hotkey", keys=["enter"]), Action("hotkey", keys=["space"]),
                               Action("type", text="", submit=True)], ids=lambda a: a.short())
def test_r01_keyboard_cannot_activate_focused_delete_button(server, web, a):
    env = web(f"http://127.0.0.1:{server}/danger")
    obs = env.observe()
    g = SafetyGuard(mode="deny")
    ok, why = g.gate(a, obs)
    assert not ok and "delete" in why.lower()
    # 对照：不经闸门时，这个按键确实会激活按钮（证明测试有意义）
    env.execute(a)
    env.page.wait_for_timeout(100)
    assert env.page.title() == "DELETED"


def test_rx_enter_in_text_field_submitting_dangerous_form(server, web):
    """回车在普通输入框里 = 提交表单：有效激活目标是表单的提交按钮（input[type=submit] 的名字取自 value）。"""
    env = web(f"http://127.0.0.1:{server}/danger-input")
    obs = env.observe()
    f = _focused(obs)
    assert f is not None and f.attrs.get("form_submit") == "Delete account", [e.brief() for e in obs.elements]
    assert any(e.name == "Delete account" for e in obs.elements)
    g = SafetyGuard(mode="deny")
    assert not g.gate(Action("type", text="Alice", submit=True), obs)[0]
    assert not g.gate(Action("hotkey", keys=["enter"]), obs)[0]
    assert g.gate(Action("type", text="Alice"), obs)[0]           # 只打字、不提交：放行


# =====================================================================  r05 白名单 fail-closed
def test_r05_prefetch_timeout_fails_closed_then_offlist_redirect_blocked(server, web):
    HITS.clear()
    env = web(f"http://127.0.0.1:{server}/start5", allowed_domains=["127.0.0.1"], fetch_timeout=0.5)
    box = env.page.locator("#slow").bounding_box()
    r1 = env.execute(Action("click", x=box["x"] + 5, y=box["y"] + 5))
    env.page.wait_for_timeout(800)
    r1b = env.execute(Action("wait", seconds=0.1)) if r1.ok else r1
    assert not r1b.ok and "blocked_by_safety" in r1b.error, (r1, r1b)
    assert env.safety_failures
    time.sleep(2.2)                                   # 服务器第一次请求结束；不允许出现自动重发
    assert sum(1 for h in HITS if h.endswith("/slow")) == 1, HITS
    env.page.goto(f"http://127.0.0.1:{server}/start5")
    box = env.page.locator("#slow").bounding_box()
    r2 = env.execute(Action("click", x=box["x"] + 5, y=box["y"] + 5))
    env.page.wait_for_timeout(300)
    assert not any(h.startswith("localhost") for h in HITS), HITS
    assert "127.0.0.1" in env.page.url and "evil" not in env.page.url
    assert not r2.ok or env.blocked_navigations


def test_r05_every_redirect_hop_is_checked(server, web):
    HITS.clear()
    env = web(f"http://127.0.0.1:{server}/start5", allowed_domains=["127.0.0.1"])
    box = env.page.locator("#hop").bounding_box()
    r = env.execute(Action("click", x=box["x"] + 5, y=box["y"] + 5))
    env.page.wait_for_timeout(300)
    assert "127.0.0.1/hop2" in HITS, HITS
    assert not any(h.startswith("localhost") for h in HITS), HITS
    assert not r.ok and "blocked_by_safety" in r.error


def test_r05_post_navigation_sent_exactly_once(server, web):
    HITS.clear()
    env = web(f"http://127.0.0.1:{server}/form", allowed_domains=["127.0.0.1"])
    box = env.page.locator("#s").bounding_box()
    assert env.execute(Action("click", x=box["x"] + 5, y=box["y"] + 5)).ok
    env.page.wait_for_timeout(300)
    assert HITS.count("POST 127.0.0.1/post") == 1, HITS
    assert env.page.title() == "posted"

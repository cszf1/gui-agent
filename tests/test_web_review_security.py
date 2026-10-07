"""第三轮 Web 审查：真实 headless Chromium + 本地 HTTP 服务器的安全回归。

覆盖条目：
2  合成跳转页无脚本（meta refresh），恶意 Location fragment 无法执行脚本；原 3xx 的 CSP/sandbox/
   x-frame-options 不阻断合法跳转，目标页自身 CSP 保留；Set-Cookie（多个）保留；iframe 内跳转可完成。
3  真实 POST→303→GET→307→GET、307 保持 POST、跨 origin 删敏感请求头、白名单外主机收不到请求。
4  掩码 contenteditable 文本不外泄；autocomplete token 判为密码框；closed shadow host / 自定义节点焦点保守 unknown。
5  提交按钮与文本框共享稳定 DOM 身份（dom_id / form_submit_id），跨 frame 带 frame 前缀。
6  clear/type 绑定真实焦点元素：焦点中途移开 / 没有焦点 → 发送秘密前 blocked_by_safety。

服务器用两个主机名访问：127.0.0.1（默认白名单内）与 localhost（跨源 / 白名单外）。
没有 Playwright / Chromium 时整文件自动跳过。
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

RECORDS: list[dict] = []
LOCK = threading.Lock()


def _rec(method, path, headers, body):
    with LOCK:
        RECORDS.append({"method": method, "path": path,
                        "headers": {k.lower(): v for k, v in headers.items()}, "body": body})


def _pages(port):
    return {
        "/chain": "<title>Chain</title><p>chain page</p>",
        "/iframe-redir": '<title>FrameHost</title>'
                         '<iframe id="f" src="/redir-csp" style="width:400px;height:200px"></iframe>',
        "/sensitive": """<title>Sensitive</title>
<h1>Normal heading</h1><p id="normal">ordinary visible text</p>
<div role="group" id="wrap">
  <div id="masked" contenteditable="true" style="-webkit-text-security:disc">TOPSECRETXYZ</div>
</div>
<input id="ac" placeholder="AC field" autocomplete="section-blue current-password">
<div id="closed" tabindex="0" aria-label="Fancy label">Closed host label</div>
<x-field id="xf" tabindex="0"></x-field>
<div id="openhost"></div>
<script>
  document.getElementById('closed').attachShadow({mode:'closed'}).innerHTML = '<input type="password">';
  customElements.define('x-field', class extends HTMLElement { connectedCallback(){
    this.attachShadow({mode:'closed'}).innerHTML = '<input type="password" id="xpw">'; }});
  document.getElementById('openhost').attachShadow({mode:'open'}).innerHTML =
    '<input id="opw" type="password" placeholder="Open shadow pw">';
</script>""",
        "/form-id": """<title>FormId</title>
<form id="f" action="/noop"><input id="n" placeholder="Name"><button id="s" type="submit">Submit order</button></form>""",
        "/form-id-frame": '<title>FrameForm</title><iframe id="f" style="width:400px;height:200px" '
                          "srcdoc=\"<form><input placeholder='Frame Name'><button>Frame Submit</button></form>\"></iframe>",
        "/focus-steal": """<title>Steal</title><input id="a" placeholder="First"><input id="b" placeholder="Second">
<script>document.getElementById('a').addEventListener('input', () => { document.getElementById('b').focus(); });</script>""",
        "/focus-none": '<title>Nothing</title><p>no focus</p><input id="t" placeholder="Target">',
    }


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", headers=(), content_type="text/html; charset=utf-8"):
        self.send_response(code)
        if content_type:
            self.send_header("Content-Type", content_type)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _handle(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        path = self.path.split("?")[0]
        _rec(method, path, dict(self.headers), body)
        port = self.server.server_address[1]
        pages = _pages(port)
        if path in pages:
            self._send(200, pages[path].encode())
        elif path == "/redir-frag":
            self._send(302, headers=[("Location", "/ok#</script><script>globalThis.PROOF=1</script>")])
        elif path == "/redir-csp":
            self._send(302, headers=[
                ("Location", "/ok-csp"),
                ("Content-Security-Policy", "default-src 'self'; sandbox; frame-ancestors 'none'"),
                ("X-Frame-Options", "DENY")])
        elif path == "/redir-cookie":
            self._send(302, headers=[("Location", "/ok"), ("Set-Cookie", "c1=one; Path=/"),
                                     ("Set-Cookie", "c2=two; Path=/")])
        elif path == "/ok":
            self._send(200, b"<title>OK</title>ok")
        elif path == "/ok-csp":
            self._send(200, b"<title>OKC</title><script>window.TARGET_SCRIPT=1</script>ok",
                       headers=[("Content-Security-Policy", "script-src 'none'")])
        elif path == "/s303":
            self._send(303, headers=[("Location", "/s307")])
        elif path == "/s307":
            self._send(307, headers=[("Location", "/s-final")])
        elif path == "/s-final":
            self._send(200, b"FINAL", content_type="text/plain")
        elif path == "/p307":
            self._send(307, headers=[("Location", "/p-target")])
        elif path == "/p-target":
            self._send(200, b"KEPT", content_type="text/plain")
        elif path == "/cross":
            self._send(307, headers=[("Location", f"http://localhost:{port}/land")])
        elif path == "/cross-cookie":
            self._send(307, headers=[("Location", f"http://localhost:{port}/land-cookie")])
        elif path == "/land-cookie":
            self._send(200, b"LANDED_COOKIE", headers=[("Set-Cookie", "injected=1; Path=/")], content_type="text/plain")
        elif path == "/cross-off":
            self._send(307, headers=[("Location", f"http://localhost:{port}/evil")])
        elif path == "/land":
            self._send(200, b"LANDED", content_type="text/plain")
        elif path == "/evil":
            self._send(200, b"EVIL", content_type="text/plain")
        else:
            self._send(404, b"nf", content_type="text/plain")

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
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


def _wait(env, pred, timeout=6.0):
    """轮询等待：用 page.wait_for_timeout 推进 Playwright 事件循环（time.sleep 不会）。"""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return True
        except Exception:  # noqa: BLE001
            pass
        env.page.wait_for_timeout(50)
    return pred()


# =====================================================================  条目 2：无脚本跳转
def test_script_injected_via_location_fragment_cannot_execute(server, web):
    env = web(f"http://127.0.0.1:{server}/redir-frag", allowed_domains=["127.0.0.1"])
    assert _wait(env, lambda: "/ok" in env.page.url), env.page.url
    assert env.page.evaluate("() => globalThis.PROOF") is None


def test_redirect_completes_despite_3xx_csp_and_target_csp_preserved(server, web):
    env = web(f"http://127.0.0.1:{server}/redir-csp", allowed_domains=["127.0.0.1"])
    assert _wait(env, lambda: "/ok-csp" in env.page.url), env.page.url
    assert env.page.title() == "OKC"
    # 目标页自己的 CSP（script-src 'none'）必须保留 → 内联脚本不执行
    assert env.page.evaluate("() => typeof window.TARGET_SCRIPT") == "undefined"


def test_iframe_redirect_with_frame_ancestors_none_completes(server, web):
    env = web(f"http://127.0.0.1:{server}/iframe-redir", allowed_domains=["127.0.0.1"])
    assert _wait(env, lambda: any("/ok-csp" in f.url for f in env.page.frames)), \
        [f.url for f in env.page.frames]


def test_multiple_set_cookie_survive_redirect(server, web):
    env = web(f"http://127.0.0.1:{server}/redir-cookie", allowed_domains=["127.0.0.1"])
    assert _wait(env, lambda: env.page.url.rstrip("/").endswith("/ok")), env.page.url
    names = {c["name"] for c in env._ctx.cookies()}
    assert {"c1", "c2"} <= names, names


# =====================================================================  条目 3：真实逐跳请求
def test_real_post_303_get_307_get(server, web):
    RECORDS.clear()
    env = web(f"http://127.0.0.1:{server}/chain", allowed_domains=["127.0.0.1"], block_subresources=True)
    out = env.page.evaluate("() => fetch('/s303', {method:'POST', body:'secretbody'}).then(r => r.text())")
    assert out == "FINAL"
    seq = [(r["method"], r["path"]) for r in RECORDS if r["path"] in ("/s303", "/s307", "/s-final")]
    assert seq == [("POST", "/s303"), ("GET", "/s307"), ("GET", "/s-final")], seq
    assert next(r for r in RECORDS if r["path"] == "/s303")["body"] == b"secretbody"
    assert not any(r["body"] for r in RECORDS if r["path"] in ("/s307", "/s-final"))


def test_real_307_keeps_post_and_body(server, web):
    RECORDS.clear()
    env = web(f"http://127.0.0.1:{server}/chain", allowed_domains=["127.0.0.1"], block_subresources=True)
    out = env.page.evaluate("() => fetch('/p307', {method:'POST', body:'KEEP'}).then(r => r.text())")
    assert out == "KEPT"
    tgt = [r for r in RECORDS if r["path"] == "/p-target"]
    assert tgt and tgt[0]["method"] == "POST" and tgt[0]["body"] == b"KEEP", tgt


def test_cross_origin_redirect_strips_sensitive_headers(server, web):
    RECORDS.clear()
    env = web(f"http://127.0.0.1:{server}/chain",
              allowed_domains=["127.0.0.1", "localhost"], block_subresources=True)
    out = env.page.evaluate(
        "() => fetch('/cross', {method:'POST', headers:{'Authorization':'Bearer SECRET'}, body:'x'})"
        ".then(r => r.text())")
    assert out == "LANDED"
    land = [r for r in RECORDS if r["path"] == "/land"]
    assert land and land[0]["method"] == "POST" and land[0]["body"] == b"x", land
    assert "authorization" not in land[0]["headers"], land[0]["headers"]


def test_offlist_redirect_target_receives_no_request(server, web):
    RECORDS.clear()
    env = web(f"http://127.0.0.1:{server}/chain", allowed_domains=["127.0.0.1"], block_subresources=True)
    with pytest.raises(Exception):
        env.page.evaluate("() => fetch('/cross-off', {method:'POST', body:'y'}).then(r => r.text())")
    env.page.wait_for_timeout(300)
    assert not any(r["path"] == "/evil" for r in RECORDS), RECORDS


# =====================================================================  条目 4：敏感输入
def test_masked_contenteditable_text_not_leaked_but_normal_text_kept(server, web):
    env = web(f"http://127.0.0.1:{server}/sensitive", allowed_domains=["127.0.0.1"])
    # 证明测试有意义：真实文本确实在 textContent 里
    assert env.page.evaluate("() => document.body.textContent.includes('TOPSECRETXYZ')") is True
    obs = env.observe()
    assert "TOPSECRETXYZ" not in obs.text
    assert "TOPSECRETXYZ" not in obs.all_text()
    assert all("TOPSECRETXYZ" not in e.name for e in obs.elements)
    assert "ordinary visible text" in obs.text          # 普通文本功能保留


def test_autocomplete_token_focus_is_secure(server, web):
    env = web(f"http://127.0.0.1:{server}/sensitive", allowed_domains=["127.0.0.1"])
    box = env.page.locator("#ac").bounding_box()
    e = env.observe().element_at(box["x"] + 5, box["y"] + 5)
    assert e is not None and e.is_password, e
    env.page.evaluate("() => document.getElementById('ac').focus()")
    obs = env.observe()
    assert obs.focus_state == "known"
    assert next(x for x in obs.elements if x.focused).is_password


def test_closed_shadow_and_custom_element_focus_is_unknown(server, web):
    from gua.sensitive import safe_short
    env = web(f"http://127.0.0.1:{server}/sensitive", allowed_domains=["127.0.0.1"])
    env.page.evaluate("() => document.getElementById('closed').focus()")
    obs = env.observe()
    assert obs.focus_state == "unknown"
    assert not any(e.focused for e in obs.elements)
    a = Action("type", text="s3cret-value")
    assert SafetyGuard(mode="deny").assess(a, obs).verdict == "confirm"
    assert "s3cret-value" not in safe_short(a, obs)
    env.page.evaluate("() => document.getElementById('xf').focus()")
    assert env.observe().focus_state == "unknown"
    # 对照：open shadow root 里的原生输入框仍是已知焦点
    env.page.evaluate("() => document.getElementById('openhost').shadowRoot.getElementById('opw').focus()")
    obs3 = env.observe()
    assert obs3.focus_state == "known"
    assert next(e for e in obs3.elements if e.focused).is_password


# =====================================================================  条目 5：稳定 DOM 身份
def test_form_submit_identity_shared_and_stable(server, web):
    env = web(f"http://127.0.0.1:{server}/form-id", allowed_domains=["127.0.0.1"])
    obs = env.observe()
    inp = next(e for e in obs.elements if e.name == "Name")
    btn = next(e for e in obs.elements if e.name == "Submit order")
    assert inp.attrs.get("form_submit") == "Submit order"
    assert inp.attrs.get("form_submit_id") and inp.attrs["form_submit_id"] == btn.attrs.get("dom_id")
    env.page.evaluate("() => document.getElementById('n').focus()")
    obs2 = env.observe()
    focused = next(e for e in obs2.elements if e.focused)
    assert focused.attrs.get("form_submit_id") == btn.attrs["dom_id"]
    obs3 = env.observe()
    assert next(e for e in obs3.elements if e.name == "Submit order").attrs["dom_id"] == btn.attrs["dom_id"]
    # 点按钮与在输入框回车，安全闸门看到同一个提交目标名与身份
    g = SafetyGuard(mode="deny")
    click_name = g.activation_target(Action("click", x=btn.center[0], y=btn.center[1]), obs)[1]
    enter_name = g.activation_target(Action("type", text="Alice", submit=True), obs2)[1]
    assert click_name == enter_name == "Submit order"


def test_cross_frame_form_identity_has_frame_prefix(server, web):
    env = web(f"http://127.0.0.1:{server}/form-id-frame", allowed_domains=["127.0.0.1"])
    env.page.wait_for_timeout(300)
    obs = env.observe()
    inp = next(e for e in obs.elements if e.name == "Frame Name")
    btn = next(e for e in obs.elements if e.name == "Frame Submit")
    assert inp.attrs.get("frame") == "1"
    assert inp.attrs["form_submit_id"] == btn.attrs["dom_id"]
    assert inp.attrs["form_submit_id"].startswith("1-")


# =====================================================================  条目 6：输入绑定焦点
def test_typing_stops_when_focus_moves_midway(server, web):
    env = web(f"http://127.0.0.1:{server}/focus-steal", allowed_domains=["127.0.0.1"])
    env.page.evaluate("() => document.getElementById('a').focus()")
    r = env.execute(Action("type", text="ABCDEFGHIJKLMNOP"))
    assert not r.ok and "blocked_by_safety" in r.error, r
    assert env.page.evaluate("() => document.getElementById('b').value") == ""


def test_cross_origin_redirect_does_not_inject_cookie_to_source_origin(server, web):
    env = web(f"http://127.0.0.1:{server}/chain",
              allowed_domains=["127.0.0.1", "localhost"], block_subresources=True)
    out = env.page.evaluate(
        "() => fetch('/cross-cookie', {method:'POST', body:'x'}).then(r => r.text())")
    assert out == "LANDED_COOKIE"
    cookies = env._ctx.cookies()
    c_127 = [c for c in cookies if "127.0.0.1" in c.get("domain", "") and c.get("name") == "injected"]
    assert not c_127, f"Cookie must not be planted on source origin: {c_127}"


def test_typing_without_focus_is_blocked_then_ok_after_focus(server, web):
    env = web(f"http://127.0.0.1:{server}/focus-none", allowed_domains=["127.0.0.1"])
    r = env.execute(Action("type", text="hello"))
    assert not r.ok and "blocked_by_safety" in r.error, r
    env.page.evaluate("() => document.getElementById('t').focus()")
    assert env.execute(Action("type", text="hello")).ok
    assert env.page.evaluate("() => document.getElementById('t').value") == "hello"

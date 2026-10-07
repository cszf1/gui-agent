"""第三轮 Web 审查：URL 严格规范化 + 逐跳重定向状态机（离线，无需 Playwright）。

覆盖条目 1（检查与真实请求共用规范化 URL；反斜杠 / 控制空白 / userinfo / 百分号 authority /
数字·十六进制 IP 别名一律拒绝；Location 只允许 http/https）与条目 3（手动逐跳 fetch 维护
current_url / current_method / current_body / current_headers；303/301/302 的 POST 转 GET 清空 body；
307/308 保留当前状态；跨 origin 删敏感头；异常一律 abort，绝不重发）。

重定向用假 route 驱动 WebEnv._route，记录每跳真正发出的 fetch 参数。
"""
from __future__ import annotations

import types

import pytest

from gua.actions import Action, ActionParseError
from gua.env.web import WebEnv
from gua.urlpolicy import UrlRejected, domain_allowed, host_of, normalize

BASE = "http://a.test"


# =====================================================================  条目 1：URL 规范化
@pytest.mark.parametrize("raw,expected", [
    ("https://Example.COM/Path?Q=1", "https://example.com/Path?Q=1"),
    ("HTTP://127.0.0.1:8000/a", "http://127.0.0.1:8000/a"),
    ("http://example.com", "http://example.com/"),
    ("http://[::1]:90/x", "http://[::1]:90/x"),
    ("http://[2001:DB8::1]/p", "http://[2001:db8::1]/p"),
    ("example.com/x", "https://example.com/x"),
])
def test_normalize_accepts_unambiguous_network_urls(raw, expected):
    assert normalize(raw) == expected


@pytest.mark.parametrize("raw", [
    "http://exa mple.com/",              # 空白
    "http://exa\tmple.com/",             # 控制字符
    "http://exa\x00mple.com/",           # NUL
    "http:\\evil.test/x",                # 反斜杠（浏览器会当 /，不能假装一致）
    "http://127.0.0.1\\@evil.test/",     # 反斜杠 + userinfo
    "http://user:pass@evil.test/",       # userinfo
    "http://a%2eb.test/",                # 百分号 authority
    "http://2130706433/",                # 十进制的 127.0.0.1
    "http://127.1/",                     # 少段数字 IP
    "http://0x7f.1/",                    # 十六进制 IP
    "http://0177.0.0.1/",                # 八进制 / 前导零
    "http://exa_mple.test/",             # 非法 label
    "http://-bad.test/",
    "http://a..b.test/",
    "http://[::1",                       # 不闭合的 IPv6
    "javascript:alert(1)",               # 非导航 scheme
])
def test_normalize_rejects_ambiguous_or_non_network(raw):
    with pytest.raises(UrlRejected):
        normalize(raw, allow_local=False) if raw.startswith("javascript") else normalize(raw)


def test_redirect_target_only_allows_http_https():
    with pytest.raises(UrlRejected):
        normalize("javascript:alert(1)", allow_local=False)
    with pytest.raises(UrlRejected):
        normalize("file:///etc/passwd", allow_local=False)
    assert normalize("http://a.test/x", allow_local=False) == "http://a.test/x"


def test_allowlist_matches_subdomains_and_rejects_aliases():
    assert domain_allowed("https://a.example.com/x", ["example.com"])
    assert domain_allowed("https://example.com/x", ["example.com"])
    assert not domain_allowed("https://evil.com/x", ["example.com"])
    assert not domain_allowed("http://2130706433/", ["127.0.0.1"])   # 别名不匹配 = 拒绝（fail-closed）
    assert not domain_allowed("javascript:alert(1)", ["example.com"])
    assert domain_allowed("file:///c:/task.html", ["example.com"])   # 本地任务语义保留
    assert domain_allowed("about:blank", ["example.com"])
    assert domain_allowed("http://anything/", [])                    # 白名单为空 = 不限制
    assert host_of("http://[::1]:90/") == "::1"
    assert host_of("https://a.b.example.com/x") == "a.b.example.com"
    assert host_of("http:\\evil.test/x") == ""


def test_safety_guard_uses_the_same_url_policy():
    from gua.safety import SafetyGuard
    g = SafetyGuard(allowed_domains=["example.com"])
    assert g.assess(Action("navigate", url="https://a.example.com/x")).verdict == "allow"
    assert g.assess(Action("navigate", url="http://evil.com/")).verdict == "deny"
    assert g.assess(Action("navigate", url="http://2130706433/")).verdict == "deny"
    assert g.assess(Action("navigate", url="javascript:alert(1)")).verdict == "deny"


# =====================================================================  假 route
class Resp:
    def __init__(self, status, headers=None):
        self.status = status
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}


class Req:
    def __init__(self, url, method="GET", headers=None, body=None, nav=False):
        self.url, self.method = url, method
        self._h = dict(headers or {})
        self.post_data_buffer = body
        self.frame = types.SimpleNamespace(page=None)
        self._nav = nav

    @property
    def headers(self):
        return dict(self._h)

    def is_navigation_request(self):
        return self._nav


class Route:
    def __init__(self, req, responses):
        self.request, self.responses, self.calls = req, responses, []

    def fetch(self, **kw):
        self.calls.append(("fetch", dict(kw)))
        url = kw.get("url", self.request.url)
        if url not in self.responses:
            raise AssertionError(f"unexpected fetch: {url}")
        r = self.responses[url]
        if isinstance(r, Exception):
            raise r
        return r

    def abort(self, *a):
        self.calls.append(("abort", a))

    def continue_(self, *a, **k):
        self.calls.append(("continue", ()))

    def fulfill(self, **k):
        self.calls.append(("fulfill", k))


def _fetches(route):
    return [kw for kind, kw in route.calls if kind == "fetch"]


# =====================================================================  条目 3：逐跳状态机
def test_post_303_get_307_get_keeps_current_state():
    req = Req(f"{BASE}/a", method="POST",
              headers={"content-type": "application/x-www-form-urlencoded", "content-length": "5",
                       "authorization": "Bearer tok", "accept": "*/*"},
              body=b"hello", nav=False)
    route = Route(req, {
        f"{BASE}/a": Resp(303, {"location": f"{BASE}/b"}),
        f"{BASE}/b": Resp(307, {"location": f"{BASE}/c"}),
        f"{BASE}/c": Resp(200, {"content-type": "text/plain"}),
    })
    env = WebEnv(allowed_domains=["a.test"], block_subresources=True)
    env._route(route)
    f = _fetches(route)
    assert len(f) == 3, route.calls
    assert "url" not in f[0]                                       # 第一跳用原始请求
    # 303 之后 POST → GET，body 与 body 相关头都被清空；同源保留 authorization
    assert f[1]["url"] == f"{BASE}/b" and f[1]["method"] == "GET" and f[1]["post_data"] == ""
    assert "content-type" not in f[1]["headers"] and "content-length" not in f[1]["headers"]
    assert f[1]["headers"].get("authorization") == "Bearer tok"
    # 再遇到 307：当前状态是 GET，绝不能恢复原来的 POST / body
    assert f[2]["url"] == f"{BASE}/c" and f[2]["method"] == "GET" and f[2]["post_data"] == ""
    assert route.calls[-1][0] == "fulfill" and "response" in route.calls[-1][1]


def test_307_preserves_post_and_body():
    req = Req(f"{BASE}/p", method="POST", headers={"content-type": "text/plain"}, body=b"payload", nav=False)
    route = Route(req, {
        f"{BASE}/p": Resp(307, {"location": f"{BASE}/q"}),
        f"{BASE}/q": Resp(200),
    })
    env = WebEnv(allowed_domains=["a.test"], block_subresources=True)
    env._route(route)
    f = _fetches(route)
    assert f[1]["url"] == f"{BASE}/q" and f[1]["method"] == "POST" and f[1]["post_data"] == b"payload"
    assert f[1]["headers"].get("content-type") == "text/plain"


@pytest.mark.parametrize("status", [301, 302])
def test_301_302_post_becomes_get_without_body(status):
    req = Req(f"{BASE}/a", method="POST", headers={"content-type": "text/plain", "content-length": "3"},
              body=b"abc", nav=False)
    route = Route(req, {f"{BASE}/a": Resp(status, {"location": f"{BASE}/b"}), f"{BASE}/b": Resp(200)})
    env = WebEnv(allowed_domains=["a.test"], block_subresources=True)
    env._route(route)
    f = _fetches(route)
    assert f[1]["method"] == "GET" and f[1]["post_data"] == ""
    assert "content-type" not in f[1]["headers"]


def test_303_head_stays_head():
    req = Req(f"{BASE}/h", method="HEAD", headers={"accept": "*/*"}, nav=False)
    route = Route(req, {f"{BASE}/h": Resp(303, {"location": f"{BASE}/h2"}), f"{BASE}/h2": Resp(200)})
    env = WebEnv(allowed_domains=["a.test"], block_subresources=True)
    env._route(route)
    f = _fetches(route)
    assert f[1]["method"] == "HEAD" and f[1]["post_data"] == ""


def test_cross_origin_redirect_drops_sensitive_headers():
    req = Req(f"{BASE}/cross", method="POST",
              headers={"authorization": "Bearer secret", "proxy-authorization": "p", "cookie": "sid=1",
                       "content-type": "text/plain", "accept": "*/*"},
              body=b"x", nav=False)
    route = Route(req, {
        f"{BASE}/cross": Resp(307, {"location": "http://b.test/land"}),
        "http://b.test/land": Resp(200),
    })
    env = WebEnv(allowed_domains=["a.test", "b.test"], block_subresources=True)
    env._route(route)
    f = _fetches(route)
    assert f[1]["url"] == "http://b.test/land" and f[1]["method"] == "POST" and f[1]["post_data"] == b"x"
    h = f[1]["headers"]
    assert not {"authorization", "proxy-authorization", "cookie"} & set(h)
    assert h.get("content-type") == "text/plain"          # 同方法保留 body 相关头


@pytest.mark.parametrize("loc", [
    "javascript:alert(1)",
    "http:\\b.test\\@evil",
    "http://2130706433/",
    "http://a%2eb.test/",
    "http://user:pass@a.test/",
])
def test_bad_location_is_rejected_not_followed(loc):
    req = Req(f"{BASE}/j", nav=False)
    route = Route(req, {f"{BASE}/j": Resp(302, {"location": loc})})
    env = WebEnv(allowed_domains=["a.test"], block_subresources=True)
    env._route(route)
    assert len(_fetches(route)) == 1
    assert route.calls[-1][0] == "abort"
    assert env.blocked_navigations


def test_fetch_exception_never_retried():
    req = Req(f"{BASE}/t", nav=True)
    route = Route(req, {f"{BASE}/t": TypeError("got an unexpected keyword argument 'timeout'")})
    env = WebEnv(allowed_domains=["a.test"])
    env._route(route)
    assert len(_fetches(route)) == 1                       # 绝不按异常类型重发
    assert route.calls[-1][0] == "abort"
    assert env.safety_failures and "TypeError" in env.safety_failures[-1]


# =====================================================================  条目 2：合成跳转页（nav）
def _fulfilled(route):
    kind, kw = route.calls[-1]
    assert kind == "fulfill", route.calls
    return kw


def test_navigation_redirect_is_script_free_meta_refresh():
    req = Req(f"{BASE}/a", nav=True)
    route = Route(req, {f"{BASE}/a": Resp(302, {
        "location": f"{BASE}/b",
        "content-security-policy": "default-src 'self'",
        "x-frame-options": "DENY",
        "refresh": "0; url=http://evil.test/",
        "content-length": "10",
        "content-encoding": "gzip",
        "set-cookie": "c1=1; Path=/",
    })})
    env = WebEnv(allowed_domains=["a.test"])
    env._route(route)
    kw = _fulfilled(route)
    body, headers = kw["body"], {k.lower(): v for k, v in kw["headers"].items()}
    assert kw["status"] == 200
    assert 'http-equiv="refresh"' in body and "<script" not in body.lower()
    assert f"url={BASE}/b" in body
    csp = headers["content-security-policy"]
    assert "script-src 'none'" in csp and "frame-ancestors *" in csp
    for dropped in ("x-frame-options", "refresh", "content-length", "content-encoding"):
        assert dropped not in headers, dropped
    assert headers["set-cookie"] == "c1=1; Path=/"          # Set-Cookie 保留（多个也保留）


def test_malicious_location_fragment_cannot_inject_script():
    payload = "</script><script>globalThis.PROOF=1</script>"
    req = Req(f"{BASE}/a", nav=True)
    route = Route(req, {f"{BASE}/a": Resp(302, {"location": f"{BASE}/ok#{payload}"})})
    env = WebEnv(allowed_domains=["a.test"])
    env._route(route)
    body = _fulfilled(route)["body"]
    assert payload not in body                              # 未编码的注入串绝不出现
    assert "&lt;/script&gt;" in body                        # 被 html.escape 编码，处于文本属性里
    assert body.count("<script") == 0


def test_navigation_post_303_uses_meta_refresh_to_get():
    req = Req(f"{BASE}/f", method="POST", headers={"content-type": "text/plain"}, body=b"x", nav=True)
    route = Route(req, {f"{BASE}/f": Resp(303, {"location": f"{BASE}/done"})})
    env = WebEnv(allowed_domains=["a.test"])
    env._route(route)
    kw = _fulfilled(route)
    assert 'http-equiv="refresh"' in kw["body"]
    assert len(_fetches(route)) == 1                        # 不再 fetch 重发（GET 由浏览器发起）


def test_navigation_post_307_is_followed_with_post():
    req = Req(f"{BASE}/p", method="POST", headers={"content-type": "text/plain"}, body=b"keep", nav=True)
    route = Route(req, {f"{BASE}/p": Resp(307, {"location": f"{BASE}/p2"}), f"{BASE}/p2": Resp(200)})
    env = WebEnv(allowed_domains=["a.test"])
    env._route(route)
    f = _fetches(route)
    assert f[1]["url"] == f"{BASE}/p2" and f[1]["method"] == "POST" and f[1]["post_data"] == b"keep"


@pytest.mark.parametrize("status", [307, 308])
@pytest.mark.parametrize("target", ["http://b.test/land", "http://a.test:8080/land", "https://a.test/land"])
def test_method_preserving_cross_origin_navigation_is_not_fetched(status, target):
    req = Req(f"{BASE}/p", method="POST", body=b"private body", nav=True)
    route = Route(req, {req.url: Resp(status, {"location": target})})
    env = WebEnv(allowed_domains=["a.test", "b.test"])
    env._route(route)
    assert len(_fetches(route)) == 1
    assert route.calls[-1][0] == "abort"
    assert not any(kind in {"fulfill", "continue"} for kind, _ in route.calls)
    assert env.blocked_navigations == [target]


def test_same_origin_post_hop_cannot_hide_later_cross_origin_navigation():
    req = Req(f"{BASE}/p", method="POST", body=b"private body", nav=True)
    route = Route(req, {
        req.url: Resp(307, {"location": f"{BASE}/q"}),
        f"{BASE}/q": Resp(308, {"location": "http://b.test/land"}),
    })
    env = WebEnv(allowed_domains=["a.test", "b.test"])
    env._route(route)
    assert len(_fetches(route)) == 2
    assert _fetches(route)[1]["post_data"] == b"private body"
    assert route.calls[-1][0] == "abort"
    assert env.blocked_navigations == ["http://b.test/land"]


# =====================================================================  条目 6：execute 先跑 validate
def test_web_execute_calls_action_validate(monkeypatch):
    def boom(self):
        raise ActionParseError("bad_value", "hotkey must be modifiers + exactly one key", "keys")

    monkeypatch.setattr(Action, "validate", boom)
    r = WebEnv().execute(Action("wait", seconds=0))
    assert not r.ok and "invalid_action" in r.error

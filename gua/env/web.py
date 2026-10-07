"""Web 环境（Playwright，同步 API）。

- 截图：page.screenshot（device_scale_factor=1，所以截图像素 == CSS 像素 == 鼠标坐标）
- 元素：注入 JS 抽取可交互 DOM 元素（browser-use 风格：给每个元素打 data-gua-id 编号），
  包括视口外元素（标记 offscreen，供“滚动到可见”恢复使用）和对话框（role=dialog / <dialog open> / aria-modal）
- 执行：鼠标/键盘走 page.mouse / page.keyboard（坐标化，保持与桌面平台一致的验证语义）；
  navigate / back 走 page.goto / go_back；域名白名单由 safety.SafetyGuard 和这里双重检查
- 多标签页：新开的页面会被当作“前台窗口”，用于复现“焦点被抢走”的时间失配

安装：pip install "gui-agent[web]" && playwright install chromium

v0.3（审查条目 9）：域名白名单在**浏览器层**强制执行，而不只检查显式 navigate：
1. context.route 拦截所有导航请求（任意 frame、新标签页、window.open、JS location 跳转）：目标主机不在白名单 → abort；
2. 白名单内的导航用 route.fetch(max_redirects=0) 先取响应，3xx 的 Location 指向白名单外 → abort（服务器重定向）；
3. 新页面（popup / target=_blank）若被拦或落在白名单外 → 关闭，前台切回任务页；
4. 每个动作之后做 URL 复核：主页面若停在白名单外或拦截错误页 → 回到最后一个合法 URL；
   动作返回 ExecResult(ok=False, "blocked_by_safety: ...")，安全拒绝对该动作是终止性的（不会被重试）。
被拦截的 URL 记录在 WebEnv.blocked_navigations。可选 block_subresources=True 连图片/脚本等子资源一起拦。
"""
from __future__ import annotations

import io
import json
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

from PIL import Image

from ..actions import Action
from ..keys import canonical_key
from ..urlpolicy import domain_allowed
from .a11y import finalize, web_raws
from .base import Env, ExecResult, Observation

# 公共 JS 片段：元素命名 / 密码判定 / 表单提交按钮（快照与焦点探测共用）
_JS_HELPERS = r"""
  const rootOf = (el) => el.getRootNode ? el.getRootNode() : document;
  const nameOf = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria;
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const r = rootOf(el); const n = (r.getElementById ? r.getElementById(lb) : null) || document.getElementById(lb);
              if (n) return n.innerText || n.textContent || ''; }
    if (el.labels && el.labels.length) return el.labels[0].innerText;
    if (el.tagName === 'INPUT' && ['submit', 'button', 'reset'].includes((el.type || '').toLowerCase()))
      return el.value || el.name || el.id || '';
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') return el.placeholder || el.name || el.id || '';
    if (el.tagName === 'SELECT') { const o = el.options[el.selectedIndex]; return (el.name || el.id || '') + (o ? ': ' + o.text : ''); }
    if (el.tagName === 'IMG') return el.alt || '';
    const t = (el.innerText || el.textContent || '').trim();
    if (el.matches('dialog,[role=dialog],[role=alertdialog]')) return (el.getAttribute('aria-label') || t.split('\n')[0] || 'dialog');
    return t || el.title || (el.tagName === 'INPUT' ? '' : (el.value || '')) || '';
  };
  const secureOf = (el) => {
    if ((el.getAttribute('type') || '').toLowerCase() === 'password') return true;
    try { const st = window.getComputedStyle(el); const ts = st.webkitTextSecurity || st.getPropertyValue('-webkit-text-security');
          if (ts && ts !== 'none') return true; } catch (e) {}
    return false;
  };
  const submitOf = (el) => {
    const f = el.form; if (!f) return '';
    const b = f.querySelector('button:not([type]),button[type=submit],input[type=submit],input[type=image]');
    return b ? nameOf(b).slice(0, 120) : (f.getAttribute('aria-label') || '');
  };
"""

# 元素快照（v0.3.1：穿透 open shadow root；iframe 由 Python 逐个 frame 调用本脚本并加上 frame 偏移，
# 同源 / 跨源 iframe 一视同仁，见 WebEnv._snapshot）
SNAPSHOT_JS = r"""
([maxN, fid]) => {
""" + _JS_HELPERS + r"""
  const SEL = 'a[href],button,input,select,textarea,summary,progress,[role],[onclick],[aria-busy="true"],[tabindex]:not([tabindex="-1"]),[contenteditable="true"],dialog[open],label';
  const vw = window.innerWidth, vh = window.innerHeight;
  const out = [];
  const texts = [];
  let gid = 0;
  const roots = [document];
  for (let i = 0; i < roots.length && i < 200; i++) {        // 广度优先遍历所有 open shadow root
    const r = roots[i];
    for (const el of r.querySelectorAll('*')) if (el.shadowRoot && el.shadowRoot.mode === 'open') roots.push(el.shadowRoot);
  }
  for (const root of roots) {
    if (root !== document) { for (const c of root.children) { const t = (c.innerText || '').trim(); if (t) texts.push(t); } }
    for (const el of root.querySelectorAll(SEL)) {
      if (out.length >= maxN) break;
      const st = window.getComputedStyle(el);
      if (st.visibility === 'hidden' || st.display === 'none' || parseFloat(st.opacity) === 0) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) continue;
      if (el.tagName === 'LABEL' && el.control) continue;
      let role = el.getAttribute('role') || '';
      if (el.tagName === 'DIALOG' || el.getAttribute('aria-modal') === 'true') role = role || 'dialog';
      if (el.tagName === 'PROGRESS') role = role || 'progressbar';
      let covered = false;
      const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
      if (cx >= 0 && cy >= 0 && cx < vw && cy < vh && role !== 'dialog') {
        const top = (root.elementFromPoint ? root.elementFromPoint(cx, cy) : document.elementFromPoint(cx, cy));
        covered = !!top && top !== el && !el.contains(top) && !top.contains(el);
      }
      el.setAttribute('data-gua-id', fid + '-' + String(gid));
      const secure = secureOf(el);
      let vnow = el.getAttribute('aria-valuenow'), vmax = el.getAttribute('aria-valuemax');
      if (el.tagName === 'PROGRESS' && el.hasAttribute('value')) { vnow = String(el.value); vmax = String(el.max); }
      out.push({gid: gid++, tag: el.tagName.toLowerCase(), role: role, type: el.getAttribute('type') || '',
        name: nameOf(el).slice(0, 120),
        value: (!secure && el.value !== undefined && el.tagName !== 'BUTTON' && el.tagName !== 'PROGRESS') ? String(el.value) : null,
        rect: [r.left, r.top, r.right, r.bottom], disabled: !!el.disabled, focused: false,
        checked: (el.type === 'checkbox' || el.type === 'radio') ? !!el.checked : null, covered: covered,
        href: el.getAttribute('href') || '', secure: secure, autocomplete: el.getAttribute('autocomplete') || '',
        form_submit: (el.form ? submitOf(el) : ''), busy: el.getAttribute('aria-busy') === 'true' ? 'true' : '',
        valuenow: vnow, valuemax: vmax});
    }
  }
  return {items: out, text: (document.body ? document.body.innerText : '').slice(0, 6000),
          shadow_text: texts.join('\n').slice(0, 2000),
          title: document.title, url: location.href, vw: vw, vh: vh};
}
"""

# 安全焦点探测（v0.3.1，第二轮条目 4）：沿 activeElement 穿透 open shadow root；落在 iframe 上时给它打标记，
# 由 Python 找到对应的子 frame 继续探测（跨源 frame 也可以，Playwright 有特权访问）。与候选元素数量上限无关。
FOCUS_JS = r"""
(token) => {
""" + _JS_HELPERS + r"""
  let a = document.activeElement;
  while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
  if (!a || a === document.body || a === document.documentElement) return {kind: 'none'};
  if (a.tagName === 'IFRAME' || a.tagName === 'FRAME') { a.setAttribute('data-gua-focus-frame', token); return {kind: 'frame'}; }
  const r = a.getBoundingClientRect();
  return {kind: 'element', tag: a.tagName.toLowerCase(), role: a.getAttribute('role') || '',
          type: a.getAttribute('type') || '', name: nameOf(a).slice(0, 120), secure: secureOf(a),
          autocomplete: a.getAttribute('autocomplete') || '', form_submit: submitOf(a),
          rect: [r.left, r.top, r.right, r.bottom], disabled: !!a.disabled, editable: !!a.isContentEditable};
}
"""

FRAME_OFFSET_JS = r"""(e) => { const cs = window.getComputedStyle(e);
  return [e.clientLeft + (parseFloat(cs.paddingLeft) || 0), e.clientTop + (parseFloat(cs.paddingTop) || 0)]; }"""

REDIRECT_HTML = """<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer">
<title>redirect</title><script>location.replace({target});</script>"""

_KEYMAP = {"ctrl": "Control", "control": "Control", "cmd": "Meta", "command": "Meta", "win": "Meta", "insert": "Insert",
           "meta": "Meta", "alt": "Alt", "option": "Alt", "shift": "Shift", "enter": "Enter",
           "return": "Enter", "esc": "Escape", "escape": "Escape", "tab": "Tab", "backspace": "Backspace",
           "delete": "Delete", "del": "Delete", "space": "Space", "up": "ArrowUp", "down": "ArrowDown",
           "left": "ArrowLeft", "right": "ArrowRight", "pageup": "PageUp", "pagedown": "PageDown",
           "home": "Home", "end": "End"}


def pw_key(k: str) -> str:
    k = k.strip()
    low = canonical_key(k)
    if low in _KEYMAP:
        return _KEYMAP[low]
    if len(k) == 1:
        return k.lower() if k.isalpha() else k
    if low.startswith("f") and low[1:].isdigit():
        return low.upper()
    return k


def to_url(u: str, base_dir: Optional[Path] = None) -> str:
    """相对路径 / 本地文件 → file:// URL；其余原样返回。"""
    if "://" in u or u.startswith("about:") or u.startswith("data:"):
        return u
    p = Path(u)
    if not p.is_absolute() and base_dir is not None:
        p = base_dir / p
    return p.resolve().as_uri()


class WebEnv(Env):
    platform = "web"
    scroll_unit_px = 100

    def __init__(self, start_url: str = "about:blank", headless: bool = True,
                 viewport: tuple[int, int] = (1280, 800), browser: str = "chromium",
                 allowed_domains: Optional[list[str]] = None, max_elements: int = 150,
                 slow_mo: int = 0, block_subresources: bool = False, fetch_timeout: float = 30.0,
                 max_redirect_hops: int = 20):
        self.start_url = start_url
        self.headless = headless
        self.viewport = viewport
        self.browser_name = browser
        self.allowed_domains = allowed_domains or []
        self.max_elements = max_elements
        self.slow_mo = slow_mo
        self._pw = self._browser = self._ctx = None
        self.block_subresources = block_subresources
        self.page = None          # 任务页面
        self.active = None        # 当前前台页面（可能被新标签页抢走）
        self.blocked_navigations: list[str] = []   # 被白名单拦下的 URL（全部历史）
        self._unreported: list[str] = []           # 尚未通过 ExecResult 报告给 agent 的拦截
        self._blocked_pages: list = []              # 导航被拦的新页面（稍后关闭）
        self._last_good_url: Optional[str] = None
        self.cursor: Optional[tuple[int, int]] = None
        self.fetch_timeout = fetch_timeout          # 白名单预取超时（秒）；超时 = 拦截（fail-closed），不会重发
        self.max_redirect_hops = max_redirect_hops
        self.safety_failures: list[str] = []         # 白名单检查本身失败（预取异常、处理器异常）→ 已 fail-closed 拦截
        self._hops = 0                               # 连续客户端重定向跳数（防重定向环）

    # ---------------------------------------------------------------- 生命周期
    def _ensure(self) -> None:
        if self._pw is not None:
            return
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        launcher = getattr(self._pw, self.browser_name)
        self._browser = launcher.launch(headless=self.headless, slow_mo=self.slow_mo)
        self._ctx = self._browser.new_context(viewport={"width": self.viewport[0], "height": self.viewport[1]},
                                              device_scale_factor=1)
        if self.allowed_domains:
            self._ctx.route("**/*", self._route)
        self._ctx.on("page", self._on_page)
        self.page = self.active = self._ctx.new_page()
        if self.start_url:
            self.page.goto(to_url(self.start_url))
            self._remember_good()

    def _on_page(self, p) -> None:
        self.active = p  # 新开页面抢到前台

    # ---------------------------------------------------------------- 白名单（浏览器层）
    def _record_block(self, url: str, why: str, page=None) -> None:
        self.blocked_navigations.append(url)
        self._unreported.append(f"{url} ({why})")
        if page is not None and page is not self.page:
            self._blocked_pages.append(page)

    def _safety_fail(self, url: str, why: str, page=None) -> None:
        self.safety_failures.append(f"{url}: {why}")
        self._record_block(url, f"safety check failed, request aborted: {why}", page)

    def _fetch(self, route, **kw):
        try:
            return route.fetch(max_redirects=0, timeout=self.fetch_timeout * 1000, **kw)
        except TypeError:           # 旧版 Playwright 没有 timeout 参数
            return route.fetch(max_redirects=0, **kw)

    def _route(self, route) -> None:
        """浏览器层白名单（v0.3.1：任何异常都 fail-closed——abort 并记录安全失败，绝不 continue_）。"""
        url = "?"
        try:
            url = route.request.url
            self._route_inner(route)
        except Exception as e:  # noqa: BLE001
            self._safety_fail(url, f"route handler error {type(e).__name__}: {str(e)[:120]}")
            try:
                route.abort("blockedbyclient")
            except Exception:  # noqa: BLE001
                pass

    def _route_inner(self, route) -> None:
        req = route.request
        url = req.url
        try:
            page = req.frame.page
        except Exception:
            page = None
        try:
            nav = req.is_navigation_request()
        except Exception:         # 判断不了是不是导航：按导航处理（保守）
            nav = True
        if not domain_allowed(url, self.allowed_domains):
            if nav or self.block_subresources:
                self._record_block(url, "navigation to host outside allowlist" if nav else "subresource", page)
                route.abort("blockedbyclient")
            else:
                route.continue_()
            return
        if not (nav or self.block_subresources) or urlparse(url).scheme not in {"http", "https"}:
            route.continue_()
            return
        # 先取响应、不跟随重定向（请求只发送这一次：之后用 fulfill 交给浏览器，不再 continue_）
        try:
            resp = self._fetch(route)
        except Exception as e:  # noqa: BLE001  — 超时 / 连接失败：不知道服务器是否已收到，绝不重发
            self._safety_fail(url, f"allowlist prefetch failed ({type(e).__name__}); not retried", page)
            route.abort("blockedbyclient")
            return
        hops = 0
        method = (getattr(req, "method", "GET") or "GET").upper()
        while 300 <= resp.status < 400:
            loc = resp.headers.get("location", "")
            target = urljoin(url, loc) if loc else ""
            if not target:
                break
            if not domain_allowed(target, self.allowed_domains):
                self._record_block(target, f"redirect from {url}", page)
                route.abort("blockedbyclient")
                return
            keep_method = resp.status in (307, 308) and method != "GET"
            if nav and not keep_method:
                # Playwright 不会拦截浏览器自己跟随的重定向（v0.3 多跳重定向因此可绕过）：改成客户端跳转，
                # 下一跳是一个新的导航请求，会再次进入本处理器逐跳检查；原响应头（含 Set-Cookie）保留。
                self._hops += 1
                if self._hops > self.max_redirect_hops:
                    self._safety_fail(target, f"more than {self.max_redirect_hops} redirect hops", page)
                    route.abort("blockedbyclient")
                    return
                headers = {k: v for k, v in resp.headers.items()
                           if k.lower() not in {"location", "content-length", "content-type", "content-encoding",
                                                "transfer-encoding"}}
                route.fulfill(status=200, headers=headers, content_type="text/html; charset=utf-8",
                              body=REDIRECT_HTML.format(target=json.dumps(target)))
                return
            # 子资源 / 307·308 非 GET：在这里逐跳跟随（每跳检查白名单），最后把最终响应交给浏览器
            hops += 1
            if hops > self.max_redirect_hops:
                self._safety_fail(target, f"more than {self.max_redirect_hops} redirect hops", page)
                route.abort("blockedbyclient")
                return
            try:
                kw = {"url": target}
                if keep_method:
                    kw.update(method=method, post_data=req.post_data_buffer)
                else:
                    kw.update(method="GET")
                resp = self._fetch(route, **kw)
            except Exception as e:  # noqa: BLE001
                self._safety_fail(target, f"redirect hop fetch failed ({type(e).__name__})", page)
                route.abort("blockedbyclient")
                return
            url = target
        if nav:
            self._hops = 0
        route.fulfill(response=resp)

    def _remember_good(self) -> None:
        try:
            u = self.page.url
        except Exception:
            return
        if u and not u.startswith("chrome-error") and domain_allowed(u, self.allowed_domains):
            self._last_good_url = u

    def _enforce(self) -> None:
        """动作后 / 观察前复核：关闭被拦或落在白名单外的新页面；主页面离开白名单则回到最后一个合法 URL。"""
        if not self.allowed_domains or self._ctx is None:
            return
        for p in list(self._ctx.pages):
            if p is self.page:
                continue
            try:
                bad = p in self._blocked_pages or not domain_allowed(p.url, self.allowed_domains) \
                    or p.url.startswith("chrome-error")
            except Exception:
                bad = True
            if bad:
                try:
                    url = p.url
                    if p not in self._blocked_pages and not url.startswith(("chrome-error", "about:")):
                        self._record_block(url, "new page outside allowlist")
                    p.close()
                except Exception:
                    pass
                if self.active is p:
                    self.active = self.page
        self._blocked_pages = [p for p in self._blocked_pages if not p.is_closed()]
        try:
            u = self.page.url
        except Exception:
            return
        if u.startswith("chrome-error") or not domain_allowed(u, self.allowed_domains):
            if not u.startswith("chrome-error"):
                self._record_block(u, "main page left allowlist")
            ok = False
            if self._last_good_url:
                try:
                    self.page.goto(self._last_good_url)
                    ok = True
                except Exception:  # noqa: BLE001
                    pass
            if not ok:            # v0.3.1：回不到合法页面也不能停在白名单外 → about:blank（fail-closed）
                try:
                    self.page.goto("about:blank")
                except Exception:  # noqa: BLE001
                    self.safety_failures.append(f"{u}: could not leave off-allowlist page")
        else:
            self._remember_good()

    def open(self, url: str) -> None:
        self._ensure()
        self.active = self.page
        self.page.goto(to_url(url))

    def reset(self) -> None:
        if self._pw is None:
            return
        for p in list(self._ctx.pages):
            if p is not self.page:
                p.close()
        self.active = self.page
        self.page.goto("about:blank")

    def close(self) -> None:
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        finally:
            self._pw = self._browser = self._ctx = self.page = self.active = None

    # ---------------------------------------------------------------- 观察
    def _alive_active(self):
        if self.active is None or self.active.is_closed():
            self.active = self.page
        return self.active

    def _frame_offset(self, frame) -> Optional[tuple[float, float]]:
        """子 frame 内容区左上角在主视口中的坐标（bounding_box 已是主视口坐标，再加边框 / 内边距）。"""
        fe = frame.frame_element()
        box = fe.bounding_box()
        if not box:
            return None
        try:
            bx, by = fe.evaluate(FRAME_OFFSET_JS)
        except Exception:  # noqa: BLE001
            bx = by = 0.0
        return box["x"] + bx, box["y"] + by

    def _snapshot(self, pg) -> tuple[list[dict], str]:
        """逐个 frame 抽取元素（主 frame + 同源 / 跨源 iframe），矩形换算到主视口坐标。"""
        items: list[dict] = []
        texts: list[str] = []
        for fi, frame in enumerate(pg.frames):
            try:
                off = (0.0, 0.0) if frame is pg.main_frame else self._frame_offset(frame)
                if off is None:
                    continue
                snap = frame.evaluate(SNAPSHOT_JS, [self.max_elements * 2, fi])
            except Exception:  # noqa: BLE001  — frame 正在跳转 / 已分离
                if frame is pg.main_frame:
                    raise
                continue
            for it in snap["items"]:
                l, t, r, b = it["rect"]
                it["rect"] = [l + off[0], t + off[1], r + off[0], b + off[1]]
                if fi:
                    it["frame"] = str(fi)
            items += snap["items"]
            texts += [x for x in (snap.get("text", ""), snap.get("shadow_text", "")) if x]
        return items, "\n".join(texts)[:8000]

    def _probe_frame(self, frame, token: str) -> dict:
        return frame.evaluate(FOCUS_JS, token)

    def _probe_focus(self, pg) -> tuple[Optional[dict], str]:
        """安全焦点探测：返回 (焦点元素描述或 None, "known" | "none" | "unknown")。任何异常 → unknown。"""
        try:
            frame, off = pg.main_frame, (0.0, 0.0)
            for _ in range(10):
                token = f"f{time.time_ns()}"
                info = self._probe_frame(frame, token)
                kind = (info or {}).get("kind")
                if kind == "none":
                    return None, "none"
                if kind == "element":
                    l, t, r, b = info["rect"]
                    info["rect"] = [l + off[0], t + off[1], r + off[0], b + off[1]]
                    return info, "known"
                if kind != "frame":
                    return None, "unknown"
                child = None
                for ch in frame.child_frames:
                    try:
                        if ch.frame_element().get_attribute("data-gua-focus-frame") == token:
                            child = ch
                            break
                    except Exception:  # noqa: BLE001
                        continue
                if child is None:
                    return None, "unknown"
                o = self._frame_offset(child)
                if o is None:
                    return None, "unknown"
                frame, off = child, o
            return None, "unknown"
        except Exception:  # noqa: BLE001
            return None, "unknown"

    @staticmethod
    def _apply_focus(elems: list, info: Optional[dict], img_size) -> None:
        """把探测到的焦点标到元素列表上（匹配不到就追加一个元素；与候选数量上限无关）。"""
        for e in elems:
            e.focused = False
        if not info:
            return
        from .base import UIElement
        raw = web_raws([info])[0]
        l, t, r, b = (int(round(v)) for v in info["rect"])
        match = None
        for e in elems:
            el, et, er, eb = e.rect
            if abs(el - max(0, l)) <= 2 and abs(et - max(0, t)) <= 2 and e.name == raw["name"][:100].strip():
                match = e
                break
        if match is None:
            w, h = img_size
            off = r <= 0 or b <= 0 or l >= w or t >= h
            match = UIElement(len(elems), re.sub(r"\s+", " ", raw["name"]).strip()[:100], raw["role"],
                              (max(0, l), max(0, t), min(w, max(r, l + 1)), min(h, max(b, t + 1))),
                              enabled=raw["enabled"], native_role=raw["native_role"], attrs=dict(raw["attrs"]),
                              offscreen=off)
            elems.append(match)
        match.focused = True
        match.is_password = match.is_password or raw["is_password"]
        if match.is_password:
            match.value = None
        if raw["attrs"].get("form_submit"):
            match.attrs["form_submit"] = raw["attrs"]["form_submit"]

    def observe(self, with_elements: bool = True) -> Observation:
        self._ensure()
        self._enforce()
        pg = self._alive_active()
        img = Image.open(io.BytesIO(pg.screenshot(type="png"))).convert("RGB")
        title, url, text, elems = "", "", "", []
        focus_state = ""
        try:
            title, url = pg.title(), pg.url
        except Exception:
            pass
        if with_elements:
            try:
                items, text = self._snapshot(pg)
                raws = web_raws(items)
                for r, it in zip(raws, items):
                    if it.get("covered"):
                        r["attrs"]["covered"] = "true"
                elems, _ = finalize(raws, img.size, self.max_elements, include_text=False)
            except Exception as e:  # 页面跳转中
                text = f"(snapshot failed: {type(e).__name__})"
            info, focus_state = self._probe_focus(pg)
            self._apply_focus(elems, info, img.size)
        wins = []
        for p in self._ctx.pages:
            try:
                wins.append(p.title() or p.url)
            except Exception:
                pass
        return Observation(screenshot=img, timestamp=time.time(), screen_size=img.size, dpi_scale=1.0,
                           active_window=title or url, active_process=urlparse(url).netloc or url[:40],
                           windows=wins, elements=elems, platform="web", url=url, text=text, cursor=self.cursor,
                           focus_state=focus_state)

    # ---------------------------------------------------------------- 执行
    def _domain_ok(self, url: str) -> bool:
        return domain_allowed(url, self.allowed_domains)

    def execute(self, a: Action) -> ExecResult:
        r = self._execute(a)
        if a.x is not None and a.y is not None and a.is_pointer:
            self.cursor = (int(a.x2), int(a.y2)) if a.type == "drag" and a.x2 is not None else (int(a.x), int(a.y))
        if self.allowed_domains:
            try:
                self._alive_active().wait_for_timeout(60)   # 让点击触发的导航 / 弹窗事件到达
            except Exception:
                pass
            self._enforce()
            if self._unreported:
                msg = "; ".join(self._unreported)
                self._unreported = []
                return ExecResult(False, f"blocked_by_safety: off-allowlist navigation blocked: {msg}"[:400],
                                  r.started, time.time())
        return r

    def _execute(self, a: Action) -> ExecResult:
        self._ensure()
        t0 = time.time()
        if not self.supports(a.type):
            return ExecResult(False, f"unsupported action {a.type} on web", t0, time.time())
        pg = self._alive_active()
        w, h = self.viewport
        err = self._bounds_error(a, w, h)
        if err:
            return ExecResult(False, err, t0, time.time())
        try:
            m, kb = pg.mouse, pg.keyboard
            if a.type == "click":
                m.click(a.x, a.y)
            elif a.type == "double_click":
                m.dblclick(a.x, a.y)
            elif a.type == "right_click":
                m.click(a.x, a.y, button="right")
            elif a.type == "move":
                m.move(a.x, a.y)
            elif a.type == "drag":
                m.move(a.x, a.y)
                m.down()
                m.move(a.x2, a.y2, steps=10)
                m.up()
            elif a.type == "scroll":
                if a.x is not None:
                    m.move(a.x, a.y)
                d = a.amount * self.scroll_unit_px
                dx, dy = {"up": (0, -d), "down": (0, d), "left": (-d, 0), "right": (d, 0)}[a.direction]
                m.wheel(dx, dy)
                time.sleep(0.2)
            elif a.type == "type":
                if a.clear:
                    kb.press("Control+A")
                    kb.press("Backspace")
                kb.type(a.text or "", delay=5)
                if a.submit:
                    kb.press("Enter")
            elif a.type == "hotkey":
                kb.press("+".join(pw_key(k) for k in a.keys))
            elif a.type == "key_down":
                for k in a.keys:
                    kb.down(pw_key(k))
            elif a.type == "key_up":
                for k in a.keys:
                    kb.up(pw_key(k))
            elif a.type == "wait":
                time.sleep(min(a.seconds or 1.0, 10.0))
            elif a.type == "navigate":
                url = to_url(a.url or a.text or "")
                if not self._domain_ok(url):
                    return ExecResult(False, f"blocked_by_safety domain not allowed: {url}", t0, time.time())
                pg.goto(url)
            elif a.type == "back":
                pg.go_back()
            elif a.type == "focus_window":
                if not self.focus_window(a.text or ""):
                    return ExecResult(False, f"window_not_found {a.text!r}", t0, time.time())
            return ExecResult(True, "", t0, time.time())
        except Exception as e:  # noqa: BLE001
            return ExecResult(False, f"{type(e).__name__}: {str(e)[:200]}", t0, time.time())

    def focus_window(self, title_substring: str) -> bool:
        self._ensure()
        pages = list(self._ctx.pages)
        target = None
        if not title_substring:
            target = self.page
        else:
            for p in pages:
                try:
                    if title_substring.lower() in (p.title() or "").lower() or title_substring in p.url:
                        target = p
                        break
                except Exception:
                    continue
        if target is None:
            return False
        target.bring_to_front()
        self.active = target
        return True

    # ---------------------------------------------------------------- 评测 / 干扰用
    def eval_js(self, expr: str):
        self._ensure()
        return self.page.evaluate(expr)

    def disturb(self, kind: str) -> None:
        """可重复注入的 Web 干扰（对应桌面端 eval/disturb.py）。"""
        self._ensure()
        if kind == "new_tab":
            p = self._ctx.new_page()
            p.set_content("<title>Distractor</title><h1>Unrelated tab</h1>")
            self.active = p
        elif kind == "popup":
            self.page.evaluate("""() => { const d = document.createElement('div');
              d.setAttribute('role','dialog'); d.setAttribute('aria-label','Injected notice');
              d.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:99999;display:flex;align-items:center;justify-content:center';
              d.innerHTML='<div style="background:#fff;padding:30px"><p>Injected popup</p><button onclick="this.closest(\\'[role=dialog]\\').remove()">Close notice</button></div>';
              document.body.appendChild(d); }""")
        elif kind == "reload":
            self.page.reload()
        elif kind == "scroll_away":
            self.page.mouse.wheel(0, 3000)
        else:
            raise ValueError(kind)

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

SNAPSHOT_JS = r"""
(maxN) => {
  const SEL = 'a[href],button,input,select,textarea,summary,[role],[onclick],[tabindex]:not([tabindex="-1"]),[contenteditable="true"],dialog[open],label';
  const vw = window.innerWidth, vh = window.innerHeight;
  const out = [];
  let gid = 0;
  const nameOf = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria;
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const n = document.getElementById(lb); if (n) return n.innerText; }
    if (el.labels && el.labels.length) return el.labels[0].innerText;
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') return el.placeholder || el.name || el.id || '';
    if (el.tagName === 'SELECT') { const o = el.options[el.selectedIndex]; return (el.name || el.id || '') + (o ? ': ' + o.text : ''); }
    if (el.tagName === 'IMG') return el.alt || '';
    const t = (el.innerText || el.textContent || '').trim();
    if (el.matches('dialog,[role=dialog],[role=alertdialog]')) return (el.getAttribute('aria-label') || t.split('\n')[0] || 'dialog');
    return t || el.title || el.value || '';
  };
  for (const el of document.querySelectorAll(SEL)) {
    if (out.length >= maxN) break;
    const st = window.getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || parseFloat(st.opacity) === 0) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    if (el.tagName === 'LABEL' && el.control) continue;
    let role = el.getAttribute('role') || '';
    if (el.tagName === 'DIALOG' || el.getAttribute('aria-modal') === 'true') role = role || 'dialog';
    // 被其他元素遮挡（例如模态遮罩）的元素标记 covered
    let covered = false;
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    if (cx >= 0 && cy >= 0 && cx < vw && cy < vh && role !== 'dialog') {
      const top = document.elementFromPoint(cx, cy);
      covered = !!top && top !== el && !el.contains(top) && !top.contains(el);
    }
    el.setAttribute('data-gua-id', String(gid));
    out.push({gid: gid++, tag: el.tagName.toLowerCase(), role: role, type: el.getAttribute('type') || '',
      name: nameOf(el).slice(0, 120), value: (el.value !== undefined && el.tagName !== 'BUTTON') ? String(el.value) : null,
      rect: [r.left, r.top, r.right, r.bottom], disabled: !!el.disabled, focused: document.activeElement === el,
      checked: (el.type === 'checkbox' || el.type === 'radio') ? !!el.checked : null, covered: covered,
      href: el.getAttribute('href') || ''});
  }
  return {items: out, text: (document.body ? document.body.innerText : '').slice(0, 6000),
          title: document.title, url: location.href, vw: vw, vh: vh};
}
"""

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
                 slow_mo: int = 0, block_subresources: bool = False):
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

    def _route(self, route) -> None:
        req = route.request
        url = req.url
        try:
            page = req.frame.page
        except Exception:
            page = None
        try:
            nav = req.is_navigation_request()
        except Exception:
            nav = False
        if not domain_allowed(url, self.allowed_domains):
            if nav or self.block_subresources:
                self._record_block(url, "navigation to host outside allowlist" if nav else "subresource", page)
                route.abort("blockedbyclient")
            else:
                route.continue_()
            return
        if not nav or urlparse(url).scheme not in {"http", "https"}:
            route.continue_()
            return
        try:                      # 先取响应、不跟随重定向：3xx 指向白名单外则拦截
            resp = route.fetch(max_redirects=0)
        except Exception:
            route.continue_()
            return
        if 300 <= resp.status < 400:
            loc = resp.headers.get("location", "")
            target = urljoin(url, loc) if loc else ""
            if target and not domain_allowed(target, self.allowed_domains):
                self._record_block(target, f"redirect from {url}", page)
                route.abort("blockedbyclient")
                return
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
            if self._last_good_url:
                try:
                    self.page.goto(self._last_good_url)
                except Exception:
                    pass
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

    def observe(self, with_elements: bool = True) -> Observation:
        self._ensure()
        self._enforce()
        pg = self._alive_active()
        img = Image.open(io.BytesIO(pg.screenshot(type="png"))).convert("RGB")
        title, url, text, elems = "", "", "", []
        try:
            title, url = pg.title(), pg.url
        except Exception:
            pass
        if with_elements:
            try:
                snap = pg.evaluate(SNAPSHOT_JS, self.max_elements * 2)
                raws = web_raws(snap["items"])
                for r, it in zip(raws, snap["items"]):
                    if it.get("covered"):
                        r["attrs"]["covered"] = "true"
                elems, _ = finalize(raws, img.size, self.max_elements, include_text=False)
                text = snap.get("text", "")
            except Exception as e:  # 页面跳转中
                text = f"(snapshot failed: {type(e).__name__})"
        wins = []
        for p in self._ctx.pages:
            try:
                wins.append(p.title() or p.url)
            except Exception:
                pass
        return Observation(screenshot=img, timestamp=time.time(), screen_size=img.size, dpi_scale=1.0,
                           active_window=title or url, active_process=urlparse(url).netloc or url[:40],
                           windows=wins, elements=elems, platform="web", url=url, text=text, cursor=self.cursor)

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

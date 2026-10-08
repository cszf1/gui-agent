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

第三轮审查（web 条目 1-6）：
1. URL：检查与真实请求共用 ``urlpolicy.normalize`` 的严格结果；反斜杠 / 控制空白 / userinfo / 百分号 authority /
   数字·十六进制 IP 别名一律拒绝，绝不"先 replace 再比较"。Location 重定向只允许 http/https。
2. 重定向不再拼接内联 script：用 html.escape 编码的 **meta refresh** 合成无脚本跳转页，每一跳重新进 route；
   合成页丢掉原 3xx 上会卡住跳转的 CSP / sandbox / refresh / content-encoding / content-length 等头，
   换成严格无脚本 CSP，并保留 Set-Cookie（多个也保留）。
3. 手动逐跳 fetch 维护 current_url / current_method / current_body / current_headers：
   303（HEAD 例外）、301/302 的 POST 转 GET 后清空 body 与 body 相关头，307/308 保留当前状态（不恢复原 POST）；
   跨 origin 删除 authorization / proxy-authorization / cookie 等敏感请求头；所有异常 abort，绝不重发或按类型重试。
   保留非 GET/HEAD 方法的跨源导航重定向在下一跳请求前拒绝，避免把目标文档交付为来源站点的文档。
4. 敏感输入：SNAPSHOT_JS / FOCUS_JS 都按 autocomplete ASCII 空白 token 与 CSS 掩码判定密码框；
   掩码 contenteditable 的文本（含 textContent）从名字 / body 文本 / 祖先名字里整棵排除；
   closed shadow host（或任何无法证明是原生输入目标的元素）不报告为"已知焦点"，一律保守 unknown。
5. 表单提交目标提供稳定 DOM 身份：dom_id / form_submit_id（DOM WeakMap + 递增 uid，跨 frame 带 frame 前缀），
   同一提交按钮的点击与文本框回车共享身份；不拿随候选排序漂移的 UIElement.id。
6. clear / type 绑定动作开始时真实焦点元素的 handle（穿透 open shadow / iframe）；clear 或每个输入阶段焦点改变 /
   目标消失 → 在发送秘密前返回 blocked_by_safety，绝不把焦点拉回去替用户执行；execute() 先跑 a.validate()。
"""
from __future__ import annotations

import html
import io
import re
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

from PIL import Image

from ..actions import Action
from ..keys import canonical_key
from ..urlpolicy import UrlRejected, domain_allowed, normalize
from .a11y import finalize, web_raws
from .base import Env, ExecResult, Observation
from .web_state import STABILITY_JS

# 每次 type 复查焦点之间最多发送的字符数（输入事件可能中途移焦，不能整串无检查地交给全局 keyboard）
_TYPE_CHUNK = 1

# 公共 JS 片段：命名 / 密码判定 / 表单提交按钮 / 稳定 DOM 身份（快照与焦点探测共用）
_JS_HELPERS = r"""
  const rootOf = (el) => el.getRootNode ? el.getRootNode() : document;
  const PW_AUTOCOMPLETE = ['current-password', 'new-password', 'one-time-code'];
  const secureOf = (el) => {
    if (!el || el.nodeType !== 1) return false;
    if ((el.getAttribute('type') || '').toLowerCase() === 'password') return true;
    // autocomplete 按 HTML 规范的 ASCII 空白 token 匹配（section-blue current-password ... 也算）
    const ac = (el.getAttribute('autocomplete') || '').toLowerCase().split(/[\t\n\f\r ]+/);
    for (let i = 0; i < ac.length; i++) if (PW_AUTOCOMPLETE.indexOf(ac[i]) >= 0) return true;
    try { const st = window.getComputedStyle(el);
          const ts = st.webkitTextSecurity || st.getPropertyValue('-webkit-text-security');
          if (ts && ts !== 'none') return true; } catch (e) {}
    return false;
  };
  const BLOCK_TAGS = {DIV:1,P:1,BR:1,LI:1,TR:1,SECTION:1,ARTICLE:1,HEADER:1,FOOTER:1,H1:1,H2:1,H3:1,H4:1,H5:1,
                      H6:1,TABLE:1,UL:1,OL:1,FORM:1,BLOCKQUOTE:1,PRE:1,HR:1,DL:1,DD:1,DT:1,FIGURE:1,
                      FIGCAPTION:1,NAV:1,MAIN:1,ASIDE:1};
  // 文本提取：整棵跳过安全子树（密码框 / CSS 掩码 / autocomplete 密码 token），
  // 因此掩码 contenteditable 的真实 textContent 不会经名字 / body 文本 / 祖先名字泄漏。
  const textOf = (node) => {
    let s = '';
    const walk = (n) => {
      if (!n) return;
      const kids = n.childNodes || [];
      for (let i = 0; i < kids.length; i++) {
        const c = kids[i];
        if (c.nodeType === 3) { s += c.nodeValue; continue; }
        if (c.nodeType !== 1) continue;
        if (secureOf(c)) continue;
        const tag = c.tagName;
        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT' || tag === 'TEMPLATE') continue;
        let cs = null; try { cs = window.getComputedStyle(c); } catch (e) {}
        if (cs && (cs.display === 'none' || cs.visibility === 'hidden')) continue;
        if (BLOCK_TAGS[tag]) {
          if (s && !/\n$/.test(s)) s += '\n';
          walk(c);
          if (s && !/\n$/.test(s)) s += '\n';
        } else { walk(c); }
      }
    };
    walk(node);
    return s;
  };
  const nameOf = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria;
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const r = rootOf(el); const n = (r.getElementById ? r.getElementById(lb) : null) || document.getElementById(lb);
              if (n) return textOf(n); }
    if (el.labels && el.labels.length) return textOf(el.labels[0]);
    if (el.tagName === 'INPUT' && ['submit', 'button', 'reset'].includes((el.type || '').toLowerCase()))
      return el.value || el.name || el.id || '';
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') return el.placeholder || el.name || el.id || '';
    if (el.tagName === 'SELECT') { const o = el.options[el.selectedIndex]; return (el.name || el.id || '') + (o ? ': ' + o.text : ''); }
    if (el.tagName === 'IMG') return el.alt || '';
    if (secureOf(el)) return el.title || '';
    const t = textOf(el).trim();
    if (el.matches('dialog,[role=dialog],[role=alertdialog]')) return (el.getAttribute('aria-label') || t.split('\n')[0] || 'dialog');
    return t || el.title || (el.tagName === 'INPUT' ? '' : (el.value || '')) || '';
  };
  const submitElOf = (el) => {
    const f = el.form; if (!f) return null;
    return f.querySelector('button:not([type]),button[type=submit],input[type=submit],input[type=image]');
  };
  const submitOf = (el) => {
    const b = submitElOf(el);
    return b ? nameOf(b).slice(0, 120) : (el.form ? (el.form.getAttribute('aria-label') || '') : '');
  };
  const domIdOf = (el) => {
    const w = window;
    if (!w.__guaDomIds) { w.__guaDomIds = new WeakMap(); w.__guaDomSeq = 0; }
    let id = w.__guaDomIds.get(el);
    if (!id) { id = ++w.__guaDomSeq; w.__guaDomIds.set(el, id); }
    return id;
  };
  const domId = (el, fid) => String(fid) + '-' + String(domIdOf(el));
  const documentId = () => {
    if (!window.__guaDocumentId) window.__guaDocumentId = String(performance.timeOrigin) + '-' + String(Math.random());
    return window.__guaDocumentId;
  };
  const submitId = (el, fid) => { const b = submitElOf(el); return b ? domId(b, fid) : ''; };
  // 只有能证明"键盘输入确实落到这个原生控件"的元素才算已知焦点；div / 自定义节点可能挂 closed shadow root，
  // 无法证明输入目标 → 一律不报告为已知（见 FOCUS_JS / FOCUS_HANDLE_JS）。
  const NATIVE_FOCUS = {INPUT:1,TEXTAREA:1,SELECT:1,BUTTON:1,OPTION:1,SUMMARY:1};
  const isNativeFocus = (el) => {
    if (!el || el.nodeType !== 1) return false;
    const tag = el.tagName;
    if (NATIVE_FOCUS[tag]) return true;
    if ((tag === 'A' || tag === 'AREA') && el.hasAttribute('href')) return true;
    return false;
  };
"""

# 元素快照（穿透 open shadow root；iframe 由 Python 逐个 frame 调用本脚本并加上 frame 偏移）
SNAPSHOT_JS = r"""
([maxN, fid]) => {
""" + _JS_HELPERS + r"""
  const SEL = 'a[href],button,input,select,textarea,summary,progress,[role],[onclick],[aria-busy="true"],[tabindex]:not([tabindex="-1"]),[contenteditable="true"],dialog[open],label';
  const vw = window.innerWidth, vh = window.innerHeight;
  const out = [];
  const nodes = new Map();
  window.__guaSnapshotNodes = nodes;
  const texts = [];
  let gid = 0;
  const roots = [document];
  for (let i = 0; i < roots.length && i < 200; i++) {        // 广度优先遍历所有 open shadow root
    const r = roots[i];
    for (const el of r.querySelectorAll('*')) if (el.shadowRoot && el.shadowRoot.mode === 'open') roots.push(el.shadowRoot);
  }
  for (const root of roots) {
    if (root !== document) { for (const c of root.children) { const t = textOf(c).trim(); if (t) texts.push(t); } }
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
      nodes.set(domId(el, fid), el);
      const se = submitElOf(el);
      let vnow = el.getAttribute('aria-valuenow'), vmax = el.getAttribute('aria-valuemax');
      if (el.tagName === 'PROGRESS' && el.hasAttribute('value')) { vnow = String(el.value); vmax = String(el.max); }
      out.push({gid: gid++, tag: el.tagName.toLowerCase(), role: role, type: el.getAttribute('type') || '',
        name: nameOf(el).slice(0, 120),
        value: (!secure && el.value !== undefined && el.tagName !== 'BUTTON' && el.tagName !== 'PROGRESS') ? String(el.value) : null,
        rect: [r.left, r.top, r.right, r.bottom], disabled: !!el.disabled, focused: false,
        checked: (el.type === 'checkbox' || el.type === 'radio') ? !!el.checked : null, covered: covered,
        href: el.getAttribute('href') || '', secure: secure, autocomplete: el.getAttribute('autocomplete') || '',
        form_submit: (el.form ? submitOf(el) : ''), busy: el.getAttribute('aria-busy') === 'true' ? 'true' : '',
        dom_id: domId(el, fid), document_id: documentId(), form_submit_id: (se ? domId(se, fid) : ''),
        valuenow: vnow, valuemax: vmax});
    }
  }
  return {items: out, text: (document.body ? textOf(document.body) : '').slice(0, 6000),
          shadow_text: texts.join('\n').slice(0, 2000),
          title: document.title, url: location.href, vw: vw, vh: vh};
}
"""

# 安全焦点探测：沿 activeElement 穿透 open shadow root；落在 iframe 上时给它打标记，
# 由 Python 找到对应的子 frame 继续探测（跨源 frame 也可以，Playwright 有特权访问）。
# 只有原生输入目标才报告 kind=element；div / 自定义节点（可能挂 closed shadow root）→ kind=unknown。
FOCUS_JS = r"""
([token, fid]) => {
""" + _JS_HELPERS + r"""
  let a = document.activeElement;
  while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
  if (!a || a === document.body || a === document.documentElement) return {kind: 'none'};
  if (a.tagName === 'IFRAME' || a.tagName === 'FRAME') { a.setAttribute('data-gua-focus-frame', token); return {kind: 'frame'}; }
  if (!isNativeFocus(a)) return {kind: 'unknown'};
  const r = a.getBoundingClientRect();
  return {kind: 'element', tag: a.tagName.toLowerCase(), role: a.getAttribute('role') || '',
          type: a.getAttribute('type') || '', name: nameOf(a).slice(0, 120), secure: secureOf(a),
          autocomplete: a.getAttribute('autocomplete') || '', form_submit: submitOf(a),
          dom_id: domId(a, fid), document_id: documentId(), form_submit_id: submitId(a, fid),
          rect: [r.left, r.top, r.right, r.bottom], disabled: !!a.disabled, editable: !!a.isContentEditable};
}
"""

# clear / type 的绑定目标：返回 activeElement 的元素句柄；iframe 打标记走子 frame；非原生目标返回 null。
FOCUS_HANDLE_JS = r"""
(token) => {
""" + _JS_HELPERS + r"""
  let a = document.activeElement;
  while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
  if (!a || a === document.body || a === document.documentElement) return null;
  if (a.tagName === 'IFRAME' || a.tagName === 'FRAME') { a.setAttribute('data-gua-focus-frame', token); return null; }
  if (!isNativeFocus(a)) return null;
  return a;
}
"""

FRAME_OFFSET_JS = r"""(e) => { const cs = window.getComputedStyle(e);
  return [e.clientLeft + (parseFloat(cs.paddingLeft) || 0), e.clientTop + (parseFloat(cs.paddingTop) || 0)]; }"""


def redirect_html(target: str) -> str:
    """无脚本跳转页：meta refresh（目标用 html.escape 正确编码），不拼接任何内联 script。"""
    return ('<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
            '<title>redirect</title>'
            '<meta http-equiv="refresh" content="0; url=' + html.escape(target, quote=True) + '">')


def _next_method(status: int, method: str) -> str:
    """重定向后的方法（RFC 9110）：303 → GET（HEAD 例外仍 HEAD）；301/302 的 POST → GET；307/308 不变。"""
    if status == 303:
        return "HEAD" if method == "HEAD" else "GET"
    if status in (301, 302) and method == "POST":
        return "GET"
    return method


def _origin(url: str) -> tuple:
    u = urlparse(url)
    port = u.port or (443 if u.scheme == "https" else 80)
    return (u.scheme, (u.hostname or "").lower(), port)


_KEYMAP = {"ctrl": "Control", "control": "Control", "cmd": "Meta", "command": "Meta", "win": "Meta", "insert": "Insert",
           "meta": "Meta", "alt": "Alt", "option": "Alt", "shift": "Shift", "enter": "Enter",
           "return": "Enter", "esc": "Escape", "escape": "Escape", "tab": "Tab", "backspace": "Backspace",
           "delete": "Delete", "del": "Delete", "space": "Space", "up": "ArrowUp", "down": "ArrowDown",
           "left": "ArrowLeft", "right": "ArrowRight", "pageup": "PageUp", "pagedown": "PageDown",
           "home": "Home", "end": "End"}

# 合成跳转页必须丢掉的响应头：会卡住 / 干扰跳转的原 3xx 头（CSP / sandbox / refresh / 长度 / 编码等）。
# 真正的目标页 CSP 不受影响（只处理中间跳转页）。
_REDIRECT_DROP = frozenset({
    "location", "content-length", "content-type", "content-encoding", "transfer-encoding",
    "content-security-policy", "content-security-policy-report-only", "x-frame-options", "refresh",
    "clear-site-data",
})
# 严格无脚本 CSP；frame-ancestors * 明确不禁用合法的 iframe 跳转。
_REDIRECT_CSP = ("default-src 'none'; script-src 'none'; object-src 'none'; base-uri 'none'; "
                 "form-action 'none'; frame-ancestors *")
_SENSITIVE_REQUEST_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie"})
_BODY_HEADERS = frozenset({"content-type", "content-length", "content-encoding", "transfer-encoding"})


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
    targeted_input = True
    scroll_unit_px = 100

    def __init__(self, start_url: str = "about:blank", headless: bool = True,
                 viewport: tuple[int, int] = (1280, 800), browser: str = "chromium",
                 allowed_domains: Optional[list[str]] = None, max_elements: int = 150,
                 slow_mo: int = 0, block_subresources: bool = False, fetch_timeout: float = 30.0,
                 max_redirect_hops: int = 20, channel: Optional[str] = None,
                 executable_path: Optional[str] = None):
        self.start_url = start_url
        self.headless = headless
        self.viewport = viewport
        self.browser_name = browser
        self.channel = channel
        self.executable_path = executable_path
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
        self._restore_main_pending = False         # 主导航已 abort；错误页可能尚未提交
        self._nav_in_flight = 0                    # route.fetch 尚未结束的导航检查
        self._last_good_url: Optional[str] = None
        self.cursor: Optional[tuple[int, int]] = None
        self.fetch_timeout = fetch_timeout          # 白名单预取超时（秒）；超时 = 拦截（fail-closed），不会重发
        self.max_redirect_hops = max_redirect_hops
        self.safety_failures: list[str] = []         # 白名单检查本身失败（预取异常、处理器异常）→ 已 fail-closed 拦截
        self._hops = 0                               # 连续客户端重定向跳数（防重定向环）
        self._last_focus_secure: bool = False
        self._last_focus_dom_id: Optional[str] = None
        self._last_focus_page = self._last_focus_frame = self._last_focus_handle = None
        self._snapshot_id = ""
        self._snapshot_page = None
        self._snapshot_nodes: dict = {}
        self._bound_elements: dict = {}

    semantic_actions = True      # v0.6：DOM 语义动作（不移动页面指针；被遮挡时拒绝后台、交给前台真实输入）

    def bind_action(self, action: Action, obs: Observation) -> Action:
        return replace(action, binding={"snapshot_id": obs.snapshot_id})

    def pointer_position(self):
        return tuple(self.cursor) if self.cursor else None

    def foreground_token(self) -> str:
        try:
            return f"page:{id(self._alive_active())}"
        except Exception:  # noqa: BLE001
            return ""

    def element_identity(self, element):
        doc, node = element.attrs.get("document_id"), element.attrs.get("dom_id")
        return (doc, node) if doc and node else None

    def _release_snapshot(self):
        for _, nodes in self._snapshot_nodes.values():
            try:
                nodes.dispose()
            except Exception:
                pass
        self._snapshot_nodes.clear()
        self._bound_elements.clear()

    # ---------------------------------------------------------------- 生命周期
    def _ensure(self) -> None:
        if self._pw is not None:
            return
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        launcher = getattr(self._pw, self.browser_name)
        options = {"headless": self.headless, "slow_mo": self.slow_mo}
        if self.channel:
            options["channel"] = self.channel
        if self.executable_path:
            options["executable_path"] = self.executable_path
        self._browser = launcher.launch(**options)
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
        """route.fetch 返回 APIResponse（不是 Response）。任何异常都在调用处 abort，绝不重发 / 按类型重试。"""
        return route.fetch(max_redirects=0, timeout=self.fetch_timeout * 1000, **kw)

    def _route(self, route) -> None:
        """浏览器层白名单（任何异常都 fail-closed——abort 并记录安全失败，绝不 continue_）。"""
        url = "?"
        blocked_before = len(self._unreported)
        try:
            navigation = route.request.is_navigation_request()
        except Exception:
            navigation = True
        if navigation:
            self._nav_in_flight += 1
        try:
            url = route.request.url
            self._route_inner(route)
        except Exception as e:  # noqa: BLE001
            self._safety_fail(url, f"route handler error {type(e).__name__}: {str(e)[:120]}")
            try:
                route.abort("blockedbyclient")
            except Exception:  # noqa: BLE001
                pass
        finally:
            if len(self._unreported) > blocked_before:
                try:
                    req = route.request
                    if (req.is_navigation_request() and self.page is not None
                            and req.frame is self.page.main_frame):
                        # route.abort can return before Chromium commits its error
                        # document. Restore explicitly even if page.url still shows
                        # the old allowed URL; waiting a fixed 60 ms is insufficient.
                        self._restore_main_pending = True
                except Exception:  # detached frame / incomplete request metadata
                    pass
            if navigation:
                self._nav_in_flight -= 1

    @staticmethod
    def _request_headers(req) -> Optional[dict]:
        """当前请求头（小写名）。取不到（测试替身 / 已分离）返回 None。"""
        try:
            h = req.headers
            return {str(k).lower(): str(v) for k, v in dict(h).items()}
        except Exception:  # noqa: BLE001
            return None

    def _passthrough_headers(self, resp) -> dict:
        """原 3xx 响应头 → 合成跳转页头：丢掉会卡住跳转的头（含 CSP / sandbox / refresh / 长度编码），保留其余（含多个 Set-Cookie）。"""
        out: dict = {}
        for k, v in resp.headers.items():
            name = str(k).lower()
            if name in _REDIRECT_DROP:
                continue
            out[name] = str(v)
        return out

    def _route_inner(self, route) -> None:
        req = route.request
        raw_url = req.url
        try:
            page = req.frame.page
        except Exception:
            page = None
        try:
            nav = req.is_navigation_request()
        except Exception:         # 判断不了是不是导航：按导航处理（保守）
            nav = True
        if not domain_allowed(raw_url, self.allowed_domains):
            if nav or self.block_subresources:
                self._record_block(raw_url, "navigation to host outside allowlist" if nav else "subresource", page)
                route.abort("blockedbyclient")
            else:
                route.continue_()
            return
        try:
            url = normalize(raw_url)               # 检查与真实请求共用同一个规范化 URL
        except UrlRejected as e:
            if nav or self.block_subresources:
                self._record_block(raw_url, f"ambiguous url: {e}", page)
                route.abort("blockedbyclient")
            else:
                route.continue_()
            return
        if not (nav or self.block_subresources) or urlparse(url).scheme not in {"http", "https"}:
            route.continue_()
            return
        method = (getattr(req, "method", "GET") or "GET").upper()
        try:
            body = req.post_data_buffer if method not in ("GET", "HEAD") else None
        except Exception:  # noqa: BLE001
            body = None
        headers = self._request_headers(req)
        # 先取响应、不跟随重定向（请求只发送这一次：之后用 fulfill 交给浏览器，不再 continue_）
        try:
            resp = self._fetch(route)
        except Exception as e:  # noqa: BLE001  — 超时 / 连接失败：不知道服务器是否已收到，绝不重发
            self._safety_fail(url, f"allowlist prefetch failed ({type(e).__name__}); not retried", page)
            route.abort("blockedbyclient")
            return
        hops = 0
        while 300 <= resp.status < 400:
            loc = resp.headers.get("location", "")
            if not loc:
                break
            joined = urljoin(url, loc)
            try:
                target = normalize(joined, allow_local=False)   # Location 只允许 http/https（默认无白名单也不insert javascript）
            except UrlRejected as e:
                self._record_block(joined, f"redirect Location rejected ({e})", page)
                route.abort("blockedbyclient")
                return
            if not domain_allowed(target, self.allowed_domains):
                self._record_block(target, f"redirect from {url}", page)
                route.abort("blockedbyclient")
                return
            new_method = _next_method(resp.status, method)
            if nav and method not in ("GET", "HEAD") and new_method == method \
                    and _origin(target) != _origin(raw_url):
                # fulfill 只能完成原始请求，无法把文档 origin 改为 target。
                # 不预取目标，也不重放正文：方法保留的跨源导航必须拒绝。
                self._record_block(target, "method-preserving cross-origin navigation redirect "
                                   "cannot safely preserve the document origin", page)
                route.abort("blockedbyclient")
                return
            if nav and (method in ("GET", "HEAD") or new_method != method):
                # 导航：改成客户端 meta refresh（无脚本），下一跳是新的导航请求，会再次进入本处理器逐跳检查；
                # 原响应头（含 Set-Cookie）按 Playwright 约定保留，去掉会卡住跳转的头。
                self._hops += 1
                if self._hops > self.max_redirect_hops:
                    self._safety_fail(target, f"more than {self.max_redirect_hops} redirect hops", page)
                    route.abort("blockedbyclient")
                    return
                headers_out = self._passthrough_headers(resp)
                headers_out["content-type"] = "text/html; charset=utf-8"
                headers_out["content-security-policy"] = _REDIRECT_CSP
                route.fulfill(status=200, headers=headers_out, body=redirect_html(target))
                return
            # 子资源 / 307·308 非 GET：在这里逐跳跟随（每跳检查白名单 / 方法 / 请求头），最后把最终响应交给浏览器
            hops += 1
            if hops > self.max_redirect_hops:
                self._safety_fail(target, f"more than {self.max_redirect_hops} redirect hops", page)
                route.abort("blockedbyclient")
                return
            if headers is not None and _origin(target) != _origin(url):
                headers = {k: v for k, v in headers.items() if k not in _SENSITIVE_REQUEST_HEADERS}
            if method != new_method:            # POST→GET（303 / 301 / 302）：清空 body 与 body 相关头
                body = None
                if headers is not None:
                    headers = {k: v for k, v in headers.items() if k not in _BODY_HEADERS}
            method = new_method
            kw: dict = {"url": target, "method": method}
            kw["post_data"] = "" if method in ("GET", "HEAD") else (body or "")
            if headers:
                kw["headers"] = headers
            elif headers is not None:
                kw["headers"] = {"accept": "*/*"}   # 空也不能回退到原请求头（否则会带出刚删掉的敏感头）
            try:
                resp = self._fetch(route, **kw)
            except Exception as e:  # noqa: BLE001
                self._safety_fail(target, f"redirect hop fetch failed ({type(e).__name__})", page)
                route.abort("blockedbyclient")
                return
            url = target
        if nav:
            self._hops = 0
        if urlparse(req.url).netloc != urlparse(url).netloc:
            # 跨源重定向：剥离 origin 作用域响应头（Set-Cookie / Clear-Site-Data），防跨源 Cookie 注入
            headers = {k: v for k, v in resp.headers.items() if k.lower() not in {"set-cookie", "clear-site-data"}}
            body = resp.body() if callable(getattr(resp, "body", None)) else b""
            route.fulfill(status=resp.status, headers=headers, body=body)
        else:
            route.fulfill(response=resp)

    def _remember_good(self) -> None:
        try:
            u = self.page.url
        except Exception:
            return
        if u and not self._hops and not u.startswith("chrome-error") and domain_allowed(u, self.allowed_domains):
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
        if self._restore_main_pending and not u.startswith("chrome-error") and domain_allowed(u, self.allowed_domains):
            try:
                # Wait for the rejected navigation's commit rather than race its
                # late error document with a recovery goto. A cancelled navigation
                # can keep the old document, so this wait is bounded.
                self.page.wait_for_event("framenavigated", predicate=lambda f: f is self.page.main_frame,
                                         timeout=2000)
            except Exception:  # no commit / closed page
                pass
            u = self.page.url
        if self._restore_main_pending or u.startswith("chrome-error") or not domain_allowed(u, self.allowed_domains):
            self._restore_main_pending = False
            if not u.startswith("chrome-error") and not domain_allowed(u, self.allowed_domains):
                self._record_block(u, "main page left allowlist")
            ok = False
            if self._last_good_url:
                try:
                    self.page.goto(self._last_good_url)
                    ok = True
                except Exception:  # noqa: BLE001
                    pass
            if not ok:            # 回不到合法页面也不能停在白名单外 → about:blank（fail-closed）
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
        self._release_snapshot()
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        finally:
            self._pw = self._browser = self._ctx = self.page = self.active = None
            self._restore_main_pending = False
            self._nav_in_flight = 0
            self._last_focus_page = self._last_focus_frame = self._last_focus_handle = None
            self._last_focus_dom_id = None
            self._last_focus_secure = False

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
        self._release_snapshot()
        self._snapshot_id = uuid.uuid4().hex
        self._snapshot_page = pg
        items: list[dict] = []
        texts: list[str] = []
        for fi, frame in enumerate(pg.frames):
            try:
                off = (0.0, 0.0) if frame is pg.main_frame else self._frame_offset(frame)
                if off is None:
                    continue
                snap = frame.evaluate(SNAPSHOT_JS, [self.max_elements * 2, fi])
                self._snapshot_nodes[str(fi)] = (frame, frame.evaluate_handle("() => window.__guaSnapshotNodes"))
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

    def _probe_frame(self, frame, token: str, fid: int = 0) -> dict:
        return frame.evaluate(FOCUS_JS, [token, fid])

    @staticmethod
    def _frame_index(pg, frame) -> int:
        try:
            return list(pg.frames).index(frame)
        except ValueError:
            return 0

    def _probe_focus(self, pg) -> tuple[Optional[dict], str]:
        """安全焦点探测：返回 (焦点元素描述或 None, "known" | "none" | "unknown")。任何异常 → unknown。"""
        try:
            frame, off = pg.main_frame, (0.0, 0.0)
            for _ in range(10):
                token = f"f{time.time_ns()}"
                info = self._probe_frame(frame, token, self._frame_index(pg, frame))
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

    def _focus_handle(self, pg):
        """动作开始时真实焦点元素的句柄（穿透 open shadow root 与 iframe）。

        返回 (frame, ElementHandle)；非原生输入目标（可能是 closed shadow host）或没有焦点 → (None, None)。
        """
        try:
            frame = pg.main_frame
            for _ in range(10):
                token = f"h{time.time_ns()}"
                handle = frame.evaluate_handle(FOCUS_HANDLE_JS, token)
                el = handle.as_element()
                if el is not None:
                    return frame, el
                child = None
                for ch in frame.child_frames:
                    try:
                        if ch.frame_element().get_attribute("data-gua-focus-frame") == token:
                            child = ch
                            break
                    except Exception:  # noqa: BLE001
                        continue
                if child is None:
                    return None, None
                frame = child
            return None, None
        except Exception:  # noqa: BLE001
            return None, None

    def _remember_focus_handle(self, pg) -> None:
        """保存安全观察中的元素身份；句柄不会因页面刷新后的 DOM 编号复用而指向新元素。"""
        if self._last_focus_handle is not None:
            try:
                self._last_focus_handle.dispose()
            except Exception:  # 页面可能已经导航
                pass
        self._last_focus_page = pg
        self._last_focus_frame = self._last_focus_handle = None
        if self._last_focus_dom_id is None:
            return
        frame, handle = self._focus_handle(pg)
        if handle is None:
            return
        try:
            dom_id = handle.evaluate("(el, fid) => { " + _JS_HELPERS + " return domId(el, fid); }",
                                     self._frame_index(pg, frame))
            if dom_id == self._last_focus_dom_id:
                self._last_focus_frame, self._last_focus_handle = frame, handle
                return
        except Exception:  # 观察与绑定之间已换页面 / 移焦：后续输入必须拒绝
            pass
        try:
            handle.dispose()
        except Exception:  # 句柄所在文档可能已经导航
            pass

    @staticmethod
    def _handle_focused(handle) -> bool:
        """元素句柄现在还持有键盘焦点吗（open shadow root 内也算）。任何异常 → False（保守）。"""
        try:
            return bool(handle.evaluate(
                "(el) => { try { if (!el.isConnected) return false;"
                " const r = el.getRootNode ? el.getRootNode() : document;"
                " const a = (r && r.activeElement !== undefined) ? r.activeElement : document.activeElement;"
                " return a === el; } catch (e) { return false; } }"))
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _attach_dom_ids(elems: list, items: list) -> None:
        """把 raw 里的稳定 DOM 身份（dom_id / form_submit_id）传播到 UIElement.attrs
        （与 a11y 组 web_raws 的透传契约一致；这里再兜一次，保证提交目标身份可用）。"""
        by_gid = {(it.get("frame") or "0", it.get("gid")): it for it in items}
        for e in elems:
            it = by_gid.get((e.attrs.get("frame") or "0", e.attrs.get("gid")))
            if not it:
                continue
            if it.get("dom_id"):
                e.attrs["dom_id"] = it["dom_id"]
            if it.get("form_submit_id"):
                e.attrs["form_submit_id"] = it["form_submit_id"]

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
        for key in ("dom_id", "document_id", "form_submit_id"):     # 稳定 DOM 身份
            if info.get(key):
                match.attrs[key] = info[key]

    def observe(self, with_elements: bool = True) -> Observation:
        self._ensure()
        self._enforce()
        pg = self._alive_active()
        # Playwright's default caret hiding temporarily injects a stylesheet.
        # That is an observer-visible DOM mutation caused by the screenshot
        # itself. Preserve the caret so real page changes remain distinguishable.
        img = Image.open(io.BytesIO(pg.screenshot(type="png", caret="initial"))).convert("RGB")
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
                self._attach_dom_ids(elems, items)
            except Exception as e:  # 页面跳转中
                text = f"(snapshot failed: {type(e).__name__})"
            info, focus_state = self._probe_focus(pg)
            self._apply_focus(elems, info, img.size)
            self._last_focus_secure = bool(info.get("secure")) if isinstance(info, dict) else False
            self._last_focus_dom_id = info.get("dom_id") if isinstance(info, dict) else None
            self._remember_focus_handle(pg)
            self._bound_elements = {e.id: e for e in elems}
        wins = []
        for p in self._ctx.pages:
            try:
                wins.append(p.title() or p.url)
            except Exception:
                pass
        return Observation(screenshot=img, timestamp=time.time(), screen_size=img.size, dpi_scale=1.0,
                           active_window=title or url, active_process=urlparse(url).netloc or url[:40],
                           windows=wins, elements=elems, platform="web", url=url, text=text, cursor=self.cursor,
                           focus_state=focus_state, snapshot_id=self._snapshot_id if with_elements else "")

    def _stability(self, pg):
        states = []
        for frame in pg.frames:
            state = frame.evaluate(STABILITY_JS)
            states.append((frame, state))
        return states

    @staticmethod
    def _quiet(states, quiet_ms):
        return bool(states) and all(s["ready"] and not s["animating"] and s["quiet"] >= quiet_ms
                                    for _, s in states)

    @staticmethod
    def _same_state(before, after):
        return (len(before) == len(after) and all(f1 is f2 and s1["document"] == s2["document"]
                and s1["seq"] == s2["seq"] for (f1, s1), (f2, s2) in zip(before, after)))

    def wait_until_stable(self, timeout=5.0, interval=0.4, threshold=0.002, stable_frames=2):
        """Cheap DOM polling followed by pixel checks and a fresh full snapshot.

        Never reuse an old screenshot as completion evidence. DOM quiet alone is
        insufficient for canvas, CSS animations or changes while capturing.
        """
        from ..verify.diff import frame_diff
        self._ensure()
        deadline = time.monotonic() + max(0, timeout)
        quiet_ms = max(60, min(interval * 1000, 120))
        sample_ms = max(10, min(interval * 1000, 60))
        prev, calm = self.observe(False), 0
        pg = self._alive_active()
        while time.monotonic() < deadline:
            try:
                pg.wait_for_timeout(min(sample_ms, max(0, (deadline-time.monotonic()) * 1000)))
                if self._alive_active() is not pg:
                    pg, calm = self._alive_active(), 0
                    prev = self.observe(False)
                states = self._stability(pg)
                if not self._quiet(states, quiet_ms) or self._nav_in_flight or self._hops:
                    calm = 0
                    continue
                # The last candidate is already a full observation; do not
                # capture a redundant fourth image after two stable comparisons.
                full = calm >= max(1, stable_frames) - 1
                cur = self.observe(with_elements=full)
                after = self._stability(pg)
                if (self._alive_active() is pg and self._same_state(states, after)
                        and self._quiet(after, quiet_ms)
                        and frame_diff(prev.screenshot, cur.screenshot) < threshold):
                    calm += 1
                    if full and calm >= max(1, stable_frames):
                        return cur, True
                else:
                    calm = 0
                prev = cur
            except Exception:  # A frame can detach or navigate between probes.
                calm = 0
        return self.observe(), False

    def _check_bound_element(self, handle, element):
        info = handle.evaluate("(el, fid) => { " + _JS_HELPERS + """
          if (!el.isConnected) return null;
          return {tag:el.tagName.toLowerCase(), role:el.getAttribute('role') || '',
            type:el.getAttribute('type') || '', name:nameOf(el), secure:secureOf(el),
            disabled:!!el.disabled, href:el.getAttribute('href') || '',
            form_submit:submitOf(el), form_submit_id:submitId(el, fid),
            checked:['checkbox','radio'].includes(el.type) ? !!el.checked : null};
        }""", int(element.attrs.get("frame") or 0))
        if info is None:
            raise ValueError("original element detached")
        raw = web_raws([dict(info, rect=element.rect)])[0]
        name = re.sub(r"\s+", " ", raw["name"]).strip()[:100]
        if (not raw["enabled"] or name != element.name or raw["role"] != element.role
                or raw["is_password"] != element.is_password or raw["checked"] != element.checked
                or info["href"] != element.attrs.get("href", "")
                or info["form_submit"] != element.attrs.get("form_submit", "")
                or info["form_submit_id"] != element.attrs.get("form_submit_id", "")):
            raise ValueError("element semantics changed")

    def _resolve_bound(self, pg, a: Action):
        """观察时的原始 DOM 节点（不会用新快照里复用同一编号的节点代替）。"""
        element = self._bound_elements.get(a.element_id)
        if element is None:
            raise ValueError("element binding unavailable")
        frame, nodes = self._snapshot_nodes[element.attrs.get("frame") or "0"]
        handle = nodes.evaluate_handle("(nodes, id) => nodes.get(id)", element.attrs.get("dom_id")).as_element()
        if handle is None or frame not in pg.frames:
            raise ValueError("original element detached")
        return handle, element

    _JS_COVERED = """el => {
      const r = el.getBoundingClientRect();
      const x = r.left + r.width / 2, y = r.top + r.height / 2;
      if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) return false;
      let hit = document.elementFromPoint(x, y);
      while (hit && hit.shadowRoot && hit.shadowRoot.elementFromPoint) {
        const inner = hit.shadowRoot.elementFromPoint(x, y);
        if (!inner || inner === hit) break; hit = inner; }
      return !(hit === el || el.contains(hit) || (hit && hit.contains && hit.contains(el) && hit.tagName === 'LABEL'));
    }"""
    _JS_SET_VALUE = """(el, v) => {
      const tag = el.tagName;
      if (tag !== 'INPUT' && tag !== 'TEXTAREA') return 'unsupported';
      const proto = tag === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);
      el.dispatchEvent(new Event('input', {bubbles: true}));
      el.dispatchEvent(new Event('change', {bubbles: true}));
      return el.value === v ? 'ok' : 'mismatch';
    }"""

    def _bound_semantic(self, pg, a: Action, t0: float) -> ExecResult:
        """语义动作：DOM 方法 / 属性写入，不产生指针事件序列，也不移动 self.cursor。

        与前台路径相同的身份检查（同一原始节点、名称 / 角色 / 密码属性 / 勾选状态未变）；被其他元素遮挡时
        返回 background_unavailable（绝不“穿透”弹窗），由混合执行器改走 Playwright 真实点击（会被遮挡拦下）。
        """
        route = f"dom_semantic:{a.method}"
        handle = None
        try:
            handle, element = self._resolve_bound(pg, a)
            self._check_bound_element(handle, element)
            m = a.method
            if m in {"invoke", "toggle", "select", "expand", "collapse"}:
                if m == "toggle" and element.checked is None:
                    return ExecResult(False, "unsupported: element has no checked state", t0, time.time(), route=route)
                if handle.evaluate(self._JS_COVERED):
                    return ExecResult(False, "background_unavailable: target covered by another element", t0,
                                      time.time(), route=route)
                tag = handle.evaluate("el => el.tagName")
                if m == "select" and tag == "SELECT" and a.text:
                    handle.select_option(label=a.text, timeout=500)
                else:
                    handle.evaluate("el => el.click()")
            elif m == "set_value":
                if element.is_password:
                    return ExecResult(False, "blocked_by_safety: semantic set_value never writes password fields",
                                      t0, time.time(), route=route)
                res = handle.evaluate(self._JS_SET_VALUE, a.text or "")
                if res == "unsupported":
                    return ExecResult(False, "background_unavailable: not a plain input/textarea", t0, time.time(),
                                      route=route)
                if res != "ok":
                    return ExecResult(False, "native_action_error: value not applied; observe again", t0,
                                      time.time(), route=route)
            elif m == "focus":
                handle.focus()
            elif m == "scroll_into_view":
                handle.scroll_into_view_if_needed(timeout=800)
            else:
                return ExecResult(False, f"unsupported: {m}", t0, time.time(), route=route)
            return ExecResult(True, "", t0, time.time(), route=route)
        except Exception as exc:  # noqa: BLE001
            return ExecResult(False, f"stale_target: bound element unavailable ({type(exc).__name__}); observe again",
                              t0, time.time(), route=route)
        finally:
            if handle is not None:
                try:
                    handle.dispose()
                except Exception:
                    pass

    def _bound_click(self, pg, a: Action, t0: float) -> ExecResult:
        """Use the original node, never a new node that reuses its candidate ID."""
        element = self._bound_elements.get(a.element_id)
        handle = None
        try:
            if element is None:
                raise ValueError("element binding unavailable")
            frame, nodes = self._snapshot_nodes[element.attrs.get("frame") or "0"]
            handle = nodes.evaluate_handle("(nodes, id) => nodes.get(id)", element.attrs.get("dom_id")).as_element()
            if handle is None or frame not in pg.frames:
                raise ValueError("original element detached")
            self._check_bound_element(handle, element)
            box = handle.bounding_box()
            if box is None:
                raise ValueError("original element hidden")
            x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            if not 0 <= x < self.viewport[0] or not 0 <= y < self.viewport[1]:
                return ExecResult(False, "out_of_bounds: bound element is offscreen", t0, time.time())
            # Playwright performs live visibility, geometry and hit-target checks.
            # No force=True or JS click: overlays and trusted-input behavior matter.
            handle.hover(timeout=350)
            self._check_bound_element(handle, element)  # Hover handlers can replace or relabel a target.
            handle.click(timeout=350, no_wait_after=True)
            self.cursor = (int(x), int(y))
            return ExecResult(True, "", t0, time.time(), route="browser_element")
        except Exception as exc:
            return ExecResult(False, f"stale_target: bound element unavailable ({type(exc).__name__}); observe again",
                              t0, time.time(), route="browser_element")
        finally:
            if handle is not None:
                try:
                    handle.dispose()
                except Exception:
                    pass

    # ---------------------------------------------------------------- 执行
    def _domain_ok(self, url: str) -> bool:
        return domain_allowed(url, self.allowed_domains)

    def execute(self, a: Action) -> ExecResult:
        try:
            a.validate()        # 安全组会把 hotkey 等收窄；直接调用 Env 也不能绕过
        except Exception as e:  # noqa: BLE001
            return ExecResult(False, f"invalid_action: {type(e).__name__}: {str(e)[:160]}",
                              time.time(), time.time())
        r = self._execute(a)
        if r.route != "browser_element" and a.x is not None and a.y is not None and a.is_pointer:
            self.cursor = (int(a.x2), int(a.y2)) if a.type == "drag" and a.x2 is not None else (int(a.x), int(a.y))
        if self.allowed_domains:
            try:
                self._alive_active().wait_for_timeout(60)   # 让点击触发的导航 / 弹窗事件到达
            except Exception:
                pass
            deadline = time.monotonic() + self.fetch_timeout + 1.0
            while (self._nav_in_flight or self._hops) and not self._restore_main_pending \
                    and time.monotonic() < deadline:
                try:
                    self._alive_active().wait_for_timeout(20)
                except Exception:
                    break
            self._enforce()
            if self._unreported:
                msg = "; ".join(self._unreported)
                self._unreported = []
                return ExecResult(False, f"blocked_by_safety: off-allowlist navigation blocked: {msg}"[:400],
                                  r.started, time.time())
        return r

    def _do_type(self, pg, a: Action, t0: float) -> ExecResult:
        """clear / type：核对安全观察中的元素身份，再绑定实际焦点（穿透 open shadow / iframe），
        clear 与每个输入阶段都复查焦点仍是它；焦点改变 / 目标消失 → 发送秘密前返回 blocked_by_safety，
        绝不把焦点拉回去替用户执行，也不整串无检查地交给全局 keyboard。"""
        frame, handle = self._focus_handle(pg)
        if handle is None:
            return ExecResult(False, "blocked_by_safety: keyboard focus target could not be bound",
                              t0, time.time())
        if self._last_focus_dom_id is not None:
            try:
                same_target = (pg is self._last_focus_page and frame is self._last_focus_frame
                               and self._last_focus_handle is not None
                               and self._last_focus_handle.evaluate(
                                   "(expected, actual) => expected.isConnected && expected === actual", handle))
                if not same_target:
                    return ExecResult(False, "blocked_by_safety: focus target changed since observation",
                                      t0, time.time())
                is_secure = bool(handle.evaluate("el => { " + _JS_HELPERS + " return secureOf(el); }"))
                if is_secure != self._last_focus_secure:
                    return ExecResult(False, "blocked_by_safety: focus target security changed since observation",
                                      t0, time.time())
            except Exception:
                return ExecResult(False, "blocked_by_safety: focus target security could not be verified",
                                  t0, time.time())
        kb = pg.keyboard
        try:
            if not self._handle_focused(handle):
                return ExecResult(False, "blocked_by_safety: focus changed before typing", t0, time.time())
            if a.clear:
                kb.press("Control+A")
                if not self._handle_focused(handle):
                    return ExecResult(False, "blocked_by_safety: focus changed during clear", t0, time.time())
                kb.press("Backspace")
                if not self._handle_focused(handle):
                    return ExecResult(False, "blocked_by_safety: focus changed during clear", t0, time.time())
            text = a.text or ""
            for i in range(0, len(text), _TYPE_CHUNK):
                if not self._handle_focused(handle):
                    return ExecResult(False, "blocked_by_safety: focus changed while typing", t0, time.time())
                kb.type(text[i:i + _TYPE_CHUNK], delay=5)
                if not self._handle_focused(handle):
                    return ExecResult(False, "blocked_by_safety: focus changed while typing", t0, time.time())
            if a.submit:
                if not self._handle_focused(handle):
                    return ExecResult(False, "blocked_by_safety: focus changed before submit", t0, time.time())
                kb.press("Enter")
            return ExecResult(True, "", t0, time.time(), route="browser_type")
        except Exception as e:  # noqa: BLE001
            return ExecResult(False, f"{type(e).__name__}: {str(e)[:200]}", t0, time.time())

    def _execute(self, a: Action) -> ExecResult:
        self._ensure()
        t0 = time.time()
        if not self.supports(a.type):
            return ExecResult(False, f"unsupported action {a.type} on web", t0, time.time())
        pg = self._alive_active()
        if a.binding and a.type not in {"navigate", "back", "focus_window", "wait"}:
            if a.binding.get("snapshot_id") != self._snapshot_id or pg is not self._snapshot_page:
                return ExecResult(False, "stale_target: page changed; observe again", t0, time.time())
        w, h = self.viewport
        err = self._bounds_error(a, w, h)
        if err:
            return ExecResult(False, err, t0, time.time())
        if a.type == "type":
            return self._do_type(pg, a, t0)
        if a.type == "invoke":
            if not a.binding or a.element_id is None:
                return ExecResult(False, "stale_target: semantic action needs an observed element", t0, time.time())
            return self._bound_semantic(pg, a, t0)
        if a.binding and a.type == "click" and a.element_id is not None:
            return self._bound_click(pg, a, t0)
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
                try:
                    url = normalize(url)
                except UrlRejected as e:
                    return ExecResult(False, f"blocked_by_safety invalid url: {e}", t0, time.time())
                if not self._domain_ok(url):
                    return ExecResult(False, f"blocked_by_safety domain not allowed: {url}", t0, time.time())
                pg.goto(url)
            elif a.type == "back":
                pg.go_back()
            elif a.type == "focus_window":
                if not self.focus_window(a.text or ""):
                    return ExecResult(False, f"window_not_found {a.text!r}", t0, time.time())
            return ExecResult(True, "", t0, time.time(), route="browser_input")
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

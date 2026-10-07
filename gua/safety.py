"""安全守卫：危险动作确认闸门 + 域名白名单 + ask_user 通道。

参考：
- OpenAI CUA 的 pending_safety_checks / 敏感操作需人工确认；OpenAI CUA sample app README 的
  “生成的代码以用户权限运行、请在受控环境中使用”
- Anthropic computer-use 文档：在隔离 VM/容器中运行、对外部可见/不可逆操作要求人类确认
- browser-use 的 sensitive_data / allowed_domains
- UI-TARS 的 call_user()、AndroidWorld 的 answer/status 动作

决策三档：allow（直接执行）/ confirm（交给确认回调，默认 CLI 询问）/ deny（拒绝，返回 blocked_by_safety）。
规则是保守的关键词 + 模式匹配，**不是**完备的安全方案；真实部署仍应在虚拟机/沙箱里跑。

v0.3：
- 拒绝是终止性的（审查条目 1）：人工拒绝 / deny 过的动作记下签名，之后同一动作直接拒绝、不再询问、不再执行；
  GUIAgent 把恢复动作也送进这里。
- 按键先规范化（gua.keys：del/delete、ctrl/control、cmd/win/super/meta…），危险组合按“子集”匹配；
  key_down 按住的键会累计到之后的 hotkey / key_down / type（审查条目 8）。typed text 含控制字符也要确认。
- 密码框按统一元素模型的 is_password 判断（各平台无障碍属性），名字匹配只作补充（审查条目 11）。
- 域名白名单按主机名匹配（gua.urlpolicy，与 WebEnv 的浏览器层拦截共用）；可选 allowed_apps 白名单（审查条目 7）。

v0.3.1（第二轮审查）：
- 激活目标语义检查（条目 1）：对每个动作求“实际被激活的目标”——指针动作的目标元素；Enter / Space /
  DPAD_CENTER（hotkey、key_down）、type(submit=True)、文字里含换行、往按钮里打字 → 当前焦点元素
  （文本框则取它所在表单的提交按钮 attrs["form_submit"]）。同一套危险目标规则；拒绝签名按目标记录
  （activate|<目标名>），所以点击被拒后用回车 / 空格 / 提交去激活同一按钮也直接拒绝、不再询问。
  焦点无法确定（focus_state=unknown、没有观察、或者同一观察上之前已经执行过可能移动焦点的动作）
  或平台没有报告焦点时按激活键 → 保守确认。
- 敏感输入统一摘要（条目 2）：日志、重复拒绝日志、确认回调 / 终端一律用 gua.sensitive 的安全摘要，
  脱敏只由输入目标决定；拒绝签名里的 typed text 只存哈希。
- assess 本身抛异常 → 按“需要确认”处理（fail-closed），deny 模式下即拒绝。
- drag 的终点落在废纸篓 / 回收站 / 危险目标上也要确认。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Optional

from .actions import Action
from .env.base import Observation, UIElement
from .keys import ACTIVATION_KEYS, MODIFIERS, canonical_set
from .sensitive import focus_target, is_password_el, is_sensitive_type, redacted_action, safe_short
from .urlpolicy import domain_allowed, host_of

# 点击/确认这些语义的元素属于“不可逆或对外可见”的操作
RISKY_WORDS = [
    "delete", "remove", "erase", "format", "uninstall", "reset", "factory", "wipe", "purge",
    "pay", "payment", "purchase", "buy now", "place order", "checkout", "transfer", "send money",
    "send", "post", "publish", "submit order", "confirm payment", "sign out everywhere", "revoke",
    "删除", "移除", "清空", "格式化", "卸载", "重置", "支付", "付款", "购买", "下单", "转账", "发送", "发布",
    "确认付款", "注销",
]
DANGEROUS_TEXT = [
    r"\brm\s+-[a-z]*r[a-z]*f", r"\brm\s+-[a-z]*f[a-z]*r", r"\bmkfs\b", r"\bdd\s+if=", r"\bformat\s+[a-z]:",
    r"\bdel\s+/[sq]", r"\brd\s+/s", r"\bshutdown\b", r"\breboot\b", r":\(\)\s*\{\s*:\|:&\s*\};:",
    r"\bdrop\s+(table|database)\b", r"\btruncate\s+table\b", r"curl[^|]*\|\s*(sudo\s+)?(ba)?sh",
    r"\bchmod\s+-R\s+777\s+/", r"Remove-Item\s+.*-Recurse",
]
# 规范名（gua.keys）；按下的键集合只要**包含**其中任一组合就需要确认（ctrl+shift+alt+delete 也算）
DANGEROUS_HOTKEYS = [frozenset(s) for s in (
    {"alt", "f4"},                  # 关闭窗口 / 应用（Windows / Linux）
    {"meta", "q"},                  # 退出应用（macOS）；meta+shift+q 注销也被覆盖
    {"ctrl", "alt", "delete"}, {"ctrl", "shift", "delete"}, {"ctrl", "alt", "backspace"},
    {"meta", "alt", "esc"},         # 强制退出（macOS）
    {"shift", "delete"},            # 永久删除（Windows 资源管理器）
    {"meta", "delete"}, {"meta", "backspace"},   # 移到废纸篓（macOS Finder）
    {"ctrl", "meta", "q"},          # 锁屏（macOS）
)]
# drag 终点（拖进废纸篓 / 回收站 = 删除）
TRASH_WORDS = ["trash", "bin", "recycle bin", "wastebasket", "废纸篓", "回收站", "垃圾桶"]
_ALLOWED_CTRL_CHARS = {"\n", "\t", "\r"}
_TEXT_ENTRY_ROLES = {"textbox", "combobox"}
# 执行后可能移动键盘焦点的动作：之后若仍用同一个观察判断焦点，焦点视为未知
_FOCUS_STABLE_ACTIONS = {"wait", "scroll", "move", "key_up", "ask_user", "done", "fail"}
_APP_BAD = re.compile(r"[&|;<>^%$`\"'\n\r\x00*?!]")


@dataclass
class Decision:
    verdict: str            # allow | confirm | deny
    reason: str = ""
    sigs: list = field(default_factory=list)   # 命中规则对应的拒绝签名（拒绝时全部记下）


def _contains_word(text: str, word: str) -> bool:
    if re.search(r"[\u4e00-\u9fff]", word):
        return word in text
    return re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", text) is not None


def _h(text: Optional[str]) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


@dataclass
class SafetyGuard:
    enabled: bool = True
    mode: str = "confirm"                      # confirm（询问）| deny（遇到危险一律拒绝）| allow（全部放行，仅测试）
    allowed_domains: list[str] = field(default_factory=list)
    extra_risky_words: list[str] = field(default_factory=list)
    confirm_fn: Optional[Callable[[Action, str], bool]] = None
    ask_fn: Optional[Callable[[str], Optional[str]]] = None
    allowed_apps: list[str] = field(default_factory=list)   # 非空时 open_app 只允许这些应用名 / 包名
    log: list[dict] = field(default_factory=list)
    denied: dict[str, str] = field(default_factory=dict)    # 动作签名 → 被拒绝的原因（终止性）
    held: set = field(default_factory=set)                  # key_down 按住、尚未 key_up 的规范键名
    _stale_obs: Optional[Observation] = field(default=None, repr=False)   # 已执行过移焦动作的观察

    # ---------------------------------------------------------------- 焦点 / 激活目标
    def focus(self, obs: Optional[Observation]) -> tuple[Optional[UIElement], str]:
        """焦点元素与状态；同一个观察上已经执行过可能移动焦点的动作 → unknown。"""
        if obs is not None and obs is self._stale_obs:
            return None, "unknown"
        return focus_target(obs)

    def _activation(self, a: Action, obs: Optional[Observation]) -> Optional[str]:
        """动作是否会“激活”某个目标：pointer（点到的元素）| key（焦点元素）| submit（焦点 / 其表单）。"""
        if a.type in {"click", "double_click", "long_press"}:
            return "pointer"
        if a.type in {"hotkey", "key_down"}:
            ks = canonical_set(a.keys) | frozenset(self.held)
            if ks & ACTIVATION_KEYS:
                f, _ = self.focus(obs)
                if f is not None and f.role in _TEXT_ENTRY_ROLES and not (ks & {"enter", "66", "160"}):
                    return None              # 文本框里按空格 = 输入空格，不是激活
                return "submit" if f is not None and f.role in _TEXT_ENTRY_ROLES else "key"
            return None
        if a.type == "type":
            text = a.text or ""
            if a.submit or "\n" in text or "\r" in text:
                return "submit"
            f, st = self.focus(obs)
            if text and f is not None and f.role not in _TEXT_ENTRY_ROLES and not is_password_el(f):
                return "key"                 # 往按钮 / 链接里“打字”只会触发空格 / 回车激活
        return None

    def activation_target(self, a: Action, obs: Optional[Observation]) -> tuple[Optional[str], str, str]:
        """返回 (激活方式或 None, 目标名, 焦点状态)。目标名为空表示无法确定。"""
        kind = self._activation(a, obs)
        if kind is None:
            return None, "", ""
        if kind == "pointer":
            return kind, (self._element_name(a, obs) or a.target or "").strip(), "known"
        f, st = self.focus(obs)
        if f is None:
            return kind, "", st
        label = f.name or ""
        if kind == "submit" and f.role in _TEXT_ENTRY_ROLES:
            label = " ".join(filter(None, [f.attrs.get("form_submit", ""), label]))
        return kind, label.strip(), st

    def _activation_sig(self, a: Action, obs: Optional[Observation]) -> Optional[str]:
        kind, label, st = self.activation_target(a, obs)
        if kind is None:
            return None
        if label:
            return f"activate|{label.lower()}"
        if kind == "pointer":
            return f"{a.type}|{a.point}"
        return f"activate|?{st}|{(obs.active_window if obs else '')}"

    # ---------------------------------------------------------------- 判定
    def _combo(self, a: Action) -> frozenset:
        return canonical_set(a.keys) | frozenset(self.held)

    def _risky_word(self, label: str, extra: tuple = ()) -> Optional[str]:
        label = (label or "").lower()
        if not label:
            return None
        for w in RISKY_WORDS + [w.lower() for w in self.extra_risky_words] + list(extra):
            if _contains_word(label, w.lower()):
                return w
        return None

    def assess(self, a: Action, obs: Optional[Observation] = None) -> Decision:
        if not self.enabled:
            return Decision("allow")
        hits: list[tuple[str, str]] = []          # (reason, signature)
        base = self.base_signature(a)
        if a.type == "navigate":
            url = a.url or a.text or ""
            if self.allowed_domains and not domain_allowed(url, self.allowed_domains):
                return Decision("deny", f"domain {host_of(url)!r} not in allowlist {self.allowed_domains}", [base])
        if a.type == "open_app":
            app = (a.app or a.text or "").strip()
            if not app or _APP_BAD.search(app) or app.startswith("-"):
                return Decision("deny", f"invalid app name {app!r} (shell metacharacters / option-like)", [base])
            if self.allowed_apps and app.lower() not in {x.lower() for x in self.allowed_apps}:
                return Decision("deny", f"app {app!r} not in allowed_apps {self.allowed_apps}", [base])
        f, fstate = self.focus(obs)
        sensitive = is_sensitive_type(a, obs, fstate)
        if a.type == "type" and a.text:
            ctrl = sorted({repr(c) for c in a.text if unicodedata.category(c) == "Cc" and c not in _ALLOWED_CTRL_CHARS})
            if ctrl:
                hits.append(("typed text contains control characters" +
                             ("" if sensitive else f" {', '.join(ctrl)}"), base))
            for pat in DANGEROUS_TEXT:
                if re.search(pat, a.text, re.I):
                    hits.append((f"typed text matches destructive pattern {pat!r}", base))
                    break
            if fstate == "password":
                hits.append(("typing into a password field", base))
            elif fstate == "unknown":
                hits.append(("typing while the keyboard focus target cannot be determined "
                             "(treated as a possible password field)", base))
            mods = (set(self.held) & MODIFIERS) - {"shift"}
            if mods:      # 按住 cmd/ctrl/alt 时打字 = 一串快捷键
                for c in a.text:
                    combo = frozenset(mods | canonical_set([c]))
                    if any(h <= combo for h in DANGEROUS_HOTKEYS):
                        hits.append((f"typing {'a character' if sensitive else repr(c)} while holding "
                                     f"{'+'.join(sorted(mods))}", base))
                        break
        if a.type in {"hotkey", "key_down"}:
            combo = self._combo(a)
            hit = next((h for h in DANGEROUS_HOTKEYS if h <= combo), None)
            if hit:
                held = f" (holding {'+'.join(sorted(self.held))})" if self.held else ""
                hits.append((f"key combination {'+'.join(sorted(combo))}{held} contains "
                             f"{'+'.join(sorted(hit))}: may close apps or delete data", base))
            elif sensitive:
                hits.append(("key presses into a password / unknown input target", base))
        # 激活目标（指针、Enter/Space、提交、往按钮里打字）——同一条危险目标规则
        kind, label, st = self.activation_target(a, obs)
        if kind is not None:
            asig = self._activation_sig(a, obs)
            risk_label = label
            if kind == "pointer":       # 指针：元素名 + 模型给的 target 描述都要看
                risk_label = " ".join(filter(None, [self._element_name(a, obs), a.target or ""]))
            w = self._risky_word(risk_label)
            if w:
                label = risk_label
                how = "clicking" if kind == "pointer" else "keyboard activation of" if kind == "key" else "submitting"
                hits.append((f"{how} target {label[:60].lower()!r} looks irreversible/externally visible ({w})", asig))
            elif kind != "pointer" and not label and st in {"unknown", "unreported"}:
                hits.append((f"activation key with an undeterminable focus target ({st}); cannot check what it "
                             f"activates", asig))
        if a.type == "drag":
            end = self._element_name(Action("click", x=a.x2, y=a.y2) if a.x2 is not None else Action("wait"), obs)
            label2 = " ".join(filter(None, [end, a.target2 or ""]))
            w = self._risky_word(label2, tuple(TRASH_WORDS))
            if w:
                hits.append((f"dragging onto {label2[:60].lower()!r} may delete / send data ({w})",
                             f"drag-to|{label2.lower()}"))
        if hits:
            return Decision("confirm", "; ".join(dict.fromkeys(r for r, _ in hits)),
                            list(dict.fromkeys(s for _, s in hits)))
        return Decision("allow")

    @staticmethod
    def _element_name(a: Action, obs: Optional[Observation]) -> str:
        if obs is None:
            return ""
        if a.element_id is not None:
            e = obs.element(a.element_id)
            if e:
                return e.name
        if a.point:
            e = obs.element_at(*a.point)
            if e:
                return e.name
        return ""

    def base_signature(self, a: Action) -> str:
        if a.type in {"hotkey", "key_down"}:
            return f"keys|{'+'.join(sorted(self._combo(a)))}"
        if a.type == "type":
            return f"type|{_h(a.text)}|{a.submit}"           # 只存哈希，不存明文
        if a.type == "navigate":
            return f"navigate|{a.url or a.text}"
        if a.type == "open_app":
            return f"open_app|{(a.app or a.text or '').lower()}"
        if a.is_pointer:
            return f"{a.type}|{a.target or ''}|{a.point}"
        return f"{a.type}|{_h(json.dumps(a.to_dict(), sort_keys=True))}"

    def signatures(self, a: Action, obs: Optional[Observation] = None) -> list[str]:
        """一个动作的全部意图签名：动作本身 + 它激活的目标（同一目标的不同激活方式共享）。"""
        sigs = [self.base_signature(a)]
        asig = self._activation_sig(a, obs)
        if asig:
            sigs.insert(0, asig)
        return sigs

    def signature(self, a: Action, obs: Optional[Observation] = None) -> str:
        """兼容 v0.3：主签名（有激活目标时就是目标签名）。"""
        return self.signatures(a, obs)[0]

    def remember_denial(self, a: Action, obs: Optional[Observation], reason: str) -> None:
        self.denied[self.signature(a, obs)] = reason

    def summary(self, a: Action, obs: Optional[Observation] = None) -> str:
        """安全摘要（日志 / 终端 / 提示词）；考虑“同一观察上焦点已移动”的情况。"""
        return safe_short(a, obs, self.focus(obs)[1])

    # ---------------------------------------------------------------- 闸门
    def gate(self, a: Action, obs: Optional[Observation] = None) -> tuple[bool, str]:
        """返回 (是否放行, 原因)。被拒绝过的同一动作 / 同一激活目标直接拒绝（不再询问）。"""
        if not self.enabled:
            self._track_keys(a)
            return True, ""
        err = ""
        try:
            fstate = self.focus(obs)[1]
            shown = safe_short(a, obs, fstate)
            sigs = self.signatures(a, obs)
        except Exception as e:  # noqa: BLE001  — 摘要都算不出来：保守地完全脱敏、按危险处理
            fstate, err = "unknown", type(e).__name__
            shown, sigs = json.dumps({"type": a.type, "redacted": True}), [f"error|{a.type}"]
        hit = next((s for s in sigs if s in self.denied), None)
        if hit:
            why = f"previously rejected, not retried: {self.denied[hit]}"
            self.log.append({"action": shown, "decision": "deny", "reason": why, "approved": False,
                             "repeat": True})
            return False, why
        d = None
        if not err:
            try:
                d = self.assess(a, obs)
            except Exception as e:  # noqa: BLE001
                err = type(e).__name__
        if d is None:                 # fail-closed：判定出错 = 需要确认（deny 模式即拒绝）
            d = Decision("confirm", f"safety check error ({err}); treated as dangerous", sigs[:1])
        approved = d.verdict == "allow"
        if d.verdict == "confirm":
            if self.mode == "allow":
                approved = True
            elif self.mode == "deny":
                approved = False
            else:
                fn = self.confirm_fn or cli_confirm
                approved = bool(fn(redacted_action(a, obs, fstate), d.reason))
        self.log.append({"action": shown, "decision": d.verdict, "reason": d.reason, "approved": approved})
        if approved:
            self._track_keys(a)
            if a.type not in _FOCUS_STABLE_ACTIONS and not (a.type == "type" and not a.submit
                                                             and "\n" not in (a.text or "")):
                self._stale_obs = obs
        else:
            for s in (d.sigs or sigs[:1]):
                self.denied[s] = d.reason or "rejected"
        return approved, d.reason

    def _track_keys(self, a: Action) -> None:
        if a.type == "key_down":
            self.held |= set(canonical_set(a.keys))
        elif a.type == "key_up":
            self.held -= set(canonical_set(a.keys))

    def ask(self, question: str) -> Optional[str]:
        fn = self.ask_fn or cli_ask
        return fn(question)


def cli_confirm(a: Action, reason: str) -> bool:
    """终端确认。收到的 a 已经是安全摘要版本（SafetyGuard.gate 传入 redacted_action）；这里再按摘要打印一次。"""
    shown = safe_short(a, None)      # 没有观察 = 输入目标未知 → typed text 一律脱敏
    if not sys.stdin or not sys.stdin.isatty():
        print(f"[safety] 非交互环境，拒绝危险动作: {shown} ({reason})", file=sys.stderr)
        return False
    ans = input(f"\n[safety] 需要确认的动作: {shown}\n  原因: {reason}\n  执行吗? [y/N] ").strip().lower()
    return ans in {"y", "yes", "是"}


def cli_ask(question: str) -> Optional[str]:
    if not sys.stdin or not sys.stdin.isatty():
        return None
    ans = input(f"\n[agent 提问] {question}\n  你的回答（回车跳过）: ").strip()
    return ans or None

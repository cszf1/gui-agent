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
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Optional

from .actions import Action
from .env.base import Observation
from .keys import MODIFIERS, canonical_set
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
_ALLOWED_CTRL_CHARS = {"\n", "\t", "\r"}
_APP_BAD = re.compile(r"[&|;<>^%$`\"'\n\r\x00*?!]")


@dataclass
class Decision:
    verdict: str            # allow | confirm | deny
    reason: str = ""


def _contains_word(text: str, word: str) -> bool:
    if re.search(r"[\u4e00-\u9fff]", word):
        return word in text
    return re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", text) is not None


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

    # ---------------------------------------------------------------- 判定
    def _combo(self, a: Action) -> frozenset:
        return canonical_set(a.keys) | frozenset(self.held)

    def assess(self, a: Action, obs: Optional[Observation] = None) -> Decision:
        if not self.enabled:
            return Decision("allow")
        if a.type == "navigate":
            url = a.url or a.text or ""
            if self.allowed_domains and not domain_allowed(url, self.allowed_domains):
                return Decision("deny", f"domain {host_of(url)!r} not in allowlist {self.allowed_domains}")
        if a.type == "open_app":
            app = (a.app or a.text or "").strip()
            if not app or _APP_BAD.search(app) or app.startswith("-"):
                return Decision("deny", f"invalid app name {app!r} (shell metacharacters / option-like)")
            if self.allowed_apps and app.lower() not in {x.lower() for x in self.allowed_apps}:
                return Decision("deny", f"app {app!r} not in allowed_apps {self.allowed_apps}")
        if a.type == "type" and a.text:
            ctrl = sorted({repr(c) for c in a.text if unicodedata.category(c) == "Cc" and c not in _ALLOWED_CTRL_CHARS})
            if ctrl:
                return Decision("confirm", f"typed text contains control characters {', '.join(ctrl)}")
            for pat in DANGEROUS_TEXT:
                if re.search(pat, a.text, re.I):
                    return Decision("confirm", f"typed text matches destructive pattern {pat!r}")
            if obs is not None:
                f = next((e for e in obs.elements if e.focused), None)
                if f is not None and (getattr(f, "is_password", False) or f.attrs.get("password") == "true"
                                      or f.attrs.get("type") == "password"
                                      or "password" in f.name.lower() or "密码" in f.name):
                    return Decision("confirm", "typing into a password field")
            mods = (set(self.held) & MODIFIERS) - {"shift"}
            if mods:      # 按住 cmd/ctrl/alt 时打字 = 一串快捷键
                for c in a.text:
                    combo = frozenset(mods | canonical_set([c]))
                    if any(h <= combo for h in DANGEROUS_HOTKEYS):
                        return Decision("confirm", f"typing {c!r} while holding {'+'.join(sorted(mods))}")
        if a.type in {"hotkey", "key_down"}:
            combo = self._combo(a)
            hit = next((h for h in DANGEROUS_HOTKEYS if h <= combo), None)
            if hit:
                held = f" (holding {'+'.join(sorted(self.held))})" if self.held else ""
                return Decision("confirm", f"key combination {'+'.join(sorted(combo))}{held} contains "
                                           f"{'+'.join(sorted(hit))}: may close apps or delete data")
        if a.is_pointer or a.type == "type" and a.submit:
            label = " ".join(filter(None, [a.target, self._element_name(a, obs)])).lower()
            for w in RISKY_WORDS + [w.lower() for w in self.extra_risky_words]:
                if label and _contains_word(label, w.lower()):
                    return Decision("confirm", f"target {label[:60]!r} looks irreversible/externally visible ({w})")
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

    def signature(self, a: Action, obs: Optional[Observation] = None) -> str:
        """同一个“动作意图”的签名：用于让拒绝成为终止性的（换个坐标点同一个按钮也算同一动作）。"""
        if a.is_pointer:
            label = (self._element_name(a, obs) or a.target or "").strip().lower()
            return f"{a.type}|{label}" if label else f"{a.type}|{a.point}"
        if a.type in {"hotkey", "key_down"}:
            return f"keys|{'+'.join(sorted(self._combo(a)))}"
        if a.type == "type":
            return f"type|{a.text}|{a.submit}"
        if a.type == "navigate":
            return f"navigate|{a.url or a.text}"
        if a.type == "open_app":
            return f"open_app|{(a.app or a.text or '').lower()}"
        return a.short()

    def remember_denial(self, a: Action, obs: Optional[Observation], reason: str) -> None:
        self.denied[self.signature(a, obs)] = reason

    # ---------------------------------------------------------------- 闸门
    def gate(self, a: Action, obs: Optional[Observation] = None) -> tuple[bool, str]:
        """返回 (是否放行, 原因)。被拒绝过的同一动作直接拒绝（不再询问）。"""
        if not self.enabled:
            self._track_keys(a)
            return True, ""
        sig = self.signature(a, obs)
        if sig in self.denied:
            why = f"previously rejected, not retried: {self.denied[sig]}"
            self.log.append({"action": a.short(), "decision": "deny", "reason": why, "approved": False,
                             "repeat": True})
            return False, why
        d = self.assess(a, obs)
        approved = d.verdict == "allow"
        if d.verdict == "confirm":
            if self.mode == "allow":
                approved = True
            elif self.mode == "deny":
                approved = False
            else:
                fn = self.confirm_fn or cli_confirm
                approved = bool(fn(a, d.reason))
        shown = a.short()
        if a.type == "type" and "password" in d.reason:
            shown = json.dumps({**a.to_dict(), "text": "***"}, ensure_ascii=False)
        self.log.append({"action": shown, "decision": d.verdict, "reason": d.reason, "approved": approved})
        if approved:
            self._track_keys(a)
        else:
            self.denied[sig] = d.reason or "rejected"
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
    if not sys.stdin or not sys.stdin.isatty():
        print(f"[safety] 非交互环境，拒绝危险动作: {a.short()} ({reason})", file=sys.stderr)
        return False
    ans = input(f"\n[safety] 需要确认的动作: {a.short()}\n  原因: {reason}\n  执行吗? [y/N] ").strip().lower()
    return ans in {"y", "yes", "是"}


def cli_ask(question: str) -> Optional[str]:
    if not sys.stdin or not sys.stdin.isatty():
        return None
    ans = input(f"\n[agent 提问] {question}\n  你的回答（回车跳过）: ").strip()
    return ans or None

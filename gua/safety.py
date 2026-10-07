"""安全守卫：危险动作确认闸门 + 域名白名单 + ask_user 通道。

参考：
- OpenAI CUA 的 pending_safety_checks / 敏感操作需人工确认；OpenAI CUA sample app README 的
  “生成的代码以用户权限运行、请在受控环境中使用”
- Anthropic computer-use 文档：在隔离 VM/容器中运行、对外部可见/不可逆操作要求人类确认
- browser-use 的 sensitive_data / allowed_domains
- UI-TARS 的 call_user()、AndroidWorld 的 answer/status 动作

决策三档：allow（直接执行）/ confirm（交给确认回调，默认 CLI 询问）/ deny（拒绝，返回 blocked_by_safety）。
规则是保守的关键词 + 模式匹配，**不是**完备的安全方案；真实部署仍应在虚拟机/沙箱里跑。
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urlparse

from .actions import Action
from .env.base import Observation

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
DANGEROUS_HOTKEYS = [{"alt", "f4"}, {"cmd", "q"}, {"ctrl", "alt", "delete"}, {"ctrl", "shift", "delete"},
                     {"cmd", "option", "esc"}, {"shift", "delete"}, {"cmd", "delete"}]


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
    log: list[dict] = field(default_factory=list)

    # ---------------------------------------------------------------- 判定
    def assess(self, a: Action, obs: Optional[Observation] = None) -> Decision:
        if not self.enabled:
            return Decision("allow")
        if a.type == "navigate":
            url = a.url or a.text or ""
            u = urlparse(url if "://" in url else "https://" + url)
            if self.allowed_domains and u.scheme not in {"file", "about", "data"}:
                ok = any(u.netloc == d or u.netloc.endswith("." + d) for d in self.allowed_domains)
                if not ok:
                    return Decision("deny", f"domain {u.netloc!r} not in allowlist {self.allowed_domains}")
        if a.type == "type" and a.text:
            for pat in DANGEROUS_TEXT:
                if re.search(pat, a.text, re.I):
                    return Decision("confirm", f"typed text matches destructive pattern {pat!r}")
            if obs is not None:
                f = next((e for e in obs.elements if e.focused), None)
                if f is not None and (f.attrs.get("password") == "true" or f.attrs.get("type") == "password"
                                      or "password" in f.name.lower() or "密码" in f.name):
                    return Decision("confirm", "typing into a password field")
        if a.type == "hotkey":
            ks = {k.lower() for k in a.keys}
            if any(ks == h for h in DANGEROUS_HOTKEYS):
                return Decision("confirm", f"hotkey {'+'.join(a.keys)} may close apps or delete data")
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

    # ---------------------------------------------------------------- 闸门
    def gate(self, a: Action, obs: Optional[Observation] = None) -> tuple[bool, str]:
        """返回 (是否放行, 原因)。"""
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
        self.log.append({"action": a.short(), "decision": d.verdict, "reason": d.reason, "approved": approved})
        return approved, d.reason

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

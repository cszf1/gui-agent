"""敏感动作的统一摘要（v0.3.1，第二轮审查条目 2 / 3 / 6）。

原则：**只有执行器（Env.execute）持有输入的原文**。模型提示词、步骤记忆、反思、终端确认、安全日志、
轨迹日志、HTML 回放、评测结果一律使用这里生成的“安全摘要”，脱敏与否只由**输入目标**决定，
而不是由安全规则给出的 reason 字符串决定（v0.3 的做法：控制字符规则先命中时脱敏会失效）。

输入目标（焦点）状态：
- password    焦点元素是密码 / 安全输入框（is_password、type=password、名字含 password/密码 等）
- normal      焦点元素已知且不是密码框
- none        环境明确报告“没有可输入的焦点元素”（例如 Web 焦点在 body 上；MockEnv）
- unknown     环境明确报告“焦点无法确定”（例如跨源 iframe 探测失败），或者没有观察
- unreported  环境没有报告焦点信息（元素列表里没有 focused 元素）

typed text 在 password / unknown / unreported 时视为敏感（脱敏）；password / unknown 时还要人工确认
（见 safety.py）。另外支持“秘密占位符”：模型只输出 `<secret>名字</secret>`，执行器在发往环境前才替换为
AgentConfig.secrets 里的值，模型从头到尾看不到原文（参考 browser-use 的 sensitive_data）。
"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Iterable, Optional

from .keys import MODIFIERS, split_keys

REDACTED = "<redacted>"
SECRET_RE = re.compile(r"<secret>\s*([A-Za-z0-9_.\-]+)\s*</secret>")
SENSITIVE_STATES = {"password", "unknown", "unreported"}
_PW_NAME = re.compile(r"password|passcode|passwd|密码|口令", re.I)   # 名字只作补充；主依据是 is_password


def is_password_el(e) -> bool:
    if e is None:
        return False
    attrs = getattr(e, "attrs", {}) or {}
    return bool(getattr(e, "is_password", False) or attrs.get("password") == "true"
                or str(attrs.get("type", "")).lower() == "password" or _PW_NAME.search(getattr(e, "name", "") or ""))


def focus_target(obs) -> tuple[Optional[object], str]:
    """返回 (焦点元素或 None, 状态)。状态取值见模块说明。"""
    if obs is None:
        return None, "unknown"
    fs = getattr(obs, "focus_state", "") or ""
    if fs == "unknown":
        return None, "unknown"
    f = next((e for e in getattr(obs, "elements", []) if e.focused), None)
    if f is not None:
        return f, "password" if is_password_el(f) else "normal"
    if fs == "none":
        return None, "none"
    return None, "unreported"


def _printable_keys(a) -> bool:
    """hotkey / key_down 只按了可打印字符（没有 ctrl/alt/meta）：等价于逐字输入。"""
    if a.type not in {"hotkey", "key_down", "key_up"}:
        return False
    ks = split_keys(a.keys)
    return any(len(k) == 1 for k in ks) and not (set(ks) & (MODIFIERS - {"shift"})) and \
        all(len(k) == 1 or k in {"shift", "space"} for k in ks)


def is_sensitive_type(a, obs=None, state: Optional[str] = None) -> bool:
    """这个动作携带的文字是否必须脱敏（只看输入目标，不看安全规则的结论）。"""
    st = state or focus_target(obs)[1]
    if a.type == "type":
        if not a.text:
            return False
        if SECRET_RE.search(a.text):
            return True
        return st in SENSITIVE_STATES
    if _printable_keys(a):
        return st in {"password", "unknown"}
    return False


def safe_view(a, obs=None, state: Optional[str] = None) -> dict:
    """动作的可公开字典视图（日志 / 提示词 / 终端都用它）。"""
    d = a.to_dict()
    if is_sensitive_type(a, obs, state):
        if "text" in d:
            d["text"] = REDACTED
        if a.type != "type" and "keys" in d:
            d["keys"] = [REDACTED]
        d["redacted"] = True
    return d


def safe_short(a, obs=None, state: Optional[str] = None) -> str:
    d = safe_view(a, obs, state)
    d.pop("reason", None)
    return json.dumps(d, ensure_ascii=False)


def redacted_action(a, obs=None, state: Optional[str] = None):
    """返回一个文字已替换的 Action 副本（交给确认回调等外部代码；原动作不变）。"""
    if not is_sensitive_type(a, obs, state):
        return a
    if a.type == "type":
        return replace(a, text=REDACTED)
    return replace(a, keys=[REDACTED])


def resolve_secrets(text: Optional[str], secrets: dict) -> Optional[str]:
    """把 <secret>名字</secret> 替换为真实值（只在执行器出口调用）。未知名字保持原样（会被当作普通文字输入）。"""
    if not text or not secrets:
        return text
    return SECRET_RE.sub(lambda m: str(secrets.get(m.group(1), m.group(0))), text)


class Scrubber:
    """输出出口的最后一道防线：把已知的秘密字符串（及其 JSON / repr 转义形式）替换为 ***。

    长度 < min_len 的字符串不登记（否则会把日志里所有同样的短字符都抹掉）；结构化脱敏（safe_view）才是主手段。
    """

    def __init__(self, min_len: int = 4):
        self.min_len = min_len
        self._forms: set[str] = set()

    def add(self, *secrets: Optional[str]) -> None:
        for s in secrets:
            if not s or len(s) < self.min_len:
                continue
            for form in {s, json.dumps(s, ensure_ascii=False)[1:-1], json.dumps(s)[1:-1], repr(s)[1:-1]}:
                if len(form) >= self.min_len:
                    self._forms.add(form)

    def extend(self, secrets: Iterable[str]) -> None:
        self.add(*list(secrets))

    def __bool__(self) -> bool:
        return bool(self._forms)

    def scrub(self, text):
        if not self._forms or not isinstance(text, str):
            return text
        for f in sorted(self._forms, key=len, reverse=True):
            if f in text:
                text = text.replace(f, "***")
        return text

    def scrub_obj(self, obj):
        """递归清洗 dict / list / str（评测结果行等）。"""
        if not self._forms:
            return obj
        if isinstance(obj, str):
            return self.scrub(obj)
        if isinstance(obj, dict):
            return {k: self.scrub_obj(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return type(obj)(self.scrub_obj(v) for v in obj)
        return obj

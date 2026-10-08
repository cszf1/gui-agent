"""敏感动作的统一摘要（v0.3.1 第二轮审查条目 2 / 3 / 6；第三轮审查条目 2 / 5 / 6）。

原则：**只有执行器（Env.execute）持有输入的原文**。模型提示词、步骤记忆、反思、终端确认、安全日志、
轨迹日志、HTML 回放、评测结果一律使用这里生成的“安全摘要”，脱敏与否只由**输入目标**决定，
而不是由安全规则给出的 reason 字符串决定（v0.3 的做法：控制字符规则先命中时脱敏会失效）。

输入目标（焦点）状态：
- password    焦点元素是密码 / 安全输入框（is_password、type=password、名字含 password/密码 等）
- normal      焦点元素已知且不是密码框
- none        环境明确报告“没有可输入的焦点元素”（例如 Web 焦点在 body 上；MockEnv）
- unknown     环境明确报告“焦点无法确定”（例如跨源 iframe 探测失败），或者没有观察
- unreported  环境没有报告焦点信息（元素列表里没有 focused 元素）

typed text 在 password / unknown / unreported 时一律视为敏感（脱敏）。执行策略（是否需要人工确认）
保持 v0.3.1：只有 password / unknown 需要确认，unreported 的普通文本输入仍可直接执行，只是日志里
不再出现字符（第三轮审查条目 2）。

第三轮审查（条目 2 / 5 / 6）：
- 按键序列只要含**非修饰键**（单字符，或 Enter / Tab / Backspace / Space 这类输入键）就算“携带字符”，
  不再因为混入 Enter / Tab / Backspace 而取消脱敏。
- 敏感动作的安全副本（`redacted_action` 给确认回调 / cli_confirm）处理所有可能被模型抄进秘密的字符串
  字段（text / target / target2 / reason / url / app），而不仅仅是 text。
- `Scrubber` 提供共享实例接口（mark_sensitive / images_blocked / add / extend(explicit=...)），
  供 GUIAgent 在整次运行里复用，也为后续严格截图门控提供信号。
"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Iterable, Optional

from .keys import CHAR_KEYS, MODIFIERS, split_keys

REDACTED = "<redacted>"
SECRET_RE = re.compile(r"<secret>\s*([A-Za-z0-9_.\-]+)\s*</secret>")
# password / unknown / unreported 一律脱敏（第三轮条目 2）
SENSITIVE_STATES = {"password", "unknown", "unreported"}
# 需要人工确认的输入目标；保持 v0.3.1 既有可执行策略（unreported 不在内）
CONFIRM_STATES = {"password", "unknown"}
_PW_NAME = re.compile(r"password|passcode|passwd|密码|口令", re.I)   # 名字只作补充；主依据是 is_password
# 安全副本 / 签名需要清洗的字符串字段（模型可能把秘密抄进任意一个）
STR_FIELDS = ("text", "target", "target2", "reason", "url", "app", "path", "tool")


def is_password_el(e) -> bool:
    if e is None:
        return False
    attrs = getattr(e, "attrs", {}) or {}
    if bool(getattr(e, "is_password", False) or attrs.get("password") == "true"
            or str(attrs.get("type", "")).lower() == "password"):
        return True
    role = getattr(e, "role", "") or ""
    # 名字启发式仅对输入类控件生效，非输入类控件（如 "Forgot password?" 链接/按钮/说明）不属于密码框
    if role not in {"link", "button", "tab", "dialog", "heading", "text", "switch", "checkbox"}:
        if _PW_NAME.search(getattr(e, "name", "") or ""):
            return True
    return False


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


def carries_text(a) -> bool:
    """hotkey / key_down / key_up 是否携带字符（等价于把文字敲进去）。

    只按 ctrl / alt / meta 的是快捷键（不是文本）；shift 不算（shift+字符仍是字符）。
    只要有非修饰键会产生字符——单字符，或 Enter / Tab / Backspace / Space 这类输入键——就携带字符；
    不会因为混入 Enter / Tab / Backspace 而取消脱敏（第三轮条目 2）。功能键 / 方向键 / esc 不携带字符。
    """
    if a.type not in {"hotkey", "key_down", "key_up"}:
        return False
    ks = split_keys(a.keys)
    if set(ks) & (MODIFIERS - {"shift"}):
        return False
    return any(len(k) == 1 or k in CHAR_KEYS for k in ks)


def is_sensitive_type(a, obs=None, state: Optional[str] = None) -> bool:
    """这个动作携带的文字是否必须脱敏（只看输入目标，不看安全规则的结论）。"""
    st = state or focus_target(obs)[1]
    if a.type == "invoke" and a.method == "set_value":
        if not a.text:
            return False
        if SECRET_RE.search(a.text):
            return True
        element = obs.element(a.element_id) if obs is not None and a.element_id is not None else None
        return element is None or is_password_el(element)
    if a.type == "file" and a.text and SECRET_RE.search(a.text):
        return True
    if a.type == "type":
        if a.element_id is not None:
            element = obs.element(a.element_id) if obs is not None else None
            st = ("password" if is_password_el(element) else "normal") if element is not None and element.role == "textbox" else "unknown"
        if not a.text:
            return False
        if SECRET_RE.search(a.text):
            return True
        return st in SENSITIVE_STATES
    if carries_text(a):
        return st in SENSITIVE_STATES
    return False


def safe_view(a, obs=None, state: Optional[str] = None) -> dict:
    """动作的可公开字典视图（日志 / 提示词 / 终端都用它）。"""
    d = a.to_dict()
    if is_sensitive_type(a, obs, state):
        if a.type in {"type", "invoke", "file"}:
            if "text" in d:
                d["text"] = REDACTED
        elif "keys" in d:
            d["keys"] = [REDACTED]
        d["redacted"] = True
    return d


def safe_short(a, obs=None, state: Optional[str] = None) -> str:
    d = safe_view(a, obs, state)
    d.pop("reason", None)
    return json.dumps(d, ensure_ascii=False)


def redacted_action(a, obs=None, state: Optional[str] = None, scrubber=None):
    """安全副本（交给确认回调 / cli_confirm；原动作不变）。

    敏感输入的载荷（type 的 text / 按键动作的 keys）整体 redact；随后用共享 `Scrubber` 清洗所有
    字符串字段（text / target / target2 / reason / url / app），这样把秘密抄进 target / reason 也不会漏。
    """
    if is_sensitive_type(a, obs, state):
        a = replace(a, text=REDACTED) if a.type in {"type", "invoke", "file"} else replace(a, keys=[REDACTED])
    if scrubber is None:
        return a
    changes: dict = {}
    for f in STR_FIELDS:
        v = getattr(a, f, None)
        if isinstance(v, str) and v:
            sv = scrubber.scrub(v)
            if sv != v:
                changes[f] = sv
    keys = [scrubber.scrub(k) if isinstance(k, str) else k for k in (a.keys or [])]
    if keys != list(a.keys):
        changes["keys"] = keys
    return replace(a, **changes) if changes else a


def resolve_secrets(text: Optional[str], secrets: dict) -> Optional[str]:
    """把 <secret>名字</secret> 替换为真实值（只在执行器出口调用）。未知名字保持原样（会被当作普通文字输入）。"""
    if not text or not secrets:
        return text
    return SECRET_RE.sub(lambda m: str(secrets.get(m.group(1), m.group(0))), text)


class Scrubber:
    """输出出口的最后一道防线：把已知的秘密字符串（及其 JSON / repr 转义形式）替换为 ***。

    长度 < `min_len` 的字符串默认不登记（否则会把日志里所有同样的短字符都抹掉，把日志“抹失真”）；
    `explicit=True` 时即便更短也登记（配置里的秘密、完整敏感 type 原文由 GUIAgent 显式登记）。
    任何非空 secret 都会调用 `mark_sensitive()`：单调状态，供后续严格截图门控判断是否必须停止截图。

    `scrub` 使用一次性正则替换（长 form 优先），替换出来的 `***` 不会被再次处理，保证转换确定、可测试。
    """

    def __init__(self, min_len: int = 4):
        self.min_len = min_len
        self._forms: set[str] = set()
        self._sensitive = False
        self._pattern: Optional[re.Pattern] = None

    # ---------------------------------------------------------------- 共享接口
    def mark_sensitive(self) -> None:
        """单调置位：一旦有敏感信息，就一直是 True（不会被清掉）。"""
        self._sensitive = True

    @property
    def images_blocked(self) -> bool:
        """是否应当停止截图 / 回放（严格截图门控用的只读信号）。"""
        return self._sensitive

    def add(self, *secrets: Optional[str], explicit: bool = False) -> None:
        """登记秘密。任何非空 secret 都置敏感；默认按 min_len 过滤，explicit=True 时更短也登记。"""
        for s in secrets:
            if s is None:
                continue
            if not isinstance(s, str):
                s = str(s)
            if not s:
                continue
            self.mark_sensitive()
            self._add_forms(s, explicit)

    def extend(self, secrets: Iterable[str], explicit: bool = False) -> None:
        self.add(*list(secrets), explicit=explicit)

    def _add_forms(self, s: str, explicit: bool) -> None:
        if len(s) < self.min_len and not explicit:
            return
        for form in {s, json.dumps(s, ensure_ascii=False)[1:-1], json.dumps(s)[1:-1], repr(s)[1:-1]}:
            if form and (explicit or len(form) >= self.min_len):
                self._forms.add(form)
        self._pattern = None

    def __bool__(self) -> bool:
        return self._sensitive or bool(self._forms)

    # ---------------------------------------------------------------- 清洗
    def scrub(self, text):
        if not isinstance(text, str) or not self._forms:
            return text
        if self._pattern is None:
            self._pattern = re.compile("|".join(
                re.escape(f) for f in sorted(self._forms, key=len, reverse=True)))
        return self._pattern.sub("***", text)

    def scrub_obj(self, obj):
        """递归清洗 dict / list / str（评测结果行等）；dict 的字符串 key 也清洗。"""
        if not self._forms:
            return obj
        if isinstance(obj, str):
            return self.scrub(obj)
        if isinstance(obj, dict):
            return {(self.scrub(k) if isinstance(k, str) else k): self.scrub_obj(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return type(obj)(self.scrub_obj(v) for v in obj)
        return obj

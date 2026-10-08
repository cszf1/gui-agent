"""动作级后置条件（v0.6，方向 A 的核心增强）。

actor 在给出动作的同时预测“这一步做完之后界面上应当成立什么”（`expect` 列表），执行后由规则逐条核验：
无障碍树差分（元素出现 / 消失 / 值 / 勾选 / 焦点）+ 文本（带新鲜度，旧证据不算）+ 像素差（区域）。
没有任何模型调用；结论只有三种：pass / fail / unknown。只有 **全部 pass** 才能当作这一步成功的证据，
任意一条明确 fail 就是失败（并带上证据交给恢复策略），其余情况交回原有 L1 / L2 流程。

支持的谓词（JSON）：
  {"kind": "text_appears",    "text": "Saved"}
  {"kind": "text_disappears", "text": "Loading"}
  {"kind": "element_state",   "name": "Subscribe", "role": "checkbox", "checked": true}
        可用字段：checked / value / value_contains / enabled / focused / exists（任选，至少一个）
  {"kind": "window_title",    "contains": "Report"}
  {"kind": "url",             "contains": "/done"}
  {"kind": "pixel_change",    "region": [l, t, r, b], "min": 0.01}      （region 可省略 = 全屏）
  {"kind": "output_contains", "text": "3 files"}                         （shell / file / api 工具的返回输出）

隐私：密码元素的值从不读取（value / value_contains 谓词对密码元素一律 unknown）；
纯视觉消融（a11y_rules=False）下所有依赖无障碍树 / 文本的谓词都是 unknown，只有像素谓词可用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

KINDS = {"text_appears", "text_disappears", "element_state", "window_title", "url", "pixel_change", "output_contains"}
_STATE_KEYS = {"checked", "value", "value_contains", "enabled", "focused", "exists"}
_MAX_TEXT = 200


def validate_postcondition(pc: Any) -> Optional[str]:
    """返回错误说明；合法返回 None（Action.validate 调用，严格但不崩溃）。"""
    if not isinstance(pc, dict):
        return "postcondition must be an object"
    k = pc.get("kind")
    if k not in KINDS:
        return f"kind must be one of {sorted(KINDS)}"
    for key, v in pc.items():
        if isinstance(v, str) and len(v) > _MAX_TEXT:
            return f"{key} is too long"
    if k in {"text_appears", "text_disappears", "output_contains"}:
        if not isinstance(pc.get("text"), str) or not pc["text"].strip():
            return f"{k} needs non-empty text"
    elif k == "element_state":
        identity = pc.get("target_identity")
        if identity is not None and (not isinstance(identity, dict) or not identity
                                    or not set(identity) <= {"document_id", "dom_id", "uia_runtime", "atspi_identity"}
                                    or not all(isinstance(v, (str, int)) and not isinstance(v, bool)
                                               for v in identity.values())):
            return "target_identity must contain stable node attributes"
        if not isinstance(pc.get("name"), str) or not pc["name"].strip():
            return "element_state needs name"
        if not (set(pc) & _STATE_KEYS):
            return f"element_state needs one of {sorted(_STATE_KEYS)}"
        for b in ("checked", "enabled", "focused", "exists"):
            if b in pc and not isinstance(pc[b], bool):
                return f"{b} must be true/false"
        for s in ("value", "value_contains", "role"):
            if s in pc and not isinstance(pc[s], str):
                return f"{s} must be a string"
    elif k in {"window_title", "url"}:
        if not isinstance(pc.get("contains"), str) or not pc["contains"].strip():
            return f"{k} needs contains"
    elif k == "pixel_change":
        r = pc.get("region")
        if r is not None and (not isinstance(r, list) or len(r) != 4
                              or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in r)):
            return "region must be [l, t, r, b]"
        m = pc.get("min", 0.01)
        if not isinstance(m, (int, float)) or isinstance(m, bool) or not 0 <= m <= 1:
            return "min must be within 0..1"
    return None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _matches(obs, name: str, role: Optional[str] = None, identity: Optional[dict] = None):
    n = _norm(name)
    pool = [e for e in (getattr(obs, "elements", None) or []) if role is None or e.role == role]
    if identity:
        pool = [e for e in pool if all(e.attrs.get(key) == value for key, value in identity.items())]
    exact = [e for e in pool if _norm(e.name) == n]
    if exact:
        return exact
    part = [e for e in pool if n and n in _norm(e.name)]
    return part


def _find(obs, name: str, role: Optional[str] = None):
    matches = _matches(obs, name, role)
    return matches[0] if len(matches) == 1 else None


@dataclass
class PCResult:
    kind: str
    status: str          # pass | fail | unknown
    evidence: str
    spec: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "status": self.status, "evidence": self.evidence, "spec": self.spec}


@dataclass
class PostconditionReport:
    results: list[PCResult]

    @property
    def verdict(self) -> str:
        if not self.results:
            return "none"
        if any(r.status == "fail" for r in self.results):
            return "failed"
        if all(r.status == "pass" for r in self.results):
            return "success"
        return "uncertain"

    def evidence(self) -> str:
        return "; ".join(f"{r.kind}:{r.status}({r.evidence})" for r in self.results)[:600]

    def to_list(self) -> list[dict]:
        return [r.to_dict() for r in self.results]


def _safe_spec(pc: dict, before, after) -> dict:
    """日志里不回显指向密码元素的期望值。"""
    d = dict(pc)
    if pc.get("kind") == "element_state":
        for obs in (before, after):
            matches = _matches(obs, pc.get("name", ""), pc.get("role")) if obs is not None else []
            if any(getattr(e, "is_password", False) for e in matches):
                d.pop("value", None)
                d.pop("value_contains", None)
    return d


def evaluate_one(pc: dict, before, after, use_a11y: bool = True, output: Optional[str] = None) -> PCResult:
    k = pc.get("kind", "")
    spec = _safe_spec(pc, before, after)
    if k == "output_contains":
        if output is None:
            return PCResult(k, "unknown", "action produced no tool output", spec)
        hit = pc["text"] in output
        return PCResult(k, "pass" if hit else "fail", "tool output contains text" if hit else
                        "tool output lacks text", spec)
    if k == "pixel_change":
        from .diff import frame_diff, region_crop_diff
        r = pc.get("region")
        m = float(pc.get("min", 0.01))
        try:
            d = region_crop_diff(before.screenshot, after.screenshot, r) if r else frame_diff(before.screenshot,
                                                                                               after.screenshot)
        except Exception as e:  # noqa: BLE001
            return PCResult(k, "unknown", f"pixel diff failed ({type(e).__name__})", spec)
        return PCResult(k, "pass" if d >= m else "fail", f"diff={d:.4f} min={m}", spec)
    if not use_a11y:
        return PCResult(k, "unknown", "accessibility/text evidence disabled by policy (vision only)", spec)
    if k == "text_appears":
        from .verifier import _evidence_fragments
        t = pc["text"]
        now = t.lower() in after.all_text().lower()
        if not now:
            return PCResult(k, "fail", f"{t!r} not visible after the action", spec)
        cur = _evidence_fragments(after, t)
        base = _evidence_fragments(before, t) if before is not None else []
        if base and any(bf in cf or cf in bf for bf in base for cf in cur):
            return PCResult(k, "unknown", f"{t!r} was already visible before the action (stale evidence)", spec)
        return PCResult(k, "pass", f"{t!r} newly visible", spec)
    if k == "text_disappears":
        t = pc["text"].lower()
        was = before is not None and t in before.all_text().lower()
        now = t in after.all_text().lower()
        if now:
            return PCResult(k, "fail", f"{pc['text']!r} still visible", spec)
        return PCResult(k, "pass" if was else "unknown",
                        f"{pc['text']!r} gone" if was else f"{pc['text']!r} was not visible before either", spec)
    if k == "window_title":
        c = pc["contains"].lower()
        ok = c in (after.active_window or "").lower()
        return PCResult(k, "pass" if ok else "fail", f"title={after.active_window!r}", spec)
    if k == "url":
        c = pc["contains"].lower()
        if not after.url:
            return PCResult(k, "unknown", "no URL on this platform", spec)
        return PCResult(k, "pass" if c in after.url.lower() else "fail", f"url={after.url!r}", spec)
    if k == "element_state":
        matches = _matches(after, pc["name"], pc.get("role"), pc.get("target_identity"))
        if len(matches) > 1:
            return PCResult(k, "unknown", "multiple matching controls; target is ambiguous", spec)
        e = matches[0] if matches else None
        if "exists" in pc:
            if pc["exists"] is False:
                return PCResult(k, "pass" if e is None else "fail",
                                "element absent" if e is None else f"element still present: {e.brief()}", spec)
            if e is None:
                return PCResult(k, "fail", f"no element named {pc['name']!r}", spec)
        if e is None:
            return PCResult(k, "unknown", f"no element named {pc['name']!r} in the accessibility tree", spec)
        checks: list[tuple[bool, str]] = []
        if "checked" in pc:
            if e.checked is None:
                return PCResult(k, "unknown", f"{e.role} {e.name!r} reports no checked state", spec)
            checks.append((e.checked == pc["checked"], f"checked={e.checked}"))
        if "enabled" in pc:
            checks.append((e.enabled == pc["enabled"], f"enabled={e.enabled}"))
        if "focused" in pc:
            checks.append((e.focused == pc["focused"], f"focused={e.focused}"))
        if "value" in pc or "value_contains" in pc:
            if e.is_password:
                return PCResult(k, "unknown", "password field value is never read", spec)
            v = e.value or ""
            if "value" in pc:
                checks.append((v == pc["value"], "value matches" if v == pc["value"] else "value differs"))
            if "value_contains" in pc:
                hit = pc["value_contains"] in v
                checks.append((hit, "value contains text" if hit else "value lacks text"))
        if not checks:
            return PCResult(k, "pass", "element exists", spec)
        ok = all(c for c, _ in checks)
        return PCResult(k, "pass" if ok else "fail", f"{e.role} {e.name!r}: " + ", ".join(m for _, m in checks), spec)
    return PCResult(k, "unknown", "unsupported postcondition", spec)


def evaluate(preds: list, before, after, use_a11y: bool = True, output: Optional[str] = None) -> PostconditionReport:
    return PostconditionReport([evaluate_one(pc, before, after, use_a11y, output) for pc in preds or []
                                if isinstance(pc, dict)])


def implied_postconditions(action, before) -> list[dict]:
    """语义动作自带的后置条件（不依赖模型预测）：用来证明“后台动作真的生效了”。"""
    if getattr(action, "type", "") != "invoke" or before is None:
        return []
    el = before.element(action.element_id) if action.element_id is not None else None
    if el is None:
        return []
    m = action.method
    identity = {key: el.attrs[key] for key in ("document_id", "dom_id", "uia_runtime", "atspi_identity")
                if key in el.attrs}
    common = {"kind": "element_state", "name": el.name, "role": el.role}
    if identity:
        common["target_identity"] = identity
    if m == "toggle" and el.checked is not None:
        return [dict(common, checked=not el.checked)]
    if m == "select" and el.role in {"radio", "checkbox", "tab", "listitem"} and el.checked is not None:
        return [dict(common, checked=True)]
    if m == "set_value" and not el.is_password and action.text is not None:
        return [dict(common, value=action.text)]
    if m == "focus":
        return [dict(common, focused=True)]
    return []


# ---------------------------------------------------------------------------- 无障碍树差分
def _key(e) -> tuple:
    return (e.role, _norm(e.name))


def a11y_diff(before, after, limit: int = 20) -> dict:
    """按 (role, name) 对齐元素，报告新增 / 消失 / 状态变化（密码元素只报告存在性，不报告值）。"""
    if before is None or after is None:
        return {"added": [], "removed": [], "changed": []}
    b = {}
    for e in before.elements or []:
        b.setdefault(_key(e), e)
    a = {}
    for e in after.elements or []:
        a.setdefault(_key(e), e)
    added = [a[k].brief() for k in a if k not in b][:limit]
    removed = [b[k].brief() for k in b if k not in a][:limit]
    changed = []
    for k in a:
        if k not in b:
            continue
        x, y = b[k], a[k]
        diffs = []
        if x.checked != y.checked:
            diffs.append(f"checked {x.checked}->{y.checked}")
        if x.enabled != y.enabled:
            diffs.append(f"enabled {x.enabled}->{y.enabled}")
        if x.focused != y.focused:
            diffs.append(f"focused {x.focused}->{y.focused}")
        if (x.value or "") != (y.value or "") and not (x.is_password or y.is_password):
            diffs.append("value changed")
        if diffs:
            changed.append(f"{y.role} {y.name!r}: " + ", ".join(diffs))
    return {"added": added, "removed": removed, "changed": changed[:limit]}


def diff_is_empty(d: dict) -> bool:
    return not (d.get("added") or d.get("removed") or d.get("changed"))

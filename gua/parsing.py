"""模型输出解析：JSON 动作、坐标点、UI-TARS 原生动作串。

v0.3：所有失败统一抛 ActionParseError（ValueError 子类，带 code / field / feedback()），
不再出现 {"action": null} → TypeError 这类绕过反馈/恢复的崩溃。
"""
from __future__ import annotations

import ast
import json
import re
from typing import Any, Optional

from .actions import Action, ActionParseError, parse_action

_JSON_RE = re.compile(r"\{.*\}", re.S)


def extract_json(text: str) -> dict[str, Any]:
    """从模型回复里抽出第一个 JSON 对象（容忍 ```json 包裹和前后解释）。"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"```\s*$", "", text).strip()
    try:
        v = json.loads(text)
        if isinstance(v, dict):
            return v
        if isinstance(v, list):
            return {"items": v}
    except json.JSONDecodeError:
        pass
    m = _JSON_RE.search(text)
    if not m:
        raise ActionParseError("no_json", f"no JSON object in model output: {text[:200]!r}", None, text)
    try:
        v = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise ActionParseError("no_json", f"malformed JSON ({e.msg})", None, text) from None
    if not isinstance(v, dict):
        raise ActionParseError("not_object", "expected a JSON object", None, text)
    return v


_POINT_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)")
_TAG_POINT_RE = re.compile(r"<point>\s*(-?\d+(?:\.\d+)?)[\s,]+(-?\d+(?:\.\d+)?)\s*</point>")
_BOX_RE = re.compile(r"<\|box_start\|>\s*\((-?\d+),\s*(-?\d+)\)\s*<\|box_end\|>")


def parse_point(text: str) -> Optional[tuple[float, float]]:
    """兼容 {"x":..,"y":..}、{"point":[x,y]}、bbox、"(x, y)"、<point>x y</point>、UI-TARS start_box。"""
    try:
        obj = extract_json(text)
        if "x" in obj and "y" in obj:
            return float(obj["x"]), float(obj["y"])
        for k in ("point", "coordinate", "start_box", "bbox"):
            if k in obj and not isinstance(obj[k], str):
                v = obj[k]
                if len(v) == 4:
                    return (v[0] + v[2]) / 2, (v[1] + v[3]) / 2
                return float(v[0]), float(v[1])
    except Exception:
        pass
    for rx in (_TAG_POINT_RE, _BOX_RE, _POINT_RE):
        m = rx.search(text or "")
        if m:
            return float(m.group(1)), float(m.group(2))
    return None


# ---------------- UI-TARS 原生输出 ----------------
_UITARS_CALL = re.compile(r"Action:\s*(\w+)\((.*)\)\s*$", re.S | re.M)


def _pt(v: str) -> Optional[tuple[float, float]]:
    if v is None:
        return None
    m = _TAG_POINT_RE.search(v) or _POINT_RE.search(v)
    if not m:
        nums = re.findall(r"-?\d+(?:\.\d+)?", v)
        if len(nums) >= 2:
            if len(nums) >= 4:  # (x1,y1,x2,y2) → 中心
                a = list(map(float, nums[:4]))
                return (a[0] + a[2]) / 2, (a[1] + a[3]) / 2
            return float(nums[0]), float(nums[1])
        return None
    return float(m.group(1)), float(m.group(2))


def _kwargs(argstr: str) -> dict[str, str]:
    """解析 click(start_box='(1,2)', content='a,b') 这种参数串。"""
    try:
        call = ast.parse(f"f({argstr})", mode="eval").body
        return {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords}  # type: ignore[attr-defined]
    except Exception:
        out = {}
        for m in re.finditer(r"(\w+)\s*=\s*'((?:\\'|[^'])*)'", argstr):
            out[m.group(1)] = m.group(2).replace("\\'", "'")
        return out


def parse_uitars(text: str, coord_space: str = "resized") -> tuple[Action, str]:
    """解析 UI-TARS 的 "Thought: ...\\nAction: click(start_box='(x,y)')" 输出。

    UI-TARS-1.5（基于 Qwen2.5-VL）输出的是 smart_resize 后图像上的绝对坐标（coord_space=resized），
    UI-TARS-1.0 / 72B 输出 [0,1000) 归一化坐标（coord_space=norm1000）。见 UI-TARS README_coordinates.md。
    """
    thought = ""
    tm = re.search(r"Thought:\s*(.*?)(?:\nAction:|$)", text or "", re.S)
    if tm:
        thought = tm.group(1).strip()
    m = _UITARS_CALL.search(text or "")
    if not m:
        raise ActionParseError("no_json", f"no JSON action and no UI-TARS action in: {(text or '')[:160]!r}",
                               None, text)
    name, kw = m.group(1).lower(), _kwargs(m.group(2))
    p1 = _pt(kw.get("start_box") or kw.get("point") or kw.get("start_point"))
    p2 = _pt(kw.get("end_box") or kw.get("end_point"))
    base = {"coord_space": coord_space, "reason": thought}
    if p1:
        base.update(x=p1[0], y=p1[1])
    if name in {"click", "left_single"}:
        a = Action("click", **base)
    elif name in {"left_double", "double_click"}:
        a = Action("double_click", **base)
    elif name in {"right_single", "right_click"}:
        a = Action("right_click", **base)
    elif name == "long_press":
        a = Action("long_press", **base)
    elif name in {"drag", "select"}:
        if p2 is None:
            raise ActionParseError("missing_field", "drag needs end_box", "end_box", text)
        a = Action("drag", x2=p2[0], y2=p2[1], **base)
    elif name == "hotkey":
        a = Action("hotkey", keys=str(kw.get("key", "")).split(), reason=thought)
    elif name == "type":
        content = str(kw.get("content", ""))
        submit = content.endswith("\n")
        a = Action("type", text=content.rstrip("\n"), submit=submit, reason=thought)
    elif name == "scroll":
        a = Action("scroll", direction=kw.get("direction", "down"), amount=5, **base)
    elif name == "wait":
        a = Action("wait", seconds=5, reason=thought)
    elif name == "open_app":
        a = Action("open_app", app=kw.get("app_name"), reason=thought)
    elif name == "press_home":
        a = Action("home", reason=thought)
    elif name == "press_back":
        a = Action("back", reason=thought)
    elif name == "finished":
        a = Action("done", text=kw.get("content", ""), reason=thought)
    elif name == "call_user":
        a = Action("ask_user", text=thought or "need help", reason=thought)
    else:
        raise ActionParseError("unknown_action", f"unsupported UI-TARS action {name}", "action", text)
    return a.validate(), thought


def parse_model_action(text: str, default_coord_space: str = "pixel") -> tuple[Action, str]:
    """通用入口：JSON（{"thought":..,"action":{..}}）优先；没有 JSON 对象时再试 UI-TARS 格式。

    任何失败都抛 ActionParseError（主循环把 feedback() 交还模型）。
    """
    if not isinstance(text, str):
        raise ActionParseError("bad_type", f"model reply must be text, got {type(text).__name__}")
    stripped = text.strip()
    looks_json = stripped.startswith(("{", "[", "```"))
    if not looks_json and _UITARS_CALL.search(text):
        return parse_uitars(text, default_coord_space if default_coord_space != "pixel" else "resized")
    try:
        obj = extract_json(text)
    except ActionParseError:
        if stripped.startswith("[") or not _UITARS_CALL.search(text):
            raise
        return parse_uitars(text, default_coord_space if default_coord_space != "pixel" else "resized")
    if "items" in obj and len(obj) == 1:
        raise ActionParseError("not_object", "expected one JSON object with an \"action\", got a list", None, text)
    act = obj.get("action", obj) if "action" in obj else obj
    if isinstance(act, str):
        act = {"type": act, **{k: v for k, v in obj.items() if k not in {"action", "thought"}}}
    if act is None:
        raise ActionParseError("missing_type", "\"action\" is null", "action", text)
    thought = obj.get("thought", "")
    thought = thought if isinstance(thought, str) else json.dumps(thought, ensure_ascii=False)[:500]
    a = parse_action(act, default_coord_space)
    if not a.reason:
        a.reason = thought
    return a, thought

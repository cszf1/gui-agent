"""统一动作空间（跨 Windows / macOS / Linux / Android / Web）。

设计参考（详见 docs/research.md）：
- UI-TARS COMPUTER_USE / MOBILE_USE 提示词里的动作原语（click / left_double / right_single / drag /
  hotkey / type / scroll / wait / finished；移动端 long_press / open_app / press_home / press_back）
- AndroidWorld JSONAction（click / long_press / input_text / navigate_back / navigate_home / open_app / status / answer）
- Anthropic computer-use 工具（left_click / double_click / key / type / scroll / wait / zoom）
- browser-use 的 navigate / go_back

约定：进入 Env.execute 的坐标一律是 **截图像素坐标**（与 Observation.screenshot 同一坐标系）。
模型输出的归一化坐标（norm1000 / norm1 / resized）由 agent 在执行前用 coords.CoordMapper 换算，
换算后 coord_space 置为 "pixel"。各平台后端再负责把截图像素映射到自己的输入坐标（例如 macOS 的
Retina 像素 → point）。

v0.3（审查条目 5）：严格校验。模型给出的动作在进入主循环前必须通过 schema 校验（类型、必填字段、取值范围、
未知动作），否则抛出结构化的 ActionParseError（code / field / message），主循环把 `feedback()` 反馈给模型，
而不是让 TypeError 之类的异常绕过验证/恢复直接崩掉整次运行。
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

POINTER_ACTIONS = {"click", "double_click", "right_click", "long_press", "move", "drag"}
ACTION_TYPES = POINTER_ACTIONS | {
    "scroll", "type", "hotkey", "key_down", "key_up", "wait",
    "open_app", "navigate", "back", "home", "focus_window",
    "ask_user", "done", "fail",
}
TERMINAL_ACTIONS = {"done", "fail"}
# 各平台不支持的动作（执行层直接返回 unsupported，而不是静默成功）
UNSUPPORTED = {
    "web": {"home", "open_app", "long_press"},
    "android": {"right_click", "move", "key_down", "key_up", "navigate", "focus_window"},
    "windows": {"navigate", "home"},
    "macos": {"navigate", "home"},
    "linux": {"navigate", "home"},
    "mock": set(),
}
SCROLL_DIRS = {"up", "down", "left", "right"}
COORD_SPACES = {"pixel", "norm1000", "norm1", "resized"}
COORD_RANGE = {"norm1000": 1000.0, "norm1": 1.0}
MAX_WAIT_SECONDS = 120.0
MAX_SCROLL_AMOUNT = 100

_EXAMPLE = '{"thought": "...", "action": {"type": "click", "target": "Save button"}}'


class ActionParseError(ValueError):
    """模型输出无法解析 / 动作不合法。code 取值：
    no_json | not_object | missing_type | unknown_action | bad_type | missing_field | out_of_range | bad_value | invalid
    """

    def __init__(self, code: str, message: str, field: Optional[str] = None, raw: Any = None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.field, self.raw = code, message, field, raw

    def feedback(self) -> str:
        where = f" (field {self.field!r})" if self.field else ""
        return (f"Your last action was invalid [{self.code}]{where}: {self.message}. "
                f"Reply with ONE valid JSON action, e.g. {_EXAMPLE}")

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "field": self.field, "message": self.message}


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _coerce_num(v: Any, name: str) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, str):
        try:
            v = float(v.strip())
        except ValueError:
            raise ActionParseError("bad_type", f"{name} must be a number, got {v!r}", name) from None
    if not _is_num(v):
        raise ActionParseError("bad_type", f"{name} must be a finite number, got {v!r}", name)
    return v


def _coerce_bool(v: Any, name: str) -> bool:
    if isinstance(v, bool) or v is None:
        return bool(v)
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    if isinstance(v, str) and v.strip().lower() in {"true", "false", "1", "0", "yes", "no"}:
        return v.strip().lower() in {"true", "1", "yes"}
    raise ActionParseError("bad_type", f"{name} must be true/false, got {v!r}", name)


def _coerce_str(v: Any, name: str) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if _is_num(v):
        return str(v)
    raise ActionParseError("bad_type", f"{name} must be a string, got {type(v).__name__}", name)


@dataclass
class Action:
    type: str
    x: Optional[float] = None
    y: Optional[float] = None
    x2: Optional[float] = None          # drag 终点
    y2: Optional[float] = None
    coord_space: str = "pixel"          # pixel | norm1000 | norm1 | resized
    text: Optional[str] = None          # type 文本 / focus_window 标题 / ask_user 问题 / done 的答案
    keys: list[str] = field(default_factory=list)   # hotkey / key_down / key_up
    direction: Optional[str] = None     # scroll: up|down|left|right
    amount: int = 0                     # scroll 格数（>0）
    seconds: float = 0.0                # wait / long_press 时长
    app: Optional[str] = None           # open_app：应用名 / Android 包名 / 可执行文件
    url: Optional[str] = None           # navigate
    target: Optional[str] = None        # 目标元素的自然语言描述（交给 grounder）
    target2: Optional[str] = None       # drag 终点描述
    element_id: Optional[int] = None    # 来自无障碍树候选列表
    clear: bool = False                 # type 前先清空
    submit: bool = False                # type 后回车
    reason: str = ""                    # 模型给出的理由（日志用）
    # 模型坐标 → 截图像素的变换链（来自实际发送的图像尺寸，见 coords.ImageTransform）；不序列化
    transform: Optional[Any] = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.type, str) or self.type not in ACTION_TYPES:
            raise ActionParseError("unknown_action", f"unknown action type {self.type!r}; valid: "
                                   f"{', '.join(sorted(ACTION_TYPES))}", "type")
        if self.type == "scroll":
            if self.direction is None:
                # 兼容旧写法 amount>0 向上，<0 向下
                self.direction = "up" if _is_num(self.amount) and self.amount > 0 else "down"
            if not isinstance(self.direction, str) or self.direction.lower() not in SCROLL_DIRS:
                raise ActionParseError("bad_value", f"scroll direction must be one of up/down/left/right, "
                                       f"got {self.direction!r}", "direction")
            self.direction = self.direction.lower()
            if not _is_num(self.amount):
                raise ActionParseError("bad_type", f"amount must be an integer, got {self.amount!r}", "amount")
            self.amount = abs(int(self.amount)) or 3
        if self.element_id is not None:
            if isinstance(self.element_id, bool) or not (_is_num(self.element_id) or
                                                         (isinstance(self.element_id, str) and self.element_id.strip().isdigit())):
                raise ActionParseError("bad_type", f"element_id must be an integer, got {self.element_id!r}", "element_id")
            self.element_id = int(float(self.element_id))

    # ---------------------------------------------------------------- 严格校验
    def validate(self) -> "Action":
        """检查类型 / 必填字段 / 取值范围；不合法抛 ActionParseError。返回自身便于链式调用。"""
        t = self.type
        for name in ("x", "y", "x2", "y2"):
            v = getattr(self, name)
            if v is not None and not _is_num(v):
                raise ActionParseError("bad_type", f"{name} must be a finite number, got {v!r}", name)
        for a, b in (("x", "y"), ("x2", "y2")):
            if (getattr(self, a) is None) != (getattr(self, b) is None):
                missing = a if getattr(self, a) is None else b
                raise ActionParseError("missing_field", f"{missing} is required together with "
                                       f"{b if missing == a else a}", missing)
        if self.coord_space not in COORD_SPACES:
            raise ActionParseError("bad_value", f"coord_space must be one of {sorted(COORD_SPACES)}, got "
                                   f"{self.coord_space!r}", "coord_space")
        hi = COORD_RANGE.get(self.coord_space)
        for name in ("x", "y", "x2", "y2"):
            v = getattr(self, name)
            if v is None:
                continue
            if self.coord_space != "pixel" and v < 0:
                raise ActionParseError("out_of_range", f"{name}={v} is negative", name)
            if hi is not None and v > hi:
                raise ActionParseError("out_of_range", f"{name}={v} exceeds {hi:g} for coord_space "
                                       f"{self.coord_space}", name)
        if self.element_id is not None and self.element_id < 0:
            raise ActionParseError("out_of_range", "element_id must be >= 0", "element_id")
        for name in ("text", "target", "target2", "app", "url", "reason"):
            v = getattr(self, name)
            if v is not None and not isinstance(v, str):
                raise ActionParseError("bad_type", f"{name} must be a string", name)
        if not isinstance(self.keys, list) or not all(isinstance(k, str) and k.strip() for k in self.keys):
            raise ActionParseError("bad_type", "keys must be a list of key names", "keys")
        if not _is_num(self.seconds) or not 0 <= self.seconds <= MAX_WAIT_SECONDS:
            raise ActionParseError("out_of_range", f"seconds must be within 0..{MAX_WAIT_SECONDS:g}", "seconds")
        if t in POINTER_ACTIONS and self.x is None and self.element_id is None and not (self.target or "").strip():
            raise ActionParseError("missing_field", f"{t} needs x/y, element_id or target", "target")
        if t == "drag" and self.x2 is None and not (self.target2 or self.text or "").strip():
            raise ActionParseError("missing_field", "drag needs an end point: x2/y2 or target2", "target2")
        if t == "scroll" and not 1 <= self.amount <= MAX_SCROLL_AMOUNT:
            raise ActionParseError("out_of_range", f"amount must be within 1..{MAX_SCROLL_AMOUNT}", "amount")
        if t == "type" and self.text is None:
            raise ActionParseError("missing_field", "type needs text", "text")
        if t in {"hotkey", "key_down", "key_up"} and not self.keys:
            raise ActionParseError("missing_field", f"{t} needs keys, e.g. [\"ctrl\", \"s\"]", "keys")
        if t == "open_app" and not (self.app or "").strip():
            raise ActionParseError("missing_field", "open_app needs app", "app")
        if t == "navigate" and not (self.url or self.text or "").strip():
            raise ActionParseError("missing_field", "navigate needs url", "url")
        if t == "ask_user" and not (self.text or "").strip():
            raise ActionParseError("missing_field", "ask_user needs text (the question)", "text")
        return self

    @property
    def is_pointer(self) -> bool:
        return self.type in POINTER_ACTIONS

    @property
    def needs_location(self) -> bool:
        return self.is_pointer and self.x is None

    @property
    def point(self) -> Optional[tuple[int, int]]:
        if self.x is None or self.y is None:
            return None
        return int(self.x), int(self.y)

    def to_dict(self) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__
             if k not in {"coord_space", "transform"} and getattr(self, k) not in (None, [], "", 0, 0.0, False)}
        d = json.loads(json.dumps(d, default=str))
        if self.coord_space != "pixel":
            d["coord_space"] = self.coord_space
        d["type"] = self.type
        return d

    def short(self) -> str:
        d = self.to_dict()
        d.pop("reason", None)
        return json.dumps(d, ensure_ascii=False)


def parse_action(obj: dict[str, Any], default_coord_space: str = "pixel") -> Action:
    """把模型给出的 JSON 动作转成 Action：容忍别名与多种写法，但严格校验类型与取值（不合法抛 ActionParseError）。"""
    if not isinstance(obj, dict):
        raise ActionParseError("not_object", f"action must be a JSON object, got {type(obj).__name__}", "action", obj)
    obj = dict(obj)
    t = None
    for k in ("type", "action", "action_type"):
        if obj.get(k) is not None:
            t = obj.pop(k)
            break
    if isinstance(t, dict):                       # {"action": {"type": ...}} 嵌套
        return parse_action({**{k: v for k, v in obj.items() if k not in {"type", "action", "action_type"}}, **t},
                            default_coord_space)
    if t is None:
        raise ActionParseError("missing_type", "action has no \"type\"", "type", obj)
    if not isinstance(t, str) or not t.strip():
        raise ActionParseError("bad_type", f"action type must be a string, got {t!r}", "type", obj)
    t = _ALIASES.get(t.strip().lower(), t.strip().lower())
    if t not in ACTION_TYPES:
        raise ActionParseError("unknown_action", f"unknown action type {t!r}; valid: {', '.join(sorted(ACTION_TYPES))}",
                               "type", obj)
    if t == "done" and str(obj.get("goal_status", obj.get("status", ""))).lower() in {"infeasible", "failure", "fail"}:
        t = "fail"
    keys = obj.pop("keys", None)
    if keys is None:
        keys = obj.pop("key", None)
    keys = [] if keys is None else keys
    if isinstance(keys, str):
        sep = "+" if "+" in keys else " "
        keys = [k.strip() for k in keys.split(sep) if k.strip()]
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        raise ActionParseError("bad_type", f"keys must be a list of strings or \"ctrl+s\", got {keys!r}", "keys", obj)
    for k in ("point", "coordinate", "start_point"):
        if obj.get(k) is not None:
            v = obj[k]
            if not isinstance(v, (list, tuple)) or len(v) != 2:
                raise ActionParseError("bad_type", f"{k} must be [x, y], got {v!r}", k, obj)
            obj.setdefault("x", v[0])
            obj.setdefault("y", v[1])
    if obj.get("end_point") is not None:
        v = obj["end_point"]
        if not isinstance(v, (list, tuple)) or len(v) != 2:
            raise ActionParseError("bad_type", f"end_point must be [x, y], got {v!r}", "end_point", obj)
        obj.setdefault("x2", v[0])
        obj.setdefault("y2", v[1])
    if "index" in obj and "element_id" not in obj:
        obj["element_id"] = obj.pop("index")
    if t == "open_app" and "app" not in obj:
        obj["app"] = obj.pop("app_name", None) or obj.get("text")
    if t in {"done", "fail"} and "text" not in obj:
        obj["text"] = obj.get("answer") or obj.get("content") or obj.get("reason")
    if t == "ask_user" and "text" not in obj:
        obj["text"] = obj.get("question")
    kw: dict[str, Any] = {}
    for n in ("x", "y", "x2", "y2", "seconds"):
        v = _coerce_num(obj.get(n), n)
        if v is not None:
            kw[n] = v
    if obj.get("amount") is not None:
        kw["amount"] = int(_coerce_num(obj["amount"], "amount"))
    for n in ("text", "target", "target2", "app", "url", "direction", "reason"):
        v = _coerce_str(obj.get(n), n)
        if v is not None:
            kw[n] = v
    for n in ("clear", "submit"):
        if n in obj:
            kw[n] = _coerce_bool(obj[n], n)
    if obj.get("element_id") is not None:
        kw["element_id"] = obj["element_id"]
    cs = obj.get("coord_space", default_coord_space)
    kw["coord_space"] = cs if cs is not None else default_coord_space
    if "seconds" in kw and kw["seconds"] < 0:
        raise ActionParseError("out_of_range", "seconds must be >= 0", "seconds", obj)
    try:
        a = Action(type=t, keys=list(keys), **kw)
    except ActionParseError:
        raise
    except (TypeError, ValueError) as e:
        raise ActionParseError("invalid", str(e)[:200], None, obj) from None
    return a.validate()


_ALIASES = {
    "left_click": "click", "tap": "click", "left_single": "click",
    "left_double": "double_click", "double_tap": "double_click", "doubleclick": "double_click",
    "right_single": "right_click", "rightclick": "right_click",
    "mouse_move": "move", "hover": "move",
    "left_click_drag": "drag", "swipe": "drag",
    "input_text": "type", "write": "type",
    "key": "hotkey", "press": "hotkey", "keyboard": "hotkey",
    "sleep": "wait", "navigate_back": "back", "press_back": "back", "go_back": "back",
    "navigate_home": "home", "press_home": "home", "goto": "navigate", "go_to_url": "navigate",
    "finished": "done", "finish": "done", "terminate": "done", "status": "done", "answer": "done",
    "call_user": "ask_user", "ask": "ask_user", "launch_app": "open_app",
}

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
"""
from __future__ import annotations

import json
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

    def __post_init__(self) -> None:
        if self.type not in ACTION_TYPES:
            raise ValueError(f"unknown action type: {self.type}")
        if self.type == "scroll":
            if self.direction is None:
                # 兼容旧写法 amount>0 向上，<0 向下
                self.direction = "up" if self.amount > 0 else "down"
            if self.direction not in SCROLL_DIRS:
                raise ValueError(f"bad scroll direction {self.direction}")
            self.amount = abs(int(self.amount)) or 3
        if self.element_id is not None:
            self.element_id = int(self.element_id)

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
        d = {k: v for k, v in asdict(self).items()
             if v not in (None, [], "", 0, 0.0, False) and k != "coord_space"}
        if self.coord_space != "pixel":
            d["coord_space"] = self.coord_space
        d["type"] = self.type
        return d

    def short(self) -> str:
        d = self.to_dict()
        d.pop("reason", None)
        return json.dumps(d, ensure_ascii=False)


def parse_action(obj: dict[str, Any], default_coord_space: str = "pixel") -> Action:
    """把模型给出的 JSON 动作宽松地转成 Action（容忍别名与多种写法）。"""
    obj = dict(obj)
    t = obj.pop("type", None) or obj.pop("action", None) or obj.pop("action_type", None)
    if t is None:
        raise ValueError(f"action without type: {obj}")
    t = _ALIASES.get(str(t).lower(), str(t).lower())
    if t == "done" and str(obj.get("goal_status", obj.get("status", ""))).lower() in {"infeasible", "failure", "fail"}:
        t = "fail"
    keys = obj.pop("keys", None) or obj.pop("key", None) or []
    if isinstance(keys, str):
        sep = "+" if "+" in keys else " "
        keys = [k.strip() for k in keys.split(sep) if k.strip()]
    for k in ("point", "coordinate", "start_point"):
        if k in obj and isinstance(obj[k], (list, tuple)) and len(obj[k]) >= 2:
            obj.setdefault("x", obj[k][0])
            obj.setdefault("y", obj[k][1])
    if "end_point" in obj and isinstance(obj["end_point"], (list, tuple)):
        obj.setdefault("x2", obj["end_point"][0])
        obj.setdefault("y2", obj["end_point"][1])
    if "index" in obj and "element_id" not in obj:
        obj["element_id"] = obj.pop("index")
    if t == "open_app" and "app" not in obj:
        obj["app"] = obj.pop("app_name", None) or obj.get("text")
    if t in {"done", "fail"} and "text" not in obj:
        obj["text"] = obj.get("answer") or obj.get("content") or obj.get("reason")
    if t == "ask_user" and "text" not in obj:
        obj["text"] = obj.get("question")
    allowed = Action.__dataclass_fields__.keys()
    kwargs = {k: v for k, v in obj.items() if k in allowed and v is not None}
    kwargs.setdefault("coord_space", default_coord_space)
    return Action(type=t, keys=list(keys), **kwargs)


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

"""Anthropic Messages API 后端（不依赖 anthropic SDK，用标准库 urllib 直连 /v1/messages）。

两种用法：
1. AnthropicLLM：普通多模态 chat，可作为 planner / actor / verifier（输出 JSON 动作，走我们自己的 grounder）。
2. ClaudeComputerUseActor：Claude computer-use 风格的端到端 actor，声明 `computer_20250124` 工具，
   模型直接返回 tool_use（left_click / type / key / scroll ...），这里映射成统一 Action。
   截图按 Anthropic 参考实现（claude-quickstarts/computer-use-demo）的做法缩放到 XGA/WXGA/FWXGA 之一，
   模型给出的坐标在缩放后图像上，执行前乘回原始截图像素。

v0.3（审查条目 12）：`left_click_drag` 按 Anthropic computer-use 语义映射——给了 start_coordinate 就从它开始，
否则从**当前光标位置**开始（v0.2 把终点当起点，变成零长度拖拽）。光标位置优先取 Observation.cursor，
否则用本 actor 上一次发出的指针动作的落点；两者都未知时返回 ActionParseError 反馈给模型（不猜）。
缩放比例改由统一的 ImageTransform 表达（与 grounder / actor 共用一条变换链）。

简化说明（诚实标注）：参考实现会把每一步的 tool_result（含新截图）追加到同一段对话里；
这里为了与验证/恢复主循环解耦，每步重新发送“任务 + 子目标 + 历史文本 + 当前截图”，属于无状态调用。
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Any, Optional

from ..actions import Action
from ..coords import ImageTransform
from .base import Budget, LLMReply, image_to_b64, prepare_image, price_cost

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
COMPUTER_BETA = "computer-use-2025-01-24"
SCALING_TARGETS = [(1024, 768), (1280, 800), (1366, 768)]   # XGA / WXGA / FWXGA（参考实现 MAX_SCALING_TARGETS）


def scaling_target(w: int, h: int) -> tuple[int, int]:
    """与参考实现 scale_coordinates 相同：宽高比匹配（误差 <0.02）且比原图小时缩放，否则保持原尺寸。"""
    ratio = w / h
    for tw, th in SCALING_TARGETS:
        if abs(tw / th - ratio) < 0.02:
            if tw < w:
                return tw, th
            break
    return w, h


class AnthropicLLM:
    def __init__(self, model: str = "claude-sonnet-4-5", api_key_env: str = "ANTHROPIC_API_KEY",
                 base_url: Optional[str] = None, temperature: float = 0.0, max_tokens: int = 1024,
                 role: str = "llm", budget: Optional[Budget] = None, image_max_side: Optional[int] = 1568,
                 timeout: float = 120.0, betas: Optional[list[str]] = None,
                 price: Optional[tuple[float, float]] = None):
        self.model = model
        self.api_key = os.environ.get(api_key_env, "")
        self.url = (base_url.rstrip("/") + "/v1/messages") if base_url else API_URL
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.role = role
        self.budget = budget or Budget()
        self.image_max_side = image_max_side
        self.timeout = timeout
        self.betas = betas or []
        self.price = price
        self.last_transforms: tuple = ()

    # ---------------------------------------------------------------- 请求构造（可离线测试）
    def build_payload(self, system: str, text: str, images=None, tools: Optional[list] = None) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        tfs = []
        for im in images or []:
            _, tf = prepare_image(im, self.image_max_side)
            tfs.append(tf)
            b64, mt, _ = image_to_b64(im, self.image_max_side)
            content.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}})
        self.last_transforms = tuple(tfs)
        content.append({"type": "text", "text": text})
        p: dict[str, Any] = {"model": self.model, "max_tokens": self.max_tokens, "system": system,
                             "messages": [{"role": "user", "content": content}]}
        if self.temperature is not None:
            p["temperature"] = self.temperature
        if tools:
            p["tools"] = tools
        return p

    def headers(self, betas: Optional[list[str]] = None) -> dict[str, str]:
        h = {"content-type": "application/json", "x-api-key": self.api_key, "anthropic-version": API_VERSION}
        b = list(self.betas) + list(betas or [])
        if b:
            h["anthropic-beta"] = ",".join(b)
        return h

    def post(self, payload: dict, betas: Optional[list[str]] = None) -> dict:
        self.budget.before_call(self.role)          # 硬上限：触顶则请求不发出
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=self.headers(betas),
                                     method="POST")
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            resp = json.loads(r.read().decode())
        u = resp.get("usage", {}) or {}
        pt, ct = u.get("input_tokens", 0), u.get("output_tokens", 0)
        self.budget.add(self.role, pt, ct, time.time() - t0, price_cost(pt, ct, self.price))
        return resp

    @staticmethod
    def text_of(resp: dict) -> str:
        return "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")

    def chat(self, system: str, text: str, images=None) -> str:
        payload = self.build_payload(system, text, images)
        tfs = self.last_transforms
        return LLMReply(self.text_of(self.post(payload)), tfs)


# ---------------------------------------------------------------- computer-use 工具 → 统一 Action
_XDO_KEYS = {"return": "enter", "kp_enter": "enter", "escape": "esc", "super": "win", "super_l": "win",
             "control": "ctrl", "control_l": "ctrl", "ctrl_l": "ctrl", "alt_l": "alt", "shift_l": "shift",
             "page_down": "pagedown", "page_up": "pageup", "next": "pagedown", "prior": "pageup",
             "backspace": "backspace", "delete": "delete", "tab": "tab", "space": "space"}


def _keys(s: str) -> list[str]:
    out = []
    for k in s.replace(" ", "+").split("+"):
        if k:
            out.append(_XDO_KEYS.get(k.lower(), k.lower()))
    return out


def _xy(c, tf: Optional[ImageTransform], sx: float, sy: float) -> tuple[float, float]:
    from ..parsing import ActionParseError
    if not isinstance(c, (list, tuple)) or len(c) != 2 or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in c):
        raise ActionParseError("bad_type", f"coordinate must be [x, y] numbers, got {c!r}", "coordinate")
    if tf is not None:
        return tf.model_to_screenshot(c[0], c[1])
    return c[0] * sx, c[1] * sy


def tool_input_to_action(inp: dict[str, Any], sx: float = 1.0, sy: float = 1.0,
                         cursor: Optional[tuple[float, float]] = None,
                         transform: Optional[ImageTransform] = None) -> Action:
    """把 computer_20250124 的 tool_use.input 映射为统一 Action。

    坐标换算：优先用 transform（发送图 → 原始截图，pixel 约定）；否则用 (sx, sy) = 原图 / 发送图 的缩放比。
    cursor：当前光标位置（原始截图像素），left_click_drag 没有 start_coordinate 时作为起点。
    """
    from ..parsing import ActionParseError
    act = inp.get("action")
    c = inp.get("coordinate")
    xy = {}
    if c is not None:
        x, y = _xy(c, transform, sx, sy)
        xy = {"x": x, "y": y}
    mods = inp.get("text") if act in {"left_click", "right_click", "double_click", "triple_click", "middle_click"} else None
    if act in {"left_click", "middle_click"}:
        a = Action("click", **xy)
    elif act == "right_click":
        a = Action("right_click", **xy)
    elif act in {"double_click", "triple_click"}:
        a = Action("double_click", **xy)
    elif act == "mouse_move":
        a = Action("move", **xy)
    elif act == "left_click_drag":
        if c is None:
            raise ActionParseError("missing_field", "left_click_drag needs coordinate (end point)", "coordinate")
        if inp.get("start_coordinate") is not None:
            sx0, sy0 = _xy(inp["start_coordinate"], transform, sx, sy)
        elif cursor is not None:
            sx0, sy0 = cursor
        else:
            raise ActionParseError("missing_field", "left_click_drag without start_coordinate starts at the current "
                                   "cursor, which is unknown; give start_coordinate or mouse_move first",
                                   "start_coordinate")
        a = Action("drag", x=sx0, y=sy0, x2=xy["x"], y2=xy["y"])
    elif act == "type":
        a = Action("type", text=inp.get("text", ""))
    elif act in {"key", "hold_key"}:
        a = Action("hotkey", keys=_keys(inp.get("text", "")))
    elif act == "scroll":
        a = Action("scroll", direction=inp.get("scroll_direction", "down"),
                   amount=int(inp.get("scroll_amount", 3) or 3), **xy)
    elif act == "wait":
        a = Action("wait", seconds=float(inp.get("duration", 1)))
    elif act in {"screenshot", "cursor_position", "zoom"}:
        a = Action("wait", seconds=0.0, reason=f"claude requested {act}; re-observe")
    else:
        raise ActionParseError("unknown_action", f"unsupported computer-use action {act!r}", "action")
    if mods:
        a.keys = _keys(mods)  # 修饰键（例如 shift+click），记录在 keys 里供日志
    return a


CU_SYSTEM = """You are operating a {platform} computer through the `computer` tool to complete ONE sub-goal.
Take exactly one tool action per turn. When the sub-goal is complete, reply with the text DONE (no tool call).
If it is impossible, reply with FAIL: <reason>. If you need information only the user has, reply ASK: <question>."""


class ClaudeComputerUseActor:
    """端到端 actor：坐标由 Claude 直接给出（不经过我们的 grounder），验证 / 恢复 / 记忆照常生效。"""

    tool_version = "computer_20250124"

    def __init__(self, llm: AnthropicLLM, platform: str = "linux"):
        self.llm = llm
        self.platform = platform
        self.cursor: Optional[tuple[float, float]] = None     # 原始截图像素
        self.last_transform: Optional[ImageTransform] = None

    def build_request(self, task: str, sg, total: int, obs, history: str, milestones: str, feedback: str = ""):
        w, h = obs.screenshot.size
        tw, th = scaling_target(w, h)
        self.last_transform = ImageTransform((w, h), (tw, th), "pixel", dpi_scale=getattr(obs, "dpi_scale", 1.0))
        img = obs.screenshot if (tw, th) == (w, h) else obs.screenshot.resize((tw, th))
        tools = [{"type": self.tool_version, "name": "computer", "display_width_px": tw, "display_height_px": th}]
        text = (f"Overall task: {task}\nCurrent sub-goal ({sg.id}/{total}): {sg.goal}\n"
                f"Expected after this sub-goal: {sg.expected or '-'}\nDone milestones:\n{milestones}\n"
                f"Recent steps:\n{history}\n" + (f"Verifier feedback: {feedback}\n" if feedback else ""))
        old_side = self.llm.image_max_side
        self.llm.image_max_side = None  # 已经按参考实现缩放，不再二次缩放
        try:
            payload = self.llm.build_payload(CU_SYSTEM.format(platform=self.platform), text, [img], tools)
        finally:
            self.llm.image_max_side = old_side
        return payload, self.last_transform

    def parse_response(self, resp: dict, scale) -> tuple[Action, str]:
        """scale：build_request 返回的 ImageTransform（推荐），或旧式 (sx, sy) 缩放比。"""
        thought = AnthropicLLM.text_of(resp).strip()
        tf = scale if isinstance(scale, ImageTransform) else None
        sx, sy = (1.0, 1.0) if tf is not None else scale
        for b in resp.get("content", []):
            if b.get("type") == "tool_use" and b.get("name") == "computer":
                a = tool_input_to_action(b.get("input", {}) or {}, sx, sy, cursor=self.cursor, transform=tf)
                a.reason = thought
                self._track_cursor(a)
                return a, thought
        up = thought.upper()
        if up.startswith("FAIL"):
            return Action("fail", text=thought[5:].strip(), reason=thought), thought
        if up.startswith("ASK"):
            return Action("ask_user", text=thought[4:].strip(), reason=thought), thought
        return Action("done", text=thought, reason=thought), thought

    def _track_cursor(self, a: Action) -> None:
        if a.type == "drag" and a.x2 is not None:
            self.cursor = (a.x2, a.y2)
        elif a.x is not None and a.y is not None and a.type in {"click", "double_click", "right_click", "move", "scroll"}:
            self.cursor = (a.x, a.y)

    def next_action(self, task, sg, total, obs, history, milestones, feedback="", notes="(none)"):
        if getattr(obs, "cursor", None) is not None:      # 环境报告的真实光标位置优先
            self.cursor = tuple(obs.cursor)
        payload, scale = self.build_request(task, sg, total, obs, history, milestones, feedback)
        resp = self.llm.post(payload, betas=[COMPUTER_BETA])
        return self.parse_response(resp, scale)

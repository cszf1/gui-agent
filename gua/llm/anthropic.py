"""Anthropic Messages API 后端（不依赖 anthropic SDK，用标准库 urllib 直连 /v1/messages）。

两种用法：
1. AnthropicLLM：普通多模态 chat，可作为 planner / actor / verifier（输出 JSON 动作，走我们自己的 grounder）。
2. ClaudeComputerUseActor：Claude computer-use 风格的端到端 actor，声明 `computer_20250124` 工具，
   模型直接返回 tool_use（left_click / type / key / scroll ...），这里映射成统一 Action。
   截图按 Anthropic 参考实现（claude-quickstarts/computer-use-demo）的做法缩放到 XGA/WXGA/FWXGA 之一，
   模型给出的坐标在缩放后图像上，执行前乘回原始截图像素。

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
from .base import Budget, image_to_b64

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
                 timeout: float = 120.0, betas: Optional[list[str]] = None):
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

    # ---------------------------------------------------------------- 请求构造（可离线测试）
    def build_payload(self, system: str, text: str, images=None, tools: Optional[list] = None) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        for im in images or []:
            b64, mt, _ = image_to_b64(im, self.image_max_side)
            content.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}})
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
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=self.headers(betas),
                                     method="POST")
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            resp = json.loads(r.read().decode())
        u = resp.get("usage", {}) or {}
        self.budget.add(self.role, u.get("input_tokens", 0), u.get("output_tokens", 0), time.time() - t0)
        return resp

    @staticmethod
    def text_of(resp: dict) -> str:
        return "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")

    def chat(self, system: str, text: str, images=None) -> str:
        return self.text_of(self.post(self.build_payload(system, text, images)))


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


def tool_input_to_action(inp: dict[str, Any], sx: float = 1.0, sy: float = 1.0) -> Action:
    """把 computer_20250124 的 tool_use.input 映射为统一 Action；(sx, sy) = 原图 / 发送图 的缩放比。"""
    act = inp.get("action")
    c = inp.get("coordinate")
    xy = {"x": c[0] * sx, "y": c[1] * sy} if c else {}
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
        s = inp.get("start_coordinate") or c
        a = Action("drag", x=s[0] * sx, y=s[1] * sy, x2=c[0] * sx, y2=c[1] * sy)
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
        raise ValueError(f"unsupported computer-use action {act}")
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

    def build_request(self, task: str, sg, total: int, obs, history: str, milestones: str, feedback: str = ""):
        w, h = obs.screenshot.size
        tw, th = scaling_target(w, h)
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
        return payload, (w / tw, h / th)

    @staticmethod
    def parse_response(resp: dict, scale: tuple[float, float]) -> tuple[Action, str]:
        thought = AnthropicLLM.text_of(resp).strip()
        for b in resp.get("content", []):
            if b.get("type") == "tool_use" and b.get("name") == "computer":
                a = tool_input_to_action(b.get("input", {}), *scale)
                a.reason = thought
                return a, thought
        up = thought.upper()
        if up.startswith("FAIL"):
            return Action("fail", text=thought[5:].strip(), reason=thought), thought
        if up.startswith("ASK"):
            return Action("ask_user", text=thought[4:].strip(), reason=thought), thought
        return Action("done", text=thought, reason=thought), thought

    def next_action(self, task, sg, total, obs, history, milestones, feedback="", notes="(none)"):
        payload, scale = self.build_request(task, sg, total, obs, history, milestones, feedback)
        resp = self.llm.post(payload, betas=[COMPUTER_BETA])
        return self.parse_response(resp, scale)

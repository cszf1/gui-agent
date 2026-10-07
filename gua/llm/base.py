"""模型调用基础设施：预算记账 + 测试用脚本模型。

每次调用都记账（调用数、token、耗时、按角色），方向 A 的实验要求“相同预算下比较”。
"""
from __future__ import annotations

import base64
import io
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from PIL import Image


class BudgetExceeded(RuntimeError):
    pass


def image_to_b64(img: Image.Image, max_side: Optional[int] = None, fmt: str = "PNG") -> tuple[str, str, tuple[int, int]]:
    """返回 (base64, media_type, 发送给模型的图像尺寸)。"""
    if max_side and max(img.size) > max_side:
        img = img.copy()
        img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return base64.b64encode(buf.getvalue()).decode(), f"image/{fmt.lower()}", img.size


def image_to_data_url(img: Image.Image, max_side: Optional[int] = None, fmt: str = "PNG") -> str:
    b64, mt, _ = image_to_b64(img, max_side, fmt)
    return f"data:{mt};base64,{b64}"


@dataclass
class Budget:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    by_role: dict[str, int] = field(default_factory=dict)
    max_calls: Optional[int] = None
    max_tokens: Optional[int] = None

    def add(self, role: str, prompt_tokens: int = 0, completion_tokens: int = 0, dt: float = 0.0) -> None:
        self.calls += 1
        self.seconds += dt
        self.by_role[role] = self.by_role.get(role, 0) + 1
        self.prompt_tokens += prompt_tokens or 0
        self.completion_tokens += completion_tokens or 0

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def exhausted(self) -> Optional[str]:
        if self.max_calls is not None and self.calls >= self.max_calls:
            return f"model-call budget exhausted ({self.calls}/{self.max_calls})"
        if self.max_tokens is not None and self.tokens >= self.max_tokens:
            return f"token budget exhausted ({self.tokens}/{self.max_tokens})"
        return None


class ChatModel(Protocol):
    role: str
    budget: Budget

    def chat(self, system: str, text: str, images: Optional[list[Image.Image]] = None) -> str: ...


class ScriptedLLM:
    """测试 / 离线演示用：按顺序返回预设回复，或用函数 fn(system, text, images) 生成回复。"""

    def __init__(self, replies=None, fn: Optional[Callable[..., str]] = None, role: str = "scripted",
                 budget: Optional[Budget] = None):
        self.replies = list(replies or [])
        self.fn = fn
        self.role = role
        self.budget = budget or Budget()
        self.prompts: list[str] = []

    def chat(self, system: str, text: str, images=None) -> str:
        self.prompts.append(text)
        self.budget.add(self.role, 0, 0, 0.0)
        if self.fn:
            return self.fn(system, text, images)
        if not self.replies:
            raise RuntimeError("ScriptedLLM ran out of replies")
        return self.replies.pop(0)


def timed(fn: Callable[[], Any]) -> tuple[Any, float]:
    t0 = time.time()
    r = fn()
    return r, time.time() - t0

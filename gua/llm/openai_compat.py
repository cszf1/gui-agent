"""OpenAI 兼容的多模态客户端（Chat Completions）。

同一套代码可接：GPT-4o/4.1/5、Qwen2.5-VL / Qwen3-VL（阿里云百炼 DashScope 兼容模式）、
UI-TARS / OpenCUA / GUI-Owl（vLLM `--served-model-name` 部署后的 /v1 接口）、Ollama 等。

v0.3：
- 发送前缩放截图时记录 ImageTransform，回复以 LLMReply 返回（坐标换算用实际发送尺寸）。
- 调用前 Budget.before_call（硬上限）；可选 price=(输入, 输出) 美元/百万 token 计成本。
- GPT-5 / o 系列（推理模型）请求参数兼容：用 max_completion_tokens，不发 temperature。
  按模型名检测（见 is_reasoning_model）。⚠️ 未用真实 API 验证，只做了离线请求构造测试。
"""
from __future__ import annotations

import base64
import io
import os
import re
import time
from typing import Any, Optional

from .base import Budget, LLMReply, prepare_image, price_cost

_REASONING = re.compile(r"^(?:openai/|azure/)?(gpt-5|o\d)(?:[-.:_]|$)", re.I)


def is_reasoning_model(model: str) -> bool:
    """gpt-5 / gpt-5-mini / gpt-5.1 / o1 / o1-preview / o3 / o3-mini / o4-mini …"""
    return bool(_REASONING.match((model or "").strip()))


class OpenAICompatLLM:
    def __init__(self, model: str, base_url: Optional[str] = None, api_key_env: str = "OPENAI_API_KEY",
                 temperature: Optional[float] = 0.0, max_tokens: int = 1024, role: str = "llm",
                 budget: Optional[Budget] = None, image_max_side: Optional[int] = None,
                 extra_body: Optional[dict] = None, timeout: float = 120.0,
                 price: Optional[tuple[float, float]] = None, reasoning: Optional[bool] = None):
        from openai import OpenAI
        self.client = OpenAI(base_url=base_url or None, api_key=os.environ.get(api_key_env, "EMPTY"),
                             timeout=timeout)
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.role = role
        self.budget = budget or Budget()
        self.image_max_side = image_max_side
        self.extra_body = extra_body or {}
        self.price = price
        self.reasoning = is_reasoning_model(model) if reasoning is None else reasoning
        self.last_transforms: tuple = ()

    def build_messages(self, system: str, text: str, images=None) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        tfs = []
        for im in images or []:
            sent, tf = prepare_image(im, self.image_max_side)
            tfs.append(tf)
            buf = io.BytesIO()
            sent.save(buf, format="PNG")
            url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
            content.append({"type": "image_url", "image_url": {"url": url}})
        self.last_transforms = tuple(tfs)
        # 推理模型（o 系列）用 developer 角色代替 system；gpt-5 两者都接受，这里统一用 developer
        sys_role = "developer" if self.reasoning else "system"
        return [{"role": sys_role, "content": system}, {"role": "user", "content": content}]

    def request_kwargs(self, msgs: list[dict[str, Any]]) -> dict[str, Any]:
        kw: dict[str, Any] = {"model": self.model, "messages": msgs}
        if self.reasoning:
            kw["max_completion_tokens"] = self.max_tokens        # 推理模型不接受 max_tokens / temperature
        else:
            kw["max_tokens"] = self.max_tokens
            if self.temperature is not None:
                kw["temperature"] = self.temperature
        if self.extra_body:
            kw["extra_body"] = self.extra_body
        return kw

    def chat(self, system: str, text: str, images=None) -> str:
        self.budget.before_call(self.role)
        msgs = self.build_messages(system, text, images)
        tfs = self.last_transforms
        t0 = time.time()
        r = self.client.chat.completions.create(**self.request_kwargs(msgs))
        u = getattr(r, "usage", None)
        pt = getattr(u, "prompt_tokens", 0) if u else 0
        ct = getattr(u, "completion_tokens", 0) if u else 0
        self.budget.add(self.role, pt, ct, time.time() - t0, price_cost(pt, ct, self.price))
        return LLMReply(r.choices[0].message.content or "", tfs)

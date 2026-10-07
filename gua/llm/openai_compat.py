"""OpenAI 兼容的多模态客户端（Chat Completions）。

同一套代码可接：GPT-4o/4.1/5、Qwen2.5-VL / Qwen3-VL（阿里云百炼 DashScope 兼容模式）、
UI-TARS / OpenCUA / GUI-Owl（vLLM `--served-model-name` 部署后的 /v1 接口）、Ollama 等。
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

from .base import Budget, image_to_data_url


class OpenAICompatLLM:
    def __init__(self, model: str, base_url: Optional[str] = None, api_key_env: str = "OPENAI_API_KEY",
                 temperature: float = 0.0, max_tokens: int = 1024, role: str = "llm",
                 budget: Optional[Budget] = None, image_max_side: Optional[int] = None,
                 extra_body: Optional[dict] = None, timeout: float = 120.0):
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

    def build_messages(self, system: str, text: str, images=None) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for im in images or []:
            content.append({"type": "image_url", "image_url": {"url": image_to_data_url(im, self.image_max_side)}})
        return [{"role": "system", "content": system}, {"role": "user", "content": content}]

    def chat(self, system: str, text: str, images=None) -> str:
        msgs = self.build_messages(system, text, images)
        t0 = time.time()
        r = self.client.chat.completions.create(model=self.model, messages=msgs, temperature=self.temperature,
                                                max_tokens=self.max_tokens, extra_body=self.extra_body or None)
        u = getattr(r, "usage", None)
        self.budget.add(self.role, getattr(u, "prompt_tokens", 0) if u else 0,
                        getattr(u, "completion_tokens", 0) if u else 0, time.time() - t0)
        return r.choices[0].message.content or ""

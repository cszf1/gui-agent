"""模型后端：OpenAI 兼容（GPT / Qwen-VL@DashScope / UI-TARS@vLLM ...）+ Anthropic Messages API + 脚本模型。"""
from __future__ import annotations

from typing import Any, Optional

from .base import (Budget, BudgetExceeded, BudgetGate, ChatModel, LLMReply, ScriptedLLM, image_to_b64,
                   image_to_data_url, prepare_image, reply_transform)


def make_llm(spec: dict[str, Any], role: str, budget: Optional[Budget] = None):
    """spec 来自 YAML：{provider: openai|anthropic, model, base_url, api_key_env, ...}"""
    provider = spec.get("provider", "openai")
    price = spec.get("price")      # [输入, 输出] 美元/百万 token，可选（成本上限 agent.max_cost_usd 用）
    common = dict(model=spec["model"], temperature=spec.get("temperature", 0.0),
                  max_tokens=spec.get("max_tokens", 1024), role=role, budget=budget,
                  image_max_side=spec.get("image_max_side"), price=tuple(price) if price else None)
    if provider in {"openai", "openai_compat", "dashscope", "vllm"}:
        from .openai_compat import OpenAICompatLLM
        return OpenAICompatLLM(base_url=spec.get("base_url"), api_key_env=spec.get("api_key_env", "OPENAI_API_KEY"),
                               extra_body=spec.get("extra_body"), reasoning=spec.get("reasoning"), **common)
    if provider == "anthropic":
        from .anthropic import AnthropicLLM
        common["image_max_side"] = spec.get("image_max_side", 1568)
        return AnthropicLLM(base_url=spec.get("base_url"), api_key_env=spec.get("api_key_env", "ANTHROPIC_API_KEY"),
                            **common)
    raise ValueError(f"unknown provider {provider}")


__all__ = ["Budget", "BudgetExceeded", "BudgetGate", "ChatModel", "LLMReply", "ScriptedLLM", "image_to_b64",
           "image_to_data_url", "make_llm", "prepare_image", "reply_transform"]

"""反思器（Reflection）：失败 / 不确定之后，用一次模型调用总结“哪里错了、下一步换什么做法”。

参考：Agent S / S2 的 reflection agent、Mobile-Agent-v3 的 Reflector、OpenCUA 的 reflective CoT。
与验证器分工：验证器回答“这一步发生了什么”（verdict），反思器回答“接下来该怎么改”（策略建议），
产出写入 Memory.notes，并在下一次 actor 提示里出现。可在配置里关闭（消融：reflection.enabled=false）。
"""
from __future__ import annotations

from typing import Optional

from .parsing import extract_json

REFLECT_SYSTEM = "You are the reflection module of a GUI agent. Diagnose briefly and propose a concrete change."
REFLECT_PROMPT = """Task: {task}
Current sub-goal: {goal}
Recent steps (action => verdict (evidence) -> recovery):
{history}
Last failure: {failure}
In 1-2 sentences: what went wrong and what should the agent do differently next (different element,
keyboard shortcut, scroll first, close the dialog, wait for loading, ...)?
Reply JSON only: {{"diagnosis": "...", "advice": "..."}}"""


class Reflector:
    def __init__(self, llm=None, enabled: bool = True, on: tuple[str, ...] = ("no_effect", "failed", "uncertain", "blocked")):
        self.llm = llm
        self.enabled = enabled and llm is not None
        self.on = set(on)

    def should_reflect(self, verdict: str) -> bool:
        return self.enabled and verdict in self.on

    def reflect(self, task: str, goal: str, history: str, failure: str) -> Optional[str]:
        if not self.enabled:
            return None
        out = self.llm.chat(REFLECT_SYSTEM, REFLECT_PROMPT.format(task=task, goal=goal, history=history,
                                                                  failure=failure), None)
        try:
            obj = extract_json(out)
            return f"{obj.get('diagnosis', '')} -> {obj.get('advice', '')}".strip(" ->")
        except Exception:
            return out.strip()[:300] or None

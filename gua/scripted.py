"""脚本策略：不需要任何 API key 的“假 LLM”，用于集成测试、CI 与离线演示（`gua eval tasks/web --policy scripted`）。

它读取任务 JSON 里的 "demo" 字段，像真实模型一样只通过 **提示词文本** 与 agent 交互：
- planner：返回 demo.subgoals（含 expect_text，供 L1 规则核验）
- actor：解析提示词里的元素列表 `[id] role 'name'` 和验证器反馈，按脚本给出下一步 JSON 动作；
  若上一步被判为失败/遮挡/无效（反馈里出现 "Last action => <非 success>"），就重做当前脚本步骤；
  若屏幕上出现 interrupts 中声明的元素（例如弹窗的“接受”按钮），先处理它
- verifier（L2 桩）：步骤级一律乐观返回 success（真正起作用的是 L0/L1 规则与 expect_text 收尾核验）

所以脚本模式验证的是：环境后端、动作执行、L0/L1 验证、恢复策略、安全闸门、日志与报告这些“非模型”部分。
它**不能**说明真实模型的决策/定位能力。

demo 示例：
{"subgoals": [{"goal": "fill the form", "expect_text": "Submitted",
               "steps": [{"click": "Name"}, {"type": "Alice"}, {"click": "Submit"}]}],
 "interrupts": [{"if_visible": "Accept cookies", "click": "Accept cookies"}]}
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from .llm.base import ScriptedLLM

_EL = re.compile(r"^\[(\d+)\] (\S+) (['\"])(.*?)\3(.*)$", re.M)
_SG = re.compile(r"Current sub-goal \((\d+)/(\d+)\)")


def parse_elements(prompt: str) -> list[dict[str, Any]]:
    out = []
    for m in _EL.finditer(prompt):
        out.append({"id": int(m.group(1)), "role": m.group(2), "name": m.group(4), "flags": m.group(5)})
    return out


def find(elements: list[dict], name: str, role: Optional[str] = None) -> Optional[dict]:
    n = name.lower().strip()
    pool = [e for e in elements if e["role"] != "dialog" and (role is None or e["role"] == role)]
    for e in pool:
        if e["name"].lower().strip() == n:
            return e
    for e in pool:
        if n in e["name"].lower():
            return e
    return None


class ScriptedPolicy:
    def __init__(self, demo: dict[str, Any]):
        self.demo = demo
        self.subgoals = demo.get("subgoals", [])
        self.interrupts = demo.get("interrupts", [])
        self.ptr: dict[int, int] = {}       # 子目标序号 → 下一个脚本步骤下标
        self.last: dict[int, int] = {}      # 子目标序号 → 上一次发出的脚本步骤下标
        self.trace: list[str] = []

    # ---------------------------------------------------------------- planner
    def _sg_json(self, start: int = 0) -> str:
        return json.dumps({"subgoals": [{"goal": s["goal"], "expected": s.get("expected", ""),
                                         "evidence": s.get("evidence", ""), "expect_text": s.get("expect_text", "")}
                                        for s in self.subgoals[start:]]})

    def plan(self, system: str, text: str, images=None) -> str:
        if "The current sub-goal failed:" in text:
            failed = text.split("The current sub-goal failed:", 1)[1].split("\n", 1)[0].strip()
            for i, s in enumerate(self.subgoals):
                if s["goal"] == failed:
                    self.ptr.pop(i, None)
                    self.last.pop(i, None)
                    return self._sg_json(i)
        return self._sg_json(0)

    # ---------------------------------------------------------------- actor
    def act(self, system: str, text: str, images=None) -> str:
        m = _SG.search(text)
        sid = int(m.group(1)) - 1 if m else 0
        # replan 之后编号从失败子目标开始，按目标文本对齐
        gm = re.search(r"Current sub-goal \(\d+/\d+\): (.*)", text)
        if gm:
            for i, s in enumerate(self.subgoals):
                if s["goal"] == gm.group(1).strip():
                    sid = i
                    break
        els = parse_elements(text)
        failed_last = bool(re.search(r"Last action => (?!success|in_progress)", text)) or "could not be parsed" in text \
            or "Could not locate" in text
        # 1) 先处理打断（弹窗等）
        for it in self.interrupts:
            e = find(els, it["if_visible"])
            if e and "offscreen" not in e["flags"]:
                self.trace.append(f"interrupt:{it['click']}")
                tgt = find(els, it["click"]) or e
                if sid in self.last:
                    self.ptr[sid] = self.last[sid]   # 打断之后重做当前步骤
                return self._json({"type": "click", "element_id": tgt["id"]}, f"handle interrupt {it['click']}")
        steps = (self.subgoals[sid] if sid < len(self.subgoals) else {"steps": []}).get("steps", [])
        i = self.ptr.get(sid, 0)
        if failed_last and sid in self.last and "verification disagrees" not in text:
            i = self.last[sid]               # 上一步失败：重做
        if i >= len(steps):
            self.trace.append("done")
            return self._json({"type": "done", "text": self.subgoals[sid].get("answer", "")}, "script finished")
        self.last[sid], self.ptr[sid] = i, i + 1
        st = steps[i]
        self.trace.append(json.dumps(st, ensure_ascii=False))
        return self._json(self._step_to_action(st, els), f"script step {i}")

    def _step_to_action(self, st: dict, els: list[dict]) -> dict:
        for kind in ("click", "double_click", "right_click", "long_press"):
            if kind in st:
                e = find(els, st[kind], st.get("role"))
                return {"type": kind, "element_id": e["id"]} if e else {"type": kind, "target": st[kind]}
        if "type" in st:
            action = {"type": "type", "text": st["type"], "clear": st.get("clear", False), "submit": st.get("submit", False)}
            if st.get("target"):
                element = find(els, st["target"], "textbox")
                action.update({"element_id": element["id"]} if element else {"target": st["target"]})
            return action
        if "hotkey" in st:
            return {"type": "hotkey", "keys": st["hotkey"]}
        if "scroll" in st:
            return {"type": "scroll", "direction": st["scroll"], "amount": st.get("amount", 3)}
        if "wait" in st:
            return {"type": "wait", "seconds": st["wait"]}
        if "navigate" in st:
            return {"type": "navigate", "url": st["navigate"]}
        if "ask" in st:
            return {"type": "ask_user", "text": st["ask"]}
        if "back" in st:
            return {"type": "back"}
        if "raw" in st:
            return st["raw"]
        raise ValueError(f"bad demo step {st}")

    @staticmethod
    def _json(action: dict, thought: str) -> str:
        return json.dumps({"thought": thought, "action": action}, ensure_ascii=False)

    # ---------------------------------------------------------------- verifier 桩
    @staticmethod
    def verify(system: str, text: str, images=None) -> str:
        # 收尾核验（子目标 / 整任务）：脚本桩不能判断目标是否达成，一律 uncertain（v0.3：uncertain 不算完成）
        if "Sub-goal:" in text or "Whole task:" in text:
            return json.dumps({"verdict": "uncertain", "evidence": "scripted verifier cannot judge goals without expect_text"})
        return json.dumps({"verdict": "success", "evidence": "scripted L2 stub (optimistic)"})

    def llms(self) -> dict[str, Any]:
        return {"planner": ScriptedLLM(fn=self.plan, role="planner"),
                "actor": ScriptedLLM(fn=self.act, role="actor"),
                "verifier": ScriptedLLM(fn=self.verify, role="verifier"),
                "grounder": None, "reflector": None}

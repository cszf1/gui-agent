"""规划器（Planner）+ 执行决策器（Actor），跨平台提示词。

- Planner：把任务拆成带“预期结果 / 完成证据 / 期望文本”的子目标（方向 A 建议实现第 1 条；
  Agent S2 的 Proactive Hierarchical Planning：子目标失败时基于当前屏幕重规划剩余部分）。
- Actor：针对当前子目标，结合截图、统一无障碍元素列表、历史、里程碑、反思笔记，给出下一步动作。
  默认 Actor 只说“点什么”（element_id 或 target 描述），坐标交给 Grounder（Agent S / UGround 的
  planner–grounder 分离）；也可配置为直接输出坐标（end-to-end，coord_space 指定坐标约定）。
- UITarsActor：用 UI-TARS 原生提示词与输出格式的端到端 actor。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .actions import UNSUPPORTED, Action
from .env.base import Observation
from .parsing import extract_json, parse_model_action, parse_uitars

PLATFORM_DESC = {
    "windows": "a Windows desktop", "macos": "a macOS desktop", "linux": "a Linux (X11) desktop",
    "android": "an Android phone", "web": "a web browser page", "mock": "a simulated desktop",
}


@dataclass
class Subgoal:
    id: int
    goal: str
    expected: str = ""      # 完成后界面应该是什么样
    evidence: str = ""      # 如何确认真的完成（文件已保存、值已写入……）
    expect_text: str = ""   # 可选：完成后屏幕/无障碍树中必然出现的文字（L1 规则核验，零模型调用）


PLAN_SYSTEM = "You are a careful planner for a GUI agent operating {pdesc}."
PLAN_PROMPT = """Task: {task}
Current foreground: {window!r}{url}
Open windows/pages: {windows}

Break the task into 1-8 ordered sub-goals. Each must be checkable on screen.
If some text will certainly be visible once a sub-goal is done, put it in "expect_text" (else leave empty).
Reply JSON only:
{{"subgoals": [{{"goal": "...", "expected": "what the screen shows after it", "evidence": "how to confirm it is truly done", "expect_text": ""}}]}}"""

REPLAN_PROMPT = """Task: {task}
Completed milestones:
{milestones}
The current sub-goal failed: {goal}
Failure notes: {notes}
Lessons so far:
{lessons}
Current foreground: {window!r}{url}

Propose the remaining sub-goals from the CURRENT screen (use a different approach if needed).
Reply JSON only: {{"subgoals": [{{"goal": "...", "expected": "...", "evidence": "...", "expect_text": ""}}]}}"""

_ACTION_DOC = {
    "click": '{"type":"click"|"double_click"|"right_click"|"long_press"|"move", "element_id": <id>} or with "target":"<visible element description>"',
    "drag": '{"type":"drag", "target":"<start element>", "target2":"<end element>"}',
    "scroll": '{"type":"scroll", "direction":"up|down|left|right", "amount": 3, "target":"<area>"(optional)}',
    "type": '{"type":"type", "text":"...", "clear": false, "submit": false}   (types into the focused field; click it first)',
    "hotkey": '{"type":"hotkey", "keys":["ctrl","s"]}',
    "wait": '{"type":"wait", "seconds": 2}',
    "open_app": '{"type":"open_app", "app":"<app name / android package>"}',
    "navigate": '{"type":"navigate", "url":"https://..."}',
    "back": '{"type":"back"}',
    "home": '{"type":"home"}',
    "focus_window": '{"type":"focus_window", "text":"<window title substring>"}',
    "ask_user": '{"type":"ask_user", "text":"<question>"}   only if information is missing and only the user knows it',
    "done": '{"type":"done", "text":"<answer if the task asks a question>"}   when THIS sub-goal is complete',
    "fail": '{"type":"fail", "text":"<reason>"}   if impossible',
}
_COORD_DOC = ('Instead of element_id/target you MUST give the point directly as "x","y" in {space} coordinates '
              '({hint}).')
_COORD_HINT = {"norm1000": "0-1000 relative to the screenshot width/height", "norm1": "0-1 fractions",
               "pixel": "screenshot pixels", "resized": "pixels of the image you see"}

ACT_SYSTEM = """You operate {pdesc} with mouse/touch and keyboard to finish one sub-goal at a time.
Available actions (JSON):
{actions}
Rules: one action per reply. Prefer element_id when the element is in the list. Prefer keyboard shortcuts
when reliable. Never repeat an action that just had no effect; try something different. Elements marked
offscreen must be scrolled into view first. If a dialog blocks the screen, handle it first.
Reply JSON only: {{"thought": "...", "action": {{...}}}}"""

ACT_PROMPT = """Overall task: {task}
Current sub-goal ({sid}/{total}): {goal}
Expected after this sub-goal: {expected}
Done milestones:
{milestones}
Lessons (reflection):
{notes}
Recent steps in this sub-goal:
{history}
{feedback}
Foreground: {window!r}{url}
Interactive elements:
{elements}"""


def _url(obs: Observation) -> str:
    return f"\nURL: {obs.url}" if obs.url else ""


class Planner:
    def __init__(self, llm, platform: str = "windows"):
        self.llm = llm
        self.platform = platform

    def _sys(self) -> str:
        return PLAN_SYSTEM.format(pdesc=PLATFORM_DESC.get(self.platform, self.platform))

    def _parse(self, text: str, start_id: int = 1) -> list[Subgoal]:
        obj = extract_json(text)
        sgs = obj.get("subgoals") or obj.get("items") or []
        out = []
        for i, s in enumerate(sgs):
            if isinstance(s, str):
                s = {"goal": s}
            out.append(Subgoal(start_id + i, s.get("goal", ""), s.get("expected", ""), s.get("evidence", ""),
                               s.get("expect_text", "") or ""))
        return out or [Subgoal(start_id, "complete the task", "", "")]

    def plan(self, task: str, obs: Observation) -> list[Subgoal]:
        out = self.llm.chat(self._sys(), PLAN_PROMPT.format(
            task=task, window=obs.active_window, url=_url(obs), windows=obs.windows[:15]), [obs.screenshot])
        return self._parse(out)

    def replan(self, task: str, obs: Observation, failed: Subgoal, notes: str,
               milestones: str, next_id: int, lessons: str = "(none)") -> list[Subgoal]:
        out = self.llm.chat(self._sys(), REPLAN_PROMPT.format(
            task=task, milestones=milestones, goal=failed.goal, notes=notes, lessons=lessons,
            window=obs.active_window, url=_url(obs)), [obs.screenshot])
        return self._parse(out, next_id)


class Actor:
    def __init__(self, llm, platform: str = "windows", max_elements_in_prompt: int = 80,
                 coord_space: Optional[str] = None):
        self.llm = llm
        self.platform = platform
        self.max_el = max_elements_in_prompt
        self.coord_space = coord_space   # None = planner–grounder 分离；否则 actor 直接给坐标

    def system_prompt(self) -> str:
        bad = UNSUPPORTED.get(self.platform, set())
        lines = [doc for name, doc in _ACTION_DOC.items() if name not in bad]
        if self.coord_space:
            lines.append(_COORD_DOC.format(space=self.coord_space, hint=_COORD_HINT.get(self.coord_space, "")))
        return ACT_SYSTEM.format(pdesc=PLATFORM_DESC.get(self.platform, self.platform),
                                 actions="\n".join(" " + l for l in lines))

    def user_prompt(self, task, sg, total, obs, history, milestones, feedback="", notes="(none)") -> str:
        els = "\n".join(e.brief() for e in obs.elements[: self.max_el]) or "(not available, use vision)"
        fb = f"Feedback from verifier: {feedback}" if feedback else ""
        return ACT_PROMPT.format(task=task, sid=sg.id, total=total, goal=sg.goal, expected=sg.expected or "-",
                                 milestones=milestones, notes=notes, history=history, feedback=fb,
                                 window=obs.active_window, url=_url(obs), elements=els)

    def next_action(self, task: str, sg: Subgoal, total: int, obs: Observation, history: str,
                    milestones: str, feedback: str = "", notes: str = "(none)") -> tuple[Action, str]:
        out = self.llm.chat(self.system_prompt(),
                            self.user_prompt(task, sg, total, obs, history, milestones, feedback, notes),
                            [obs.screenshot])
        return parse_model_action(out, self.coord_space or "pixel")


UITARS_COMPUTER = """You are a GUI agent. You are given a task and your action history, with screenshots. You need to perform the next action to complete the task.

## Output Format
```
Thought: ...
Action: ...
```

## Action Space
click(start_box='<|box_start|>(x1,y1)<|box_end|>')
left_double(start_box='<|box_start|>(x1,y1)<|box_end|>')
right_single(start_box='<|box_start|>(x1,y1)<|box_end|>')
drag(start_box='<|box_start|>(x1,y1)<|box_end|>', end_box='<|box_start|>(x3,y3)<|box_end|>')
hotkey(key='')
type(content='') #If you want to submit your input, use "\\n" at the end of `content`.
scroll(start_box='<|box_start|>(x1,y1)<|box_end|>', direction='down or up or right or left')
wait() #Sleep for 5s and take a screenshot to check for any changes.
finished(content='xxx')
call_user() # Submit the task and call the user when the task is unsolvable, or when you need the user's help.

## Note
- Use {language} in `Thought` part.
- Write a small plan and finally summarize your next action (with its target element) in one sentence in `Thought` part.

## User Instruction
{instruction}
"""
UITARS_MOBILE = UITARS_COMPUTER.replace(
    "right_single(start_box='<|box_start|>(x1,y1)<|box_end|>')\n", "long_press(start_box='<|box_start|>(x1,y1)<|box_end|>')\n"
).replace("hotkey(key='')\n", "open_app(app_name='')\npress_home()\npress_back()\n")


class UITarsActor:
    """UI-TARS 原生端到端 actor（提示词改写自 bytedance/UI-TARS codes/ui_tars/prompt.py）。"""

    def __init__(self, llm, platform: str = "windows", coord_space: str = "resized", language: str = "Chinese"):
        self.llm = llm
        self.platform = platform
        self.coord_space = coord_space
        self.language = language

    def next_action(self, task, sg, total, obs, history, milestones, feedback="", notes="(none)"):
        tmpl = UITARS_MOBILE if self.platform == "android" else UITARS_COMPUTER
        instr = f"{task}\nCurrent sub-goal: {sg.goal}\nPrevious steps:\n{history}" + (f"\nFeedback: {feedback}" if feedback else "")
        out = self.llm.chat("You are a helpful assistant.", tmpl.format(language=self.language, instruction=instr),
                            [obs.screenshot])
        return parse_uitars(out, self.coord_space)

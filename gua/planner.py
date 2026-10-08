"""规划器（Planner）+ 执行决策器（Actor），跨平台提示词。

- Planner：把任务拆成带“预期结果 / 完成证据 / 期望文本”的子目标（方向 A 建议实现第 1 条；
  Agent S2 的 Proactive Hierarchical Planning：子目标失败时基于当前屏幕重规划剩余部分）。
- Actor：针对当前子目标，结合截图、统一无障碍元素列表、历史、里程碑、反思笔记，给出下一步动作。
  默认 Actor 只说“点什么”（element_id 或 target 描述），坐标交给 Grounder（Agent S / UGround 的
  planner–grounder 分离）；也可配置为直接输出坐标（end-to-end，coord_space 指定坐标约定）。
- UITarsActor：用 UI-TARS 原生提示词与输出格式的端到端 actor。

v0.3：
- 所有提示词按 CapabilityPolicy 组装（审查条目 6）：vision_only 下不出现元素 id / 名字 / 值 / 可见文本。
- Actor 输出坐标时，把模型回复携带的 ImageTransform（实际发送的图像尺寸）挂到 Action 上（审查条目 4）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .actions import UNSUPPORTED, Action
from .env.base import Observation
from .llm.base import reply_transform
from .parsing import extract_json, parse_model_action, parse_uitars
from .policy import DEFAULT_POLICY, CapabilityPolicy

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
    persistent: bool = True  # expect_text 在任务结束时是否仍应可见（任务收尾核验会重新检查所有 persistent 的 expect_text）
    postconditions: list = field(default_factory=list)   # v0.6：子目标级后置条件（规则核验，见 verify/postconditions.py）


PLAN_SYSTEM = "You are a careful planner for a GUI agent operating {pdesc}."
PLAN_PROMPT = """Task: {task}
Current foreground: {window!r}{url}
Open windows/pages: {windows}

Break the task into 1-8 ordered sub-goals. Each must be checkable on screen.
If some text will certainly be visible once a sub-goal is done, put it in "expect_text" (else leave empty).
Set "persistent": false if that text disappears later in the task (e.g. a transient toast).
Optionally add "postconditions": rule-checkable facts that must hold when the sub-goal is done, e.g.
[{{"kind":"element_state","name":"Subscribe","checked":true}}, {{"kind":"element_state","name":"Name","value":"Ada"}}].
Reply JSON only:
{{"subgoals": [{{"goal": "...", "expected": "what the screen shows after it", "evidence": "how to confirm it is truly done", "expect_text": "", "persistent": true}}]}}"""

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
_INVOKE_DOC = ('{"type":"invoke", "element_id": <id>, "method":"invoke|toggle|select|set_value|expand|collapse|'
               'scroll_into_view|focus", "text":"<value for set_value>"}   accessibility action on a listed element '
               '(methods in {braces} after an element); runs in the background without moving the mouse; prefer it '
               'over clicking when listed')
_EXPECT_DOC = ('Optional on any action: "expect":[{"kind":"text_appears","text":"..."}|{"kind":"element_state",'
               '"name":"...","checked":true}|{"kind":"text_disappears","text":"..."}|{"kind":"output_contains",'
               '"text":"..."}] = what must be true right after this action; it is checked and a mismatch is reported')
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
    def _sys(self) -> str:
        return PLAN_SYSTEM.format(pdesc=PLATFORM_DESC.get(self.platform, self.platform))

    def __init__(self, llm, platform: str = "windows", policy: CapabilityPolicy = DEFAULT_POLICY):
        self.llm = llm
        self.platform = platform
        self.policy = policy

    def _parse(self, text: str, start_id: int = 1, task: str = "") -> list[Subgoal]:
        try:
            obj = extract_json(text)
        except ValueError:
            obj = {}
        sgs = obj.get("subgoals") or obj.get("items") or []
        out = []
        for i, s in enumerate(sgs if isinstance(sgs, list) else []):
            if isinstance(s, str):
                s = {"goal": s}
            if not isinstance(s, dict):
                continue
            txt = lambda k: s.get(k) if isinstance(s.get(k), str) else ""   # noqa: E731
            pcs = s.get("postconditions")
            from .verify.postconditions import validate_postcondition
            pcs = [pc for pc in pcs if validate_postcondition(pc) is None][:8] if isinstance(pcs, list) else []
            out.append(Subgoal(start_id + len(out), txt("goal"), txt("expected"), txt("evidence"),
                               txt("expect_text"), s.get("persistent", True) is not False, pcs))
        return out or [Subgoal(start_id, task or "complete the task", "", "")]

    def _meta(self, obs: Observation) -> dict:
        if not self.policy.window_metadata:
            return {"window": "(hidden)", "url": "", "windows": "(hidden)"}
        return {"window": obs.active_window, "url": _url(obs), "windows": obs.windows[:15]}

    def plan(self, task: str, obs: Observation) -> list[Subgoal]:
        m = self._meta(obs)
        out = self.llm.chat(self._sys(), PLAN_PROMPT.format(task=task, **m), [obs.screenshot])
        return self._parse(out, 1, task)

    def replan(self, task: str, obs: Observation, failed: Subgoal, notes: str,
               milestones: str, next_id: int, lessons: str = "(none)") -> list[Subgoal]:
        m = self._meta(obs)
        out = self.llm.chat(self._sys(), REPLAN_PROMPT.format(
            task=task, milestones=milestones, goal=failed.goal, notes=notes, lessons=lessons,
            window=m["window"], url=m["url"]), [obs.screenshot])
        return self._parse(out, next_id, task)


class Actor:
    def __init__(self, llm, platform: str = "windows", max_elements_in_prompt: int = 80,
                 coord_space: Optional[str] = None, policy: CapabilityPolicy = DEFAULT_POLICY,
                 max_pixels: int = 1280 * 28 * 28):
        self.llm = llm
        self.platform = platform
        self.max_el = max_elements_in_prompt
        self.coord_space = coord_space   # None = planner–grounder 分离；否则 actor 直接给坐标
        self.policy = policy
        self.max_pixels = max_pixels     # coord_space=resized 时模型侧 smart_resize 的 max_pixels
        # v0.6：语义动作提示（env.semantic_methods）与工具通道说明（ToolRegistry.prompt_docs）；None/空 = 不出现
        self.semantic_hint = None
        self.extra_docs: list[str] = []
        # v0.7：Set-of-Mark——截图上画出编号框（编号 = element_id）；只在允许无障碍信息进入提示词时生效
        self.som = False

    def system_prompt(self) -> str:
        bad = UNSUPPORTED.get(self.platform, set())
        docs = dict(_ACTION_DOC)
        if self.platform in {"web", "windows"} and self.policy.a11y_in_prompts:
            docs["type"] = ('{"type":"type", "element_id": <textbox id>, "text":"...", "clear": true, '
                            '"submit": false} (focus and type in one verified action; prefer this for listed fields). '
                            'Omit element_id to type into the already focused field.')
        if not self.policy.a11y_in_prompts:     # 纯视觉：没有元素列表，也就没有 element_id
            docs["click"] = docs["click"].split(" or with ")[0].replace('"element_id": <id>', '"target":"<visible element description>"')
        lines = [doc for name, doc in docs.items() if name not in bad]
        if self.semantic_hint is not None and self.policy.a11y_in_prompts and "invoke" not in bad:
            lines.append(_INVOKE_DOC)
        lines += list(self.extra_docs or [])
        if self.semantic_hint is not None or self.extra_docs:
            lines.append(_EXPECT_DOC)
        if self.coord_space:
            lines.append(_COORD_DOC.format(space=self.coord_space, hint=_COORD_HINT.get(self.coord_space, "")))
        sysp = ACT_SYSTEM.format(pdesc=PLATFORM_DESC.get(self.platform, self.platform),
                                 actions="\n".join(" " + l for l in lines))
        if not self.policy.a11y_in_prompts:
            sysp = sysp.replace("Prefer element_id when the element is in the list. ", "Describe targets by what "
                                "is visible in the screenshot. ").replace("Elements marked\noffscreen must be "
                                                                         "scrolled into view first. ", "")
        return sysp

    def user_prompt(self, task, sg, total, obs, history, milestones, feedback="", notes="(none)") -> str:
        if self.policy.a11y_in_prompts:
            def line(e):
                b = e.brief()
                if self.semantic_hint is not None:
                    ms = sorted(self.semantic_hint(e) - {"focus", "scroll_into_view"})
                    if ms:
                        b += " {" + ",".join(ms) + "}"
                return b
            els = "\n".join(line(e) for e in obs.elements[: self.max_el]) or "(not available, use vision)"
        else:
            els = "(not provided: vision only, use the screenshot)"
        fb = f"Feedback from verifier: {feedback}" if feedback else ""
        meta = self.policy.window_metadata
        return ACT_PROMPT.format(task=task, sid=sg.id, total=total, goal=sg.goal, expected=sg.expected or "-",
                                 milestones=milestones, notes=notes, history=history, feedback=fb,
                                 window=obs.active_window if meta else "(hidden)", url=_url(obs) if meta else "",
                                 elements=els)

    def next_action(self, task: str, sg: Subgoal, total: int, obs: Observation, history: str,
                    milestones: str, feedback: str = "", notes: str = "(none)") -> tuple[Action, str]:
        image = obs.screenshot
        prompt = self.user_prompt(task, sg, total, obs, history, milestones, feedback, notes)
        if self.som and self.policy.a11y_in_prompts and obs.elements:
            from .som import render_som
            image = render_som(obs)
            prompt += ("\nThe screenshot shows numbered boxes on interactive elements; a box number is that "
                       "element's element_id. Prefer element_id over coordinates.")
        out = self.llm.chat(self.system_prompt(), prompt, [image])
        a, th = parse_model_action(out, self.coord_space or "pixel")
        attach_transform(a, out, self.max_pixels)
        return a, th


def attach_transform(a: Action, reply, max_pixels: int) -> None:
    """把模型回复里“实际发送的图像”的坐标变换挂到动作上（只在动作带模型坐标时有意义）。"""
    tf = reply_transform(reply)
    if tf is not None and (a.x is not None or a.x2 is not None):
        a.transform = tf.with_convention(a.coord_space, max_pixels)


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

    def __init__(self, llm, platform: str = "windows", coord_space: str = "resized", language: str = "Chinese",
                 max_pixels: int = 1280 * 28 * 28, policy: CapabilityPolicy = DEFAULT_POLICY):
        self.llm = llm
        self.platform = platform
        self.coord_space = coord_space
        self.language = language
        self.max_pixels = max_pixels
        self.policy = policy      # UI-TARS 提示词本来就只有截图 + 历史，不含无障碍信息

    def next_action(self, task, sg, total, obs, history, milestones, feedback="", notes="(none)"):
        tmpl = UITARS_MOBILE if self.platform == "android" else UITARS_COMPUTER
        instr = f"{task}\nCurrent sub-goal: {sg.goal}\nPrevious steps:\n{history}" + (f"\nFeedback: {feedback}" if feedback else "")
        out = self.llm.chat("You are a helpful assistant.", tmpl.format(language=self.language, instruction=instr),
                            [obs.screenshot])
        a, th = parse_uitars(out, self.coord_space)
        attach_transform(a, out, self.max_pixels)
        return a, th

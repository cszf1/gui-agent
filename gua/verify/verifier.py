"""方向 A 的核心：区分“动作已发出 / 界面已变化 / 子目标确实完成”。跨平台通用。

三级证据，从便宜到贵：
  L0 执行层：ExecResult 是否报错（越界、窗口不存在、不支持的动作、被安全策略拦截）
  L1 规则层：界面是否稳定；前台窗口/页面是否还是任务窗口；是否新出现了对话框（遮挡）；
             无障碍树证据（输入框的值是否包含刚输入的文本、复选框状态是否翻转、期望文本是否出现）；
             动作点附近/全局像素是否变化；URL / 前台应用是否变化
  L2 模型层：把前后截图拼图交给 VLM，按“预期结果”给出 verdict + 证据

触发策略 (trigger) 用于消融实验：
  "none"       不验证（原始执行循环基线）
  "every_step" 每步都调 L2（固定每步验证基线）
  "on_event"   只有 L1 判断不了、或子目标收尾时才调 L2（本项目主方法）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ..actions import Action
from ..env.base import ExecResult, Observation
from ..parsing import extract_json
from .diff import frame_diff, region_diff, side_by_side


class Verdict(str, Enum):
    SUCCESS = "success"          # 有证据表明达到预期
    IN_PROGRESS = "in_progress"  # 还在加载/动画，应等待而不是重试
    NO_EFFECT = "no_effect"      # 界面没变化（点空、被遮挡、失焦）
    FAILED = "failed"            # 有变化但不是预期（点错、打开了别的东西）/ 执行层错误
    BLOCKED = "blocked"          # 弹窗/对话框挡住
    UNCERTAIN = "uncertain"      # 证据不足，禁止盲目重复点击


@dataclass
class Check:
    verdict: Verdict
    evidence: str = ""
    level: str = "L1"
    signals: dict = field(default_factory=dict)


STEP_SYSTEM = "You verify the effect of one GUI action on a {platform} device. Be strict and evidence-based."
STEP_PROMPT = """Left: screenshot BEFORE the action. Right: AFTER (red circle = action point, if any).
Action: {action}
Expected result of this step: {expected}
Foreground before: {win_before!r}; after: {win_after!r}

Classify the outcome with one label:
- success: visible evidence that the expected result happened
- in_progress: loading / animation / partially rendered, should wait
- no_effect: nothing relevant changed
- failed: something changed but it is NOT the expected result (wrong item, wrong menu, wrong window)
- blocked: a dialog / popup / permission prompt is covering the target
- uncertain: cannot tell from the screenshots
Reply JSON only: {{"verdict": "<label>", "evidence": "<what you see that supports it>"}}"""

GOAL_PROMPT = """Screenshot of the current screen.
Sub-goal: {goal}
Completion evidence required: {evidence}
Visible text / accessibility summary (may be partial):
{text}
Is the sub-goal truly complete RIGHT NOW (e.g. file saved, value actually entered, dialog closed)?
Do not trust earlier history; judge only from the current screen.
Reply JSON only: {{"verdict": "success" | "failed" | "uncertain", "evidence": "<observation>"}}"""

BUSY_RE = re.compile(r"(\bloading\b|please wait|\bprocessing\b|\bsaving\.\.\.|加载中|正在加载|请稍候|处理中|正在保存)", re.I)
_CHANGE_ACTIONS = {"click", "double_click", "right_click", "long_press", "type", "hotkey", "drag"}


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


class Verifier:
    def __init__(self, llm=None, trigger: str = "on_event", local_thresh: float = 0.01,
                 global_thresh: float = 0.003, use_a11y: bool = True, platform: str = "desktop"):
        self.llm = llm
        self.trigger = trigger
        self.local_thresh = local_thresh
        self.global_thresh = global_thresh
        self.use_a11y = use_a11y
        self.platform = platform

    # ---------------------------------------------------------------- L0 + L1
    def rule_check(self, before: Observation, after: Observation, action: Action, exec_res: ExecResult,
                   stable: bool, task_window: str = "", expect_text: Optional[str] = None) -> Check:
        if not exec_res.ok:
            sig0 = {"exec_error": exec_res.error, "screen": list(before.screen_size)}
            if action.point:
                sig0["point"] = list(action.point)
            return Check(Verdict.FAILED, f"exec error: {exec_res.error}", "L0", sig0)
        g = frame_diff(before.screenshot, after.screenshot)
        pt = action.point
        loc = region_diff(before.screenshot, after.screenshot, pt) if pt else 0.0
        sig = {"global_diff": round(g, 4), "local_diff": round(loc, 4), "stable": stable,
               "win_before": before.active_window, "win_after": after.active_window,
               "screen": list(after.screen_size)}
        if pt:
            sig["point"] = list(pt)
        if before.url or after.url:
            sig["url_before"], sig["url_after"] = before.url, after.url

        # 1) 焦点：前台窗口不再是任务窗口（被抢焦点 / 最小化 / 切到别的应用）
        if task_window and task_window.lower() not in (after.active_window or "").lower():
            new_win = after.active_window not in before.windows
            sig["focus_lost"] = True
            return Check(Verdict.BLOCKED if new_win else Verdict.FAILED,
                         f"foreground is {after.active_window!r}, not task window {task_window!r}", "L1", sig)
        # 2) 新出现的对话框（意外弹窗遮挡）
        if self.use_a11y and after.dialogs():
            before_d = {(_norm(d.name), d.rect) for d in before.dialogs()}
            new_d = [d for d in after.dialogs() if (_norm(d.name), d.rect) not in before_d]
            if new_d and action.type in _CHANGE_ACTIONS:
                sig["new_dialog"] = new_d[0].name
                # 若动作本身就是去“打开”这个对话框，则交给后续规则/模型判断
                if not (expect_text and expect_text.lower() in _norm(new_d[0].name)):
                    return Check(Verdict.BLOCKED, f"a dialog appeared: {new_d[0].name!r}", "L1", sig)
        # 3) 被遮挡：点击位置上的元素在点击前就被别的层盖住（Web 的 covered 标记）
        if self.use_a11y and pt and action.type in {"click", "double_click"}:
            el = before.element_at(*pt)
            if el is not None and el.attrs.get("covered") == "true":
                sig["covered_target"] = el.name
                if before.dialogs():
                    return Check(Verdict.BLOCKED, f"target {el.name!r} is covered by a dialog/overlay", "L1", sig)
        # 4) 不稳定 / 忙碌指示（Loading… / progressbar / aria-busy）→ 等待，而不是重复动作。
        #    小号 spinner 的像素变化常低于全局阈值，所以还要看文字与无障碍树里的忙碌信号。
        if not stable:
            return Check(Verdict.IN_PROGRESS, "screen still changing", "L1", sig)
        if self.use_a11y:
            busy = is_busy(after)
            if busy and busy_count(after) > busy_count(before):
                sig["busy"] = busy
                return Check(Verdict.IN_PROGRESS, f"busy indicator visible: {busy!r}", "L1", sig)
        # 5) 无障碍树正面证据
        if self.use_a11y:
            if expect_text and expect_text.lower() in after.all_text().lower():
                return Check(Verdict.SUCCESS, f"accessibility tree shows {expect_text!r}", "L1", sig)
            if action.type == "type" and action.text:
                hit = [e for e in after.elements if e.role in {"textbox", "combobox"}
                       and action.text in (e.value or "")]
                if hit:
                    return Check(Verdict.SUCCESS, f"field {hit[0].name!r} now contains the typed text", "L1", sig)
            if pt and action.type == "click":
                eb = before.element_at(*pt)
                if eb is not None and eb.checked is not None:
                    ea = next((e for e in after.elements if e.role == eb.role and e.name == eb.name), None)
                    if ea is not None and ea.checked is not None and ea.checked != eb.checked:
                        return Check(Verdict.SUCCESS, f"{eb.role} {eb.name!r} toggled to {ea.checked}", "L1", sig)
        # 6) 动作类型相关的便宜判定
        if action.type == "wait":
            return Check(Verdict.SUCCESS, "wait completed", "L1", sig)
        if action.type in {"navigate", "back", "open_app", "home", "focus_window"}:
            changed = (after.url != before.url) or (after.active_window != before.active_window) or g > self.global_thresh
            return Check(Verdict.SUCCESS if changed else Verdict.NO_EFFECT,
                         "location/foreground changed" if changed else "nothing changed after navigation", "L1", sig)
        if action.type == "scroll":
            moved = g > self.global_thresh
            return Check(Verdict.SUCCESS if moved else Verdict.NO_EFFECT,
                         "content scrolled" if moved else "scroll had no effect (end of content?)", "L1", sig)
        if action.type in _CHANGE_ACTIONS and g < self.global_thresh and loc < self.local_thresh:
            return Check(Verdict.NO_EFFECT, "no pixel change after action", "L1", sig)
        return Check(Verdict.UNCERTAIN, "screen changed; effect not confirmed by rules", "L1", sig)

    # ---------------------------------------------------------------- L2
    def model_check(self, before: Observation, after: Observation, action: Action, expected: str) -> Check:
        if self.llm is None:
            return Check(Verdict.UNCERTAIN, "no verifier model", "L2")
        mark = action.point
        img = side_by_side(before.screenshot, after.screenshot, mark)
        out = self.llm.chat(STEP_SYSTEM.format(platform=self.platform), STEP_PROMPT.format(
            action=action.short(), expected=expected or "(not specified)",
            win_before=before.active_window, win_after=after.active_window), [img])
        return _parse_check(out)

    def check_step(self, before: Observation, after: Observation, action: Action, exec_res: ExecResult,
                   stable: bool, expected: str, task_window: str = "", expect_text: Optional[str] = None) -> Check:
        if self.trigger == "none":
            return Check(Verdict.SUCCESS if exec_res.ok else Verdict.FAILED,
                         "verification disabled" if exec_res.ok else exec_res.error, "off",
                         {"exec_error": exec_res.error} if not exec_res.ok else {})
        rc = self.rule_check(before, after, action, exec_res, stable, task_window, expect_text)
        if self.trigger == "every_step" and rc.level != "L0":
            mc = self.model_check(before, after, action, expected)
            mc.signals = rc.signals
            return mc
        # on_event：规则能定论的直接返回；只有 UNCERTAIN 才花一次模型调用
        if rc.verdict == Verdict.UNCERTAIN:
            mc = self.model_check(before, after, action, expected)
            mc.signals = rc.signals
            return mc
        return rc

    def check_goal(self, obs: Observation, goal: str, evidence: str, expect_text: Optional[str] = None) -> Check:
        """子目标/任务收尾核验：只看“现在”的屏幕，防止用旧证据宣告完成（任务状态失配）。

        先走 L1：若子目标给了 expect_text，且当前无障碍树/可见文本里出现 → 直接成功（零模型调用）。
        """
        if self.trigger == "none":
            return Check(Verdict.SUCCESS, "goal verification disabled", "off")
        if expect_text and self.use_a11y:
            if expect_text.lower() in obs.all_text().lower():
                return Check(Verdict.SUCCESS, f"current screen shows {expect_text!r}", "goal-L1")
            if self.llm is None:
                return Check(Verdict.FAILED, f"{expect_text!r} not found on current screen", "goal-L1")
        if self.llm is None:
            return Check(Verdict.UNCERTAIN, "no verifier model and no rule evidence", "goal-L1")
        out = self.llm.chat(STEP_SYSTEM.format(platform=self.platform),
                            GOAL_PROMPT.format(goal=goal, evidence=evidence or goal, text=obs.all_text()[:1500]),
                            [obs.screenshot])
        c = _parse_check(out)
        c.level = "goal-L2"
        return c


def is_busy(obs: Observation) -> str:
    """返回忙碌指示文本（空串表示不忙）。"""
    for e in obs.elements:
        if e.native_role in {"progressbar", "ProgressBar", "AXProgressIndicator", "AXBusyIndicator"} or \
                e.attrs.get("aria-busy") == "true":
            return e.name or e.native_role
    m = BUSY_RE.search(obs.text or "")
    return m.group(0) if m else ""


def busy_count(obs: Observation) -> int:
    n = sum(1 for e in obs.elements if e.native_role in {"progressbar", "ProgressBar", "AXProgressIndicator",
                                                          "AXBusyIndicator"} or e.attrs.get("aria-busy") == "true")
    return n + len(BUSY_RE.findall(obs.text or ""))


def _parse_check(text: str) -> Check:
    try:
        obj = extract_json(text)
        v = Verdict(str(obj.get("verdict", "uncertain")).lower())
        return Check(v, str(obj.get("evidence", "")), "L2")
    except Exception:
        return Check(Verdict.UNCERTAIN, f"unparseable verifier output: {text[:120]!r}", "L2")

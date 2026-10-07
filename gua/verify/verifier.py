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

v0.3：
- 是否允许 L2、L1 是否用无障碍证据、提示词里能否放可见文本，统一由 CapabilityPolicy 决定（审查条目 6）。
- 新增 check_final：任务收尾核验 = 重新规则核验每个子目标的 expect_text + （需要时）整任务聚合 L2；
  只有明确的 success 才算成功，uncertain / 无法解析一律不是成功（审查条目 2）。

v0.3.1（第二轮审查条目 3 / 7；复审修复“旧证据洗白 / 持续 Loading”）：
- L2 请求里的动作描述由调用方传入安全摘要（action_desc），不再直接用 action.short()（密码不进模型请求）。
- 旧证据（新鲜度）判定只看“承载 expect_text 的片段本身”（元素 name/value，或 obs.text 里的独立文本行），
  不再看全局像素差，也不再对比整体 all_text——避免不同元素被 all_text 拼在一起时误判同一证据发生了变化。
  有 baseline 时，只有这些片段被替换（例如 "Report ready (yesterday)" → "Report ready (today)"）才算新鲜；
  旧证据仍完整保留（旁边多了 banner、同一行附加了无关文字、重复出现同一行）一律仍算旧证据。
  旧证据不参与 L1 成功，交给 L2 并在提示词里显式标注它可能是旧证据；L2 不可用 → UNCERTAIN（绝不 success）。
- 忙碌判定是当前屏幕的绝对信号：未满 progressbar / aria-busy / 整行状态文字（Loading…）→ UNCERTAIN；
  不再用 baseline 做“计数不增加”的豁免（持久 Loading 必须始终算忙）。说明性段落（“Loading takes a few
  seconds”）不算忙碌：按整行状态标签匹配，而不是在任意文本里搜关键字。
- 静态的满进度条（aria-valuenow == aria-valuemax）仍不算忙碌。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ..actions import Action
from ..env.a11y import PROGRESS_ROLES
from ..env.base import ExecResult, Observation
from ..parsing import extract_json
from ..policy import CapabilityPolicy
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
{text_block}
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
{stale_block}Is the sub-goal truly complete RIGHT NOW (e.g. file saved, value actually entered, dialog closed)?
Do not trust earlier history; judge only from the current screen.
Reply JSON only: {{"verdict": "success" | "failed" | "uncertain", "evidence": "<observation>"}}"""

FINAL_PROMPT = """Screenshot of the current screen, at the END of the task.
Whole task: {task}
The task was split into these sub-goals; ALL of them must hold on the current screen:
{subgoals}
{text_block}{stale_block}Is the WHOLE task complete RIGHT NOW? A sub-goal that was done earlier but was later undone
(e.g. a setting reverted, a dialog discarded the change) means the task is NOT complete.
Do not trust earlier history; judge only from the current screen.
Reply JSON only: {{"verdict": "success" | "failed" | "uncertain", "evidence": "<observation per sub-goal>"}}"""

# 忙碌状态标签：整行就是一个状态词（可带省略号/点），而不是在任意文本里搜关键字。
# 这样既能命中真正的 "Loading..." / "正在保存"，又不会把 "Loading takes a few seconds" 这类说明段落当忙碌。
BUSY_LINE_RE = re.compile(
    r"^(?:loading|please wait|processing|saving|加载中|正在加载|请稍候|处理中|正在保存)"
    r"(?:[\s:,]+(?:loading|please wait|processing|saving|results|data|changes|正在加载|请稍候|处理中))?"
    r"[\s:,]*(?:\d{1,3}\s*%|\d+\s*(?:/|of)\s*\d+)?"
    r"[\s:.…]*$", re.I)
_CHANGE_ACTIONS = {"click", "double_click", "right_click", "long_press", "type", "hotkey", "drag"}


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def _evidence_fragments(obs: Observation, expect_text: str) -> list[str]:
    """承载 expect_text 的独立片段：obs.text 里以换行分隔、包含它的行，以及元素的 name/value。

    只比较这些片段本身，避免 all_text 把不同元素拼在一起后误判“同一证据发生了变化”。
    """
    low = expect_text.lower()
    frags: list[str] = []
    for line in (obs.text or "").splitlines():
        if low in line.lower():
            frags.append(_norm(line))
    if obs.active_window and low in obs.active_window.lower():
        frags.append(_norm(obs.active_window))
    for e in obs.elements:
        if low in (e.name or "").lower():
            frags.append(_norm(e.name))
        if e.value and not e.is_password and low in e.value.lower():
            frags.append(_norm(e.value))
    return [f for f in frags if f]


def _goal_stale_block(expect_text: str) -> str:
    return (f"IMPORTANT: the expected text {expect_text!r} was already visible on the screen before this "
            f"sub-goal started and is unchanged since — this may be OLD/stale evidence.\n"
            f"Do NOT treat its mere presence as a newly achieved result; answer success only if you can see a "
            f"real state change proving it was just produced.\n")


def _final_stale_block(sg_ids: list) -> str:
    return (f"IMPORTANT: the expected text of sub-goal(s) {sg_ids} was already visible before the task "
            f"progressed and is unchanged since — this may be OLD/stale evidence.\n"
            f"Do NOT treat their mere presence as a newly achieved result; answer success only if you can see a "
            f"real state change proving they were just produced.\n")


class Verifier:
    def __init__(self, llm=None, trigger: str = "on_event", local_thresh: float = 0.01,
                 global_thresh: float = 0.003, use_a11y: bool = True, platform: str = "desktop",
                 policy: Optional[CapabilityPolicy] = None):
        self.llm = llm
        self.trigger = trigger
        self.local_thresh = local_thresh
        self.global_thresh = global_thresh
        self.use_a11y = use_a11y
        self.platform = platform
        self.llm_step = self.llm_goal = True
        self.a11y_prompt = use_a11y
        if policy is not None:
            self.trigger = policy.step_trigger
            self.use_a11y = policy.a11y_rules
            self.llm_step, self.llm_goal = policy.llm_step_verify, policy.llm_goal_verify
            self.a11y_prompt = policy.a11y_in_prompts
        self.policy = policy

    def _screen_text(self, obs: Observation) -> str:
        return obs.all_text()[:1500] if self.a11y_prompt else "(not provided: vision only)"

    def busy(self, obs: Observation) -> str:
        """忙碌指示（只在允许使用无障碍 / 文本规则时判断；纯视觉消融下返回空串）。

        忙碌是“当前屏幕”的绝对信号：未满 progressbar / aria-busy，或整行状态文字（Loading… 等）。
        不再拿 baseline 做计数豁免——基线里也有同样的 Loading 不能说明现在不忙（持久 Loading 必须算忙）。
        """
        if not self.use_a11y:
            return ""
        return is_busy(obs)

    def _stale_evidence(self, baseline: Observation, obs: Observation, expect_text: str) -> bool:
        """旧证据判断：expect_text 在 baseline 里就出现过，且现在仍是“同一份”证据 → 不算新鲜。

        判据只看承载 expect_text 的片段：只要当前还有某个片段完整保留了 baseline 的某个片段（相等或互为子串），
        就说明旧证据仍在（例如旁边多了 banner、同一行附加了无关文字、重复出现同一行）。
        只有当这些片段被替换成别的内容（例如 "(yesterday)" → "(today)"）才算真的发生了变化。
        """
        cur = _evidence_fragments(obs, expect_text)
        if not cur:
            return False
        base = _evidence_fragments(baseline, expect_text)
        if not base:
            return False
        return any(bf in cf or cf in bf for bf in base for cf in cur)

    def _not_settled(self, obs: Observation, stable: bool, level: str) -> Optional[Check]:
        if not stable:
            return Check(Verdict.UNCERTAIN, "screen still changing at verification time; not judged complete",
                         level, {"stable": False})
        b = self.busy(obs)
        if b:
            return Check(Verdict.UNCERTAIN, f"busy indicator still visible ({b!r}); not judged complete", level,
                         {"busy": b})
        return None

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
        #    忙碌按“当前屏幕”绝对判断，不因动作前也忙而豁免。
        if not stable:
            return Check(Verdict.IN_PROGRESS, "screen still changing", "L1", sig)
        if self.use_a11y:
            busy = is_busy(after)
            if busy:
                sig["busy"] = busy
                return Check(Verdict.IN_PROGRESS, f"busy indicator visible: {busy!r}", "L1", sig)
        # 5) 无障碍树正面证据
        if self.use_a11y:
            if expect_text and expect_text.lower() in after.all_text().lower():
                if not self._stale_evidence(before, after, expect_text):
                    return Check(Verdict.SUCCESS, f"accessibility tree shows {expect_text!r}", "L1", sig)
                sig["stale_expect_text"] = True      # 动作前就有、且未发生实质替换：不能作为这一步的成功证据
            if action.type == "type" and action.text:
                hit = [e for e in after.elements if e.role in {"textbox", "combobox"} and not e.is_password
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
    def model_check(self, before: Observation, after: Observation, action: Action, expected: str,
                    action_desc: Optional[str] = None) -> Check:
        if self.llm is None:
            return Check(Verdict.UNCERTAIN, "no verifier model", "L2")
        if not self.llm_step:
            return Check(Verdict.UNCERTAIN, "L2 verification disabled by policy (rules only)", "L1")
        mark = action.point
        img = side_by_side(before.screenshot, after.screenshot, mark)
        tb_before = (before.text or "")[:400]
        tb_after = (after.text or "")[:400]
        text_block = ""
        if tb_before or tb_after:
            text_block = f"Visible text before:\n{tb_before}\nVisible text after:\n{tb_after}\n"
        out = self.llm.chat(STEP_SYSTEM.format(platform=self.platform), STEP_PROMPT.format(
            action=action_desc if action_desc is not None else action.safe_short(before),
            expected=expected or "(not specified)",
            win_before=before.active_window, win_after=after.active_window,
            text_block=text_block), [img])
        return _parse_check(out)

    def check_step(self, before: Observation, after: Observation, action: Action, exec_res: ExecResult,
                   stable: bool, expected: str, task_window: str = "", expect_text: Optional[str] = None,
                   action_desc: Optional[str] = None) -> Check:
        if self.trigger == "none":
            return Check(Verdict.SUCCESS if exec_res.ok else Verdict.FAILED,
                         "verification disabled" if exec_res.ok else exec_res.error, "off",
                         {"exec_error": exec_res.error} if not exec_res.ok else {})
        rc = self.rule_check(before, after, action, exec_res, stable, task_window, expect_text)
        if self.trigger == "every_step" and rc.level != "L0" and self.llm_step and self.llm is not None:
            mc = self.model_check(before, after, action, expected, action_desc)
            mc.signals = rc.signals
            return mc
        # on_event：规则能定论的直接返回；只有 UNCERTAIN 才花一次模型调用
        if rc.verdict == Verdict.UNCERTAIN:
            mc = self.model_check(before, after, action, expected, action_desc)
            mc.signals = rc.signals
            return mc
        return rc

    def check_goal(self, obs: Observation, goal: str, evidence: str, expect_text: Optional[str] = None,
                   stable: bool = True, baseline: Optional[Observation] = None) -> Check:
        """子目标/任务收尾核验：只看“现在”的屏幕，防止用旧证据宣告完成（任务状态失配）。

        先走 L1：若子目标给了 expect_text，且当前无障碍树/可见文本里出现、并且它不是 baseline 里就有的
        旧证据 → 直接成功（零模型调用）。v0.3.1：未稳定 / 忙碌 → UNCERTAIN；旧证据不参与 L1 成功，交给 L2
        并在提示词里显式标注；L2 不可用 → UNCERTAIN（绝不 success）。
        """
        if self.trigger == "none":
            return Check(Verdict.SUCCESS, "goal verification disabled", "off")
        ns = self._not_settled(obs, stable, "goal-L1")
        if ns:
            return ns
        stale = ""
        stale_block = ""
        if expect_text and self.use_a11y:
            low = expect_text.lower()
            if low in obs.all_text().lower():
                if baseline is not None and self._stale_evidence(baseline, obs, expect_text):
                    stale = (f"{expect_text!r} was already visible before this sub-goal started and is still the "
                             f"same evidence (stale)")
                    stale_block = _goal_stale_block(expect_text)
                else:
                    return Check(Verdict.SUCCESS, f"current screen shows {expect_text!r}", "goal-L1")
            elif self.llm is None or not self.llm_goal:
                return Check(Verdict.FAILED, f"{expect_text!r} not found on current screen", "goal-L1")
        if self.llm is None or not self.llm_goal:
            return Check(Verdict.UNCERTAIN, stale or "no rule evidence and L2 unavailable/disabled", "goal-L1",
                         {"stale_evidence": True} if stale else {})
        out = self.llm.chat(STEP_SYSTEM.format(platform=self.platform),
                            GOAL_PROMPT.format(goal=goal, evidence=evidence or goal, text=self._screen_text(obs),
                                               stale_block=stale_block),
                            [obs.screenshot])
        c = _parse_check(out, allowed=_GOAL_VERDICTS)
        c.level = "goal-L2"
        return c

    def check_final(self, obs: Observation, task: str, subgoals: list, final_l2: str = "when_needed",
                    stable: bool = True, baseline: Optional[Observation] = None) -> Check:
        """任务收尾核验（只看当前屏幕）。

        1) 规则：每个带 expect_text 且 persistent 的子目标，其文字现在必须仍在屏幕/无障碍树中（零模型调用）；
           任何一个消失 → FAILED（例如 A 打开后又被 B 的操作恢复成关闭）。
        2) 若所有子目标都有可规则核验的证据，且这些证据不是 baseline 里就有的旧证据，并且 final_l2=when_needed
           → SUCCESS（final-L1）。
        3) 否则用整任务 + 全部子目标预期做一次聚合 L2（提示词里带上“哪些是旧证据”的上下文）；L2 不可用 →
           UNCERTAIN。只有明确 success 才算成功。
        """
        if self.trigger == "none":
            return Check(Verdict.SUCCESS, "goal verification disabled", "off")
        ns = self._not_settled(obs, stable, "final-L1")
        if ns:
            return ns
        text = obs.all_text().lower()
        checkable = [sg for sg in subgoals if getattr(sg, "expect_text", "") and getattr(sg, "persistent", True)]
        stale_sgs: list = []
        proven = list(checkable)
        if self.use_a11y:
            gone = [sg for sg in checkable if sg.expect_text.lower() not in text]
            if gone:
                desc = "; ".join(f"sub-goal {sg.id} ({sg.goal!r}) expected {sg.expect_text!r}" for sg in gone)
                return Check(Verdict.FAILED, f"no longer true on the current screen: {desc}", "final-L1",
                             {"failed_subgoals": [sg.id for sg in gone]})
            if baseline is not None:            # 旧证据：任务开始时就在、之后没有实质替换 → 不参与 L1 成功
                stale_sgs = [sg for sg in checkable if self._stale_evidence(baseline, obs, sg.expect_text)]
                proven = [sg for sg in checkable if sg not in stale_sgs]
            if subgoals and len(proven) == len(subgoals) and final_l2 != "always":
                return Check(Verdict.SUCCESS, f"all {len(subgoals)} sub-goal expectations visible now", "final-L1")
        if self.llm is None or not self.llm_goal:
            missing = [sg.id for sg in subgoals if sg not in proven] if self.use_a11y else [sg.id for sg in subgoals]
            stale_note = f"; sub-goals {[sg.id for sg in stale_sgs]} show only stale evidence" if stale_sgs else ""
            sig = {"stale_subgoals": [sg.id for sg in stale_sgs]} if stale_sgs else {}
            return Check(Verdict.UNCERTAIN,
                         f"sub-goals {missing} have no rule evidence and L2 is unavailable/disabled{stale_note}",
                         "final-L1", sig)
        lines = []
        for sg in subgoals:
            bits = [f"{sg.id}. {sg.goal}"]
            if sg.expected:
                bits.append(f"expected: {sg.expected}")
            if sg.evidence:
                bits.append(f"evidence: {sg.evidence}")
            if sg.expect_text:
                bits.append(f"text that should be visible: {sg.expect_text!r}")
            lines.append(" | ".join(bits))
        tb = (f"Visible text / accessibility summary (may be partial):\n{obs.all_text()[:1500]}\n"
              if self.a11y_prompt else "")
        stale_block = _final_stale_block([sg.id for sg in stale_sgs]) if stale_sgs else ""
        out = self.llm.chat(STEP_SYSTEM.format(platform=self.platform),
                            FINAL_PROMPT.format(task=task, subgoals="\n".join(lines) or "(none)", text_block=tb,
                                                stale_block=stale_block),
                            [obs.screenshot])
        c = _parse_check(out, allowed=_GOAL_VERDICTS)
        c.level = "final-L2"
        return c


def _busy_el(e) -> bool:
    if e.attrs.get("aria-busy") == "true":
        return True
    if e.native_role not in PROGRESS_ROLES and not e.native_role.endswith(".ProgressBar"):
        return False
    try:        # 静态的满进度条（例如存储空间 100%）不算忙碌
        now, mx = float(e.attrs.get("aria-valuenow")), float(e.attrs.get("aria-valuemax", 100))
        return now < mx
    except (TypeError, ValueError):
        return True     # 不定进度 / 没有数值：按忙碌处理


def _busy_text(obs: Observation) -> str:
    """整行状态文字（Loading… 等）算忙碌；说明性段落（“Loading takes a few seconds”）不算。"""
    for line in (obs.text or "").splitlines():
        s = line.strip()
        if s and BUSY_LINE_RE.match(s):
            return s
    return ""


def is_busy(obs: Observation) -> str:
    """返回忙碌指示文本（空串表示不忙）。元素信号（未满 progressbar / aria-busy）优先，其次整行状态文字。"""
    for e in obs.elements:
        if _busy_el(e):
            return e.name or e.native_role
    return _busy_text(obs)


_GOAL_VERDICTS = {Verdict.SUCCESS, Verdict.FAILED, Verdict.UNCERTAIN}


def _parse_check(text: str, allowed=None) -> Check:
    """解析 L2 回复。无法解析 / 缺 verdict / 非法标签 → UNCERTAIN（绝不默认成功）。"""
    try:
        obj = extract_json(text)
        raw = obj.get("verdict")
        if not isinstance(raw, str):
            return Check(Verdict.UNCERTAIN, f"verifier gave no verdict: {str(text)[:120]!r}", "L2")
        v = Verdict(raw.strip().lower())
        if allowed is not None and v not in allowed:
            return Check(Verdict.UNCERTAIN, f"verdict {v.value!r} not valid here: {obj.get('evidence', '')}", "L2")
        return Check(v, str(obj.get("evidence", "")), "L2")
    except Exception:
        return Check(Verdict.UNCERTAIN, f"unparseable verifier output: {str(text)[:120]!r}", "L2")

"""主执行循环（跨平台）：

  规划 → [每步] 观察 → 决策(Actor) → 坐标换算 → 定位(Grounder) → 安全闸门 → 执行
       → 等待稳定 → 验证(L0/L1/L2) → (恢复 + 反思) → 子目标收尾核验 → 里程碑 → … → 任务收尾核验

预算与限制：全局步数、每子目标步数、重规划次数、模型调用数、token、墙钟时间，任一触顶即停止。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .actions import Action
from .coords import to_pixel_action
from .env.base import Env, ExecResult, Observation
from .grounding import Grounder
from .llm.base import Budget
from .logger import TrajectoryLogger
from .memory import Memory, Milestone, StepRecord
from .planner import Actor, Planner, Subgoal
from .recovery import RecoveryPolicy, Strategy
from .reflection import Reflector
from .safety import SafetyGuard
from .verify import Check, Verdict, Verifier


@dataclass
class AgentConfig:
    max_steps: int = 50
    max_steps_per_subgoal: int = 15
    max_replans: int = 2
    settle_timeout: float = 5.0
    settle_interval: float = 0.4
    task_window: str = ""            # 任务窗口标题片段 / Android 包名 / Web 页面标题，用于焦点检查
    verify_goals: bool = True        # 子目标收尾核验
    max_budget_calls: int = 200      # 模型调用硬上限（公平对比用）
    max_tokens: Optional[int] = None
    max_seconds: Optional[float] = None
    platform: str = "mock"


@dataclass
class RunResult:
    status: str                      # done | fail | step_limit | budget_limit | time_limit
    claimed_done: bool
    steps: int
    replans: int
    recoveries: list[str] = field(default_factory=list)
    budget: Optional[Budget] = None
    seconds: float = 0.0
    message: str = ""
    answer: str = ""
    safety_events: list[dict] = field(default_factory=list)


StepHook = Callable[[int, "GUIAgent"], None]


class GUIAgent:
    def __init__(self, env: Env, planner: Planner, actor: Actor, grounder: Grounder, verifier: Verifier,
                 recovery: RecoveryPolicy, cfg: AgentConfig, budget: Budget,
                 logger: Optional[TrajectoryLogger] = None, memory: Optional[Memory] = None,
                 reflector: Optional[Reflector] = None, guard: Optional[SafetyGuard] = None):
        self.env, self.planner, self.actor, self.grounder = env, planner, actor, grounder
        self.verifier, self.recovery, self.cfg, self.budget = verifier, recovery, cfg, budget
        self.log = logger
        self.mem = memory or Memory()
        self.reflector = reflector or Reflector(None, enabled=False)
        self.guard = guard or SafetyGuard(mode="deny")
        self.step_no = 0
        self.before_step: list[StepHook] = []
        self._t0 = time.time()
        self._answer = ""
        if self.budget.max_calls is None:
            self.budget.max_calls = cfg.max_budget_calls
        if self.budget.max_tokens is None:
            self.budget.max_tokens = cfg.max_tokens

    # ------------------------------------------------------------------ helpers
    def _settle(self) -> tuple[Observation, bool]:
        return self.env.wait_until_stable(timeout=self.cfg.settle_timeout, interval=self.cfg.settle_interval)

    def _limit(self) -> Optional[tuple[str, str]]:
        if self.step_no >= self.cfg.max_steps:
            return "step_limit", "global step limit"
        b = self.budget.exhausted()
        if b:
            return "budget_limit", b
        if self.cfg.max_seconds and time.time() - self._t0 > self.cfg.max_seconds:
            return "time_limit", f"wall-clock limit {self.cfg.max_seconds}s"
        return None

    def _resolve(self, a: Action, obs: Observation, zoom_around=None) -> tuple[Action, str]:
        """坐标换算 + 把 target 描述 / element_id 变成截图像素坐标。"""
        w, h = obs.screenshot.size
        to_pixel_action(a, w, h, getattr(self.grounder.mapper, "max_pixels", 1280 * 28 * 28))
        if a.is_pointer and (a.x is None or a.element_id is not None):
            g = self.grounder.ground(obs, a.target or "", a.element_id, zoom_around)
            if g is None:
                return a, "grounding_failed"
            a.x, a.y = g.x, g.y
            if a.type == "drag" and a.x2 is None:
                g2 = self.grounder.ground(obs, a.target2 or a.text or "")
                if g2 is None:
                    return a, "grounding_failed"
                a.x2, a.y2 = g2.x, g2.y
            return a, g.source
        if a.type == "scroll" and (a.target or a.element_id is not None) and a.x is None:
            g = self.grounder.ground(obs, a.target or "", a.element_id)
            if g:
                a.x, a.y = g.x, g.y
        return a, "direct" if a.is_pointer else "-"

    # ------------------------------------------------------------------ main
    def run(self, task: str) -> RunResult:
        self._t0 = time.time()
        obs = self.env.observe()
        subgoals = self.planner.plan(task, obs)
        if self.log:
            self.log.meta(task_text=task, platform=self.cfg.platform, task_window=self.cfg.task_window)
            self.log.step(kind="plan", subgoals=subgoals)
        replans, idx = 0, 0

        while idx < len(subgoals):
            sg = subgoals[idx]
            self.recovery.reset_subgoal()
            outcome, notes = self._run_subgoal(task, sg, len(subgoals))
            if outcome == "done":
                idx += 1
                continue
            if outcome in {"step_limit", "budget_limit", "time_limit"}:
                return self._finish(outcome, False, replans, notes)
            if replans >= self.cfg.max_replans or self._limit():
                return self._finish("fail", False, replans, notes)
            replans += 1
            obs = self.env.observe()
            self.mem.invalidate_after(sg.id)
            rest = self.planner.replan(task, obs, sg, notes, self.mem.milestones_text(), sg.id, self.mem.notes_text())
            subgoals = subgoals[:idx] + rest
            if self.log:
                self.log.step(kind="replan", failed=sg, notes=notes, subgoals=rest)

        # 任务收尾核验：只看当前屏幕，避免“历史上保存过”代替“现在已保存”
        if self.cfg.verify_goals and len(subgoals) > 1:
            last = subgoals[-1]
            final = self.verifier.check_goal(self._settle()[0], task, "all task requirements satisfied",
                                             last.expect_text or None)
            if self.log:
                self.log.step(kind="final_check", verdict=final.verdict, evidence=final.evidence, level=final.level)
            if final.verdict == Verdict.FAILED:
                return self._finish("fail", False, replans, f"final check failed: {final.evidence}")
        return self._finish("done", True, replans, "")

    # ------------------------------------------------------------------
    def _run_subgoal(self, task: str, sg: Subgoal, total: int) -> tuple[str, str]:
        feedback, last_failure = "", None
        zoom_around = None
        for _ in range(self.cfg.max_steps_per_subgoal):
            lim = self._limit()
            if lim:
                return lim
            self.step_no += 1
            for hook in self.before_step:
                hook(self.step_no, self)
            before = self.env.observe()
            if self._precheck_focus(sg, before):
                feedback = "The task window had lost focus; it was restored before acting."
                continue
            try:
                action, thought = self.actor.next_action(task, sg, total, before, self.mem.history_text(sg.id),
                                                         self.mem.milestones_text(), feedback,
                                                         notes=self.mem.notes_text())
            except ValueError as e:  # 模型输出无法解析
                feedback = f"Your last reply could not be parsed ({e}). Reply with ONE valid JSON action."
                self.mem.add_step(StepRecord(self.step_no, sg.id, "(unparseable)", "failed", str(e)[:120]))
                continue

            # ---- 终止 / 交互类动作
            if action.type == "fail":
                return "fail", action.text or action.reason or "actor gave up"
            if action.type == "done":
                ok, ev = self._confirm_goal(sg)
                if ok:
                    self._answer = action.text or self._answer
                    self.mem.add_milestone(Milestone(sg.id, sg.goal, ev, time.time(), self.env.observe(False).screenshot))
                    if self.log:
                        self.log.step(kind="milestone", subgoal=sg, evidence=ev, answer=action.text)
                    return "done", ev
                feedback = f"You said the sub-goal is done, but verification disagrees: {ev}"
                self.mem.add_step(StepRecord(self.step_no, sg.id, "done", "rejected", ev))
                if self.log:
                    self.log.step(kind="rejected_done", step=self.step_no, subgoal=sg.id, evidence=ev)
                continue
            if action.type == "ask_user":
                ans = self.guard.ask(action.text or "")
                if self.log:
                    self.log.step(kind="ask_user", step=self.step_no, question=action.text, answer=ans)
                feedback = (f"The user answered: {ans}" if ans else
                            "The user is not available. Proceed with your best judgment, or fail if impossible.")
                self.mem.add_step(StepRecord(self.step_no, sg.id, action.short(), "success" if ans else "uncertain",
                                             f"answer={ans!r}"))
                continue

            # ---- 定位
            action, src = self._resolve(action, before, zoom_around)
            zoom_around = None
            if src == "grounding_failed":
                feedback = f"Could not locate {action.target!r} on screen; describe it differently, use element_id, or another path."
                self.mem.add_step(StepRecord(self.step_no, sg.id, action.short(), "grounding_failed", ""))
                continue

            # ---- 安全闸门
            approved, why = self.guard.gate(action, before)
            if why and self.log:
                self.log.step(kind="safety", step=self.step_no, action=action.to_dict(), reason=why, approved=approved)
            if not approved:
                res = ExecResult(False, f"blocked_by_safety: {why}", time.time(), time.time())
                after, stable = before, True
            else:
                res = self.env.execute(action)
                after, stable = self._settle()
            check = self.verifier.check_step(before, after, action, res, stable, expected=sg.expected,
                                             task_window=self.cfg.task_window, expect_text=sg.expect_text or None)
            rec = StepRecord(self.step_no, sg.id, action.short(), check.verdict.value, check.evidence)

            if check.verdict == Verdict.SUCCESS:
                feedback, last_failure = "", None
            else:
                if self.mem.is_looping():
                    last_failure = "repeat"
                plan = self.recovery.decide(check, action, self.cfg.task_window, last_failure)
                rec.recovery = plan.strategy.value
                last_failure = check.verdict.value
                for ra in plan.actions:
                    self.env.execute(ra)
                if plan.actions:
                    self._settle()
                if plan.strategy == Strategy.REGROUND_ZOOM and action.point is not None:
                    zoom_around = action.point
                if plan.strategy in {Strategy.REPLAN, Strategy.GIVE_UP} and self.recovery.exhausted:
                    self.mem.add_step(rec)
                    self._log_step(rec, before, after, action, src, check, thought)
                    return "fail", f"{check.verdict.value}: {check.evidence}"
                feedback = f"Last action => {check.verdict.value}: {check.evidence}. Recovery: {plan.note}."
                if self.reflector.should_reflect(check.verdict.value) and not self.budget.exhausted():
                    note = self.reflector.reflect(task, sg.goal, self.mem.history_text(sg.id) + "\n" + rec.action,
                                                  f"{check.verdict.value}: {check.evidence}")
                    if note:
                        self.mem.add_note(note)
                        feedback += f" Reflection: {note}"
                        if self.log:
                            self.log.step(kind="reflection", step=self.step_no, note=note)

            self.mem.add_step(rec)
            self._log_step(rec, before, after, action, src, check, thought)
        return "fail", "sub-goal step limit"

    def _precheck_focus(self, sg: Subgoal, before: Observation) -> bool:
        """动作前的状态核对（时间失配：观察到的前台已不是任务窗口，例如被新标签页/弹窗/最小化抢走）。

        发现失配时不调用 actor（避免基于错误窗口做决策），直接走 REFOCUS 恢复。返回是否做了恢复。
        """
        tw = self.cfg.task_window
        if not tw or self.verifier.trigger == "none" or tw.lower() in (before.active_window or "").lower():
            return False
        check = Check(Verdict.FAILED, f"before acting, foreground is {before.active_window!r}, not {tw!r}",
                      "pre", {"focus_lost": True, "win_before": before.active_window})
        plan = self.recovery.decide(check, Action("wait"), tw, None)
        for ra in plan.actions:
            self.env.execute(ra)
        after, _ = self._settle()
        rec = StepRecord(self.step_no, sg.id, "(pre-action focus check)", check.verdict.value, check.evidence,
                         plan.strategy.value)
        self.mem.add_step(rec)
        self._log_step(rec, before, after, Action("wait"), "-", check, "pre-action state check")
        return bool(plan.actions) or plan.strategy != Strategy.REFOCUS

    def _confirm_goal(self, sg: Subgoal) -> tuple[bool, str]:
        if not self.cfg.verify_goals:
            return True, "not verified"
        obs, _ = self._settle()
        c: Check = self.verifier.check_goal(obs, sg.goal, sg.evidence or sg.expected, sg.expect_text or None)
        return c.verdict == Verdict.SUCCESS, f"[{c.level}] {c.evidence}"

    def _log_step(self, rec: StepRecord, before, after, action, src, check: Check, thought: str) -> None:
        if not self.log:
            return
        self.log.step(kind="step", step=rec.step, subgoal=rec.subgoal_id, thought=thought,
                      action=action.to_dict(), grounding=src, verdict=check.verdict, level=check.level,
                      evidence=check.evidence, signals=check.signals, recovery=rec.recovery,
                      window=after.active_window, url=after.url,
                      before=self.log.shot(rec.step, "before", before.screenshot),
                      after=self.log.shot(rec.step, "after", after.screenshot),
                      calls_so_far=self.budget.calls)

    def _finish(self, status: str, claimed: bool, replans: int, msg: str) -> RunResult:
        r = RunResult(status, claimed, self.step_no, replans, list(self.recovery.history), self.budget,
                      time.time() - self._t0, msg, self._answer, list(self.guard.log))
        if self.log:
            self.log.meta(result=r)
        return r

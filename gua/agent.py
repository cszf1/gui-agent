"""主执行循环（跨平台）：

  规划 → [每步] 观察 → 决策(Actor) → 坐标换算 → 定位(Grounder) → 安全闸门 → 执行
       → 等待稳定 → 验证(L0/L1/L2) → (恢复 + 反思) → 子目标收尾核验 → 里程碑 → … → 任务收尾核验

预算与限制：全局步数、每子目标步数、重规划次数、模型调用数、token、成本、墙钟时间，任一触顶即停止。

v0.3 审查修复（详见 docs/review-fixes.md）：
1. 所有真正执行的动作（actor 动作、恢复 / 重试 / 撤销 / 滚动 / 切回窗口）都经过 `_execute_gated` → 安全闸门；
   拒绝对该动作是终止性的（SafetyGuard 记住签名，fixed_retry 也不会重试）。
2. 任务收尾核验默认总是运行（不再只在 >1 个子目标时）：重新规则核验每个子目标的 expect_text + 需要时整任务聚合 L2；
   只有明确 SUCCESS 才返回 done；uncertain 默认先重规划一次，仍不确定则返回 "uncertain"（不算成功）。
3. 预算在模型调用边界上硬性执行（BudgetExceeded 在请求发出前抛出），这里转换为终止状态 "budget_exhausted"。
5. actor 输出解析 / 校验失败（ActionParseError 等）一律变成反馈交还模型，不会让运行崩溃。
6. 各组件读取同一个 CapabilityPolicy。
10. UserAbort（pyautogui fail-safe 等人工紧急停止）→ 终止状态 "user_abort"。

v0.3.1 第二轮审查修复：
- 只有执行器持有 typed text 原文（条目 2 / 3）：步骤记忆、Actor / 反思 / L2 请求、安全日志、轨迹日志、
  HTML 回放、RunResult 都使用安全摘要（gua.sensitive）；已知秘密登记到 Scrubber 做最后一道清洗（执行层报错
  回显、模型思考、证据文字）。可选 AgentConfig.secrets：模型只输出 <secret>名字</secret>，执行器在发往环境前替换。
- 子目标 / 任务收尾核验不再丢弃 _settle() 的稳定标志（条目 7）：未稳定或仍忙碌 → 再等待并复查
  busy_rechecks 次，仍不稳定 → uncertain（绝不 success）；子目标 / 任务开始时的观察作为旧证据基线。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

from .actions import Action, ActionParseError
from .coords import to_pixel_action
from .env.base import Env, ExecResult, Observation
from .errors import UserAbort
from .grounding import Grounder
from .llm.base import Budget, BudgetExceeded
from .logger import TrajectoryLogger
from .memory import Memory, Milestone, StepRecord
from .planner import Actor, Planner, Subgoal
from .policy import CapabilityPolicy
from .recovery import RecoveryPolicy, Strategy
from .reflection import Reflector
from .safety import SafetyGuard
from .sensitive import SECRET_RE, Scrubber, is_sensitive_type, resolve_secrets, safe_view
from .verify import Check, Verdict, Verifier

TERMINAL_STATUSES = {"done", "fail", "uncertain", "step_limit", "budget_exhausted", "time_limit", "user_abort"}


@dataclass
class AgentConfig:
    max_steps: int = 50
    max_steps_per_subgoal: int = 15
    max_replans: int = 2
    settle_timeout: float = 5.0
    settle_interval: float = 0.4
    task_window: str = ""            # 任务窗口标题片段 / Android 包名 / Web 页面标题，用于焦点检查
    verify_goals: bool = True        # 子目标收尾核验
    final_check: bool = True         # 任务收尾核验（v0.3：默认总是运行，含单子目标任务；显式 False 才关闭）
    final_l2: str = "when_needed"    # when_needed | always：所有子目标都被规则证实时是否还要调用聚合 L2
    on_uncertain: str = "replan"     # 收尾核验 uncertain：replan（重规划一次后报告 uncertain）| fail（直接报告 uncertain）
    max_budget_calls: int = 200      # 模型调用硬上限（公平对比用；在调用边界强制执行）
    max_tokens: Optional[int] = None
    max_cost_usd: Optional[float] = None
    max_seconds: Optional[float] = None
    platform: str = "mock"
    secrets: dict = field(default_factory=dict, repr=False)   # 名字 → 秘密；模型只看到名字（<secret>名字</secret>）
    busy_rechecks: int = 2           # 收尾核验时界面未稳定 / 忙碌：最多再等待复查几次，之后判 uncertain


@dataclass
class RunResult:
    status: str                      # done | fail | uncertain | step_limit | budget_exhausted | time_limit | user_abort
    claimed_done: bool
    steps: int
    replans: int
    recoveries: list[str] = field(default_factory=list)
    budget: Optional[Budget] = None
    seconds: float = 0.0
    message: str = ""
    answer: str = ""
    safety_events: list[dict] = field(default_factory=list)
    policy: dict = field(default_factory=dict)


StepHook = Callable[[int, "GUIAgent"], None]


class GUIAgent:
    def __init__(self, env: Env, planner: Planner, actor: Actor, grounder: Grounder, verifier: Verifier,
                 recovery: RecoveryPolicy, cfg: AgentConfig, budget: Budget,
                 logger: Optional[TrajectoryLogger] = None, memory: Optional[Memory] = None,
                 reflector: Optional[Reflector] = None, guard: Optional[SafetyGuard] = None,
                 policy: Optional[CapabilityPolicy] = None):
        self.env, self.planner, self.actor, self.grounder = env, planner, actor, grounder
        self.verifier, self.recovery, self.cfg, self.budget = verifier, recovery, cfg, budget
        self.log = logger
        self.mem = memory or Memory()
        self.reflector = reflector or Reflector(None, enabled=False)
        self.guard = guard or SafetyGuard(mode="deny")
        self.policy = policy or CapabilityPolicy(
            a11y_grounding=grounder.use_a11y, a11y_rules=verifier.use_a11y,
            a11y_in_prompts=getattr(getattr(actor, "policy", None), "a11y_in_prompts", True),
            step_trigger=verifier.trigger, llm_step_verify=getattr(verifier, "llm_step", True),
            llm_goal_verify=getattr(verifier, "llm_goal", True), llm_reflection=self.reflector.enabled,
            goal_check=cfg.verify_goals, final_check=cfg.final_check, final_l2=cfg.final_l2,
            on_uncertain=cfg.on_uncertain,
            recovery="none" if not recovery.enabled else "fixed_retry" if recovery.fixed_retry else "classified")
        self.step_no = 0
        self.before_step: list[StepHook] = []
        self._t0 = time.time()
        self._answer = ""
        # 秘密清洗器与日志共用（日志落盘、评测结果行、RunResult 都经过它）
        self.scrubber = logger.scrubber if logger is not None else Scrubber()
        self.scrubber.extend(str(v) for v in (cfg.secrets or {}).values())
        self._sg_baseline: Optional[Observation] = None
        self._task_baseline: Optional[Observation] = None
        if self.budget.max_calls is None:
            self.budget.max_calls = cfg.max_budget_calls
        if self.budget.max_tokens is None:
            self.budget.max_tokens = cfg.max_tokens
        if self.budget.max_cost_usd is None:
            self.budget.max_cost_usd = cfg.max_cost_usd

    # ------------------------------------------------------------------ helpers
    def _settle(self) -> tuple[Observation, bool]:
        return self.env.wait_until_stable(timeout=self.cfg.settle_timeout, interval=self.cfg.settle_interval)

    def _limit(self) -> Optional[tuple[str, str]]:
        if self.step_no >= self.cfg.max_steps:
            return "step_limit", "global step limit"
        b = self.budget.exhausted()
        if b:
            return "budget_exhausted", b
        if self.cfg.max_seconds and time.time() - self._t0 > self.cfg.max_seconds:
            return "time_limit", f"wall-clock limit {self.cfg.max_seconds}s"
        return None

    def _resolve(self, a: Action, obs: Observation, zoom_around=None) -> tuple[Action, str]:
        """坐标换算 + 把 target 描述 / element_id 变成截图像素坐标。"""
        w, h = obs.screenshot.size
        # 动作自带的 transform（actor 回复里“实际发送尺寸”）优先，见 coords.ImageTransform
        to_pixel_action(a, w, h, getattr(self.actor, "max_pixels", getattr(self.grounder.mapper, "max_pixels",
                                                                           1280 * 28 * 28)))
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

    def _scrub(self, text):
        return self.scrubber.scrub(text) if isinstance(text, str) else text

    def _view(self, a: Action, obs: Optional[Observation]) -> tuple[dict, str]:
        """安全摘要（dict 给日志，str 给记忆 / 提示词）；脱敏只由输入目标决定。"""
        d = safe_view(a, obs, self.guard.focus(obs)[1])
        d = self.scrubber.scrub_obj(d)
        short = dict(d)
        short.pop("reason", None)
        import json as _json
        return d, _json.dumps(short, ensure_ascii=False)

    def _actor_task(self, task: str) -> str:
        if not self.cfg.secrets:
            return task
        names = ", ".join(sorted(self.cfg.secrets))
        return (f"{task}\n(Secrets you may type without knowing them: write <secret>NAME</secret> in a type action; "
                f"available names: {names})")

    # ------------------------------------------------------------------ 统一执行出口（安全闸门）
    def _execute_gated(self, a: Action, obs: Optional[Observation], origin: str = "actor") -> ExecResult:
        """所有真正发往环境的动作都从这里走：先过安全闸门，拒绝即返回 blocked_by_safety（终止性，不重试）。

        v0.3.1：敏感输入的原文登记到 Scrubber；秘密占位符只在这里、紧挨着 env.execute 才替换成原文。
        """
        state = self.guard.focus(obs)[1]
        if a.type == "type" and a.text and is_sensitive_type(a, obs, state) and not SECRET_RE.search(a.text):
            self.scrubber.add(a.text)
        view, _ = self._view(a, obs)
        approved, why = self.guard.gate(a, obs)
        why = self._scrub(why)
        if (why or not approved) and self.log:
            self.log.step(kind="safety", step=self.step_no, origin=origin, action=view,
                          reason=why, approved=approved)
        if not approved:
            t = time.time()
            return ExecResult(False, f"blocked_by_safety: {why}", t, t)
        exec_a = a
        if a.type == "type" and a.text and self.cfg.secrets and SECRET_RE.search(a.text):
            exec_a = replace(a, text=resolve_secrets(a.text, self.cfg.secrets))
        res = self.env.execute(exec_a)
        res.error, res.output = self._scrub(res.error), self._scrub(res.output)   # 执行层报错可能回显输入
        if not res.ok and "blocked_by_safety" in (res.error or ""):
            self.guard.remember_denial(a, obs, res.error)      # 环境层（例如 Web 白名单）拦截也是终止性的
        return res

    def _settled_for_check(self, baseline: Optional[Observation] = None) -> tuple[Observation, bool]:
        """收尾核验用的观察：未稳定或仍有忙碌指示时再等待复查（最多 busy_rechecks 次）。返回 (观察, 是否已稳定)。"""
        obs, stable = self._settle()
        for _ in range(max(0, self.cfg.busy_rechecks)):
            if stable and not self.verifier.busy(obs, baseline):
                break
            obs, stable = self._settle()
        return obs, stable

    def _run_recovery_actions(self, actions: list[Action], obs: Observation, origin: str) -> list[str]:
        """执行恢复动作（每个都过安全闸门）；返回被拦下的动作说明。被拦后不再继续执行后续恢复动作。"""
        blocked = []
        for ra in actions:
            shown = self._view(ra, obs)[1]
            r = self._execute_gated(ra, obs, origin)
            if not r.ok and "blocked_by_safety" in r.error:
                blocked.append(f"{shown} ({r.error})")
                break
        if actions:
            self._settle()
        return blocked

    # ------------------------------------------------------------------ main
    def run(self, task: str) -> RunResult:
        self._t0 = time.time()
        self._replans = 0
        try:
            return self._run(task)
        except BudgetExceeded as e:
            if self.log:
                self.log.step(kind="budget_exhausted", step=self.step_no, reason=e.reason, resource=e.resource)
            return self._finish("budget_exhausted", False, self._replans, e.reason)
        except UserAbort as e:
            if self.log:
                self.log.step(kind="user_abort", step=self.step_no, reason=str(e))
            return self._finish("user_abort", False, self._replans, f"user abort: {e}")

    def _run(self, task: str) -> RunResult:
        self._replans = 0
        obs = self.env.observe()
        self._task_baseline = obs
        subgoals = self.planner.plan(task, obs)
        if self.log:
            self.log.meta(task_text=task, platform=self.cfg.platform, task_window=self.cfg.task_window,
                          policy=self.policy.to_dict())
            self.log.step(kind="plan", subgoals=subgoals)
        idx, final_retries = 0, 0

        while True:
            while idx < len(subgoals):
                sg = subgoals[idx]
                self.recovery.reset_subgoal()
                outcome, notes = self._run_subgoal(task, sg, len(subgoals))
                if outcome == "done":
                    idx += 1
                    continue
                if outcome in {"step_limit", "budget_exhausted", "time_limit"}:
                    return self._finish(outcome, False, self._replans, notes)
                lim = self._limit()
                if lim:
                    return self._finish(lim[0], False, self._replans, f"{notes}; {lim[1]}")
                if self._replans >= self.cfg.max_replans:
                    return self._finish("fail", False, self._replans, notes)
                self._replans += 1
                obs = self.env.observe()
                self.mem.invalidate_after(sg.id)
                rest = self.planner.replan(task, obs, sg, self._scrub(notes), self.mem.milestones_text(), sg.id,
                                           self._scrub(self.mem.notes_text()))
                subgoals = subgoals[:idx] + rest
                if self.log:
                    self.log.step(kind="replan", failed=sg, notes=notes, subgoals=rest)

            # 任务收尾核验：只看当前屏幕；聚合整个任务 + 全部子目标；明确 SUCCESS 才算完成
            if not self.cfg.final_check:
                return self._finish("done", True, self._replans, "final check disabled")
            fobs, fstable = self._settled_for_check(self._task_baseline)
            final = self.verifier.check_final(fobs, task, subgoals, self.cfg.final_l2, stable=fstable,
                                              baseline=self._task_baseline)
            final.evidence = self._scrub(final.evidence)
            if self.log:
                self.log.step(kind="final_check", verdict=final.verdict, evidence=final.evidence, level=final.level,
                              signals=final.signals)
            if final.verdict == Verdict.SUCCESS:
                return self._finish("done", True, self._replans, "")
            status = "fail" if final.verdict == Verdict.FAILED else "uncertain"
            note = f"final check {final.verdict.value}: {final.evidence}"
            can_retry = (final_retries < 1 and self._replans < self.cfg.max_replans and self._limit() is None
                         and not (status == "uncertain" and self.cfg.on_uncertain == "fail"))
            if not can_retry:
                return self._finish(status, False, self._replans, note)
            final_retries += 1
            self._replans += 1
            obs = self.env.observe()
            failed = Subgoal(0, f"final check of the whole task: {task}", "all task requirements hold on screen")
            rest = self.planner.replan(task, obs, failed, self._scrub(note), self.mem.milestones_text(),
                                       max((sg.id for sg in subgoals), default=0) + 1,
                                       self._scrub(self.mem.notes_text()))
            idx = len(subgoals)
            subgoals = subgoals + rest
            if self.log:
                self.log.step(kind="replan", failed=failed, notes=note, subgoals=rest)

    # ------------------------------------------------------------------
    def _run_subgoal(self, task: str, sg: Subgoal, total: int) -> tuple[str, str]:
        feedback, last_failure = "", None
        zoom_around = None
        self._sg_baseline = None
        actor_task = self._actor_task(task)
        for _ in range(self.cfg.max_steps_per_subgoal):
            lim = self._limit()
            if lim:
                return lim
            self.step_no += 1
            for hook in self.before_step:
                hook(self.step_no, self)
            before = self.env.observe()
            if self._sg_baseline is None:
                self._sg_baseline = before          # 子目标开始时的界面：旧证据基线
            if self._precheck_focus(sg, before):
                feedback = "The task window had lost focus; it was restored before acting."
                continue
            try:
                action, thought = self.actor.next_action(actor_task, sg, total, before,
                                                         self.mem.history_text(sg.id),
                                                         self.mem.milestones_text(), self._scrub(feedback),
                                                         notes=self._scrub(self.mem.notes_text()))
                if not isinstance(action, Action):
                    raise ActionParseError("not_object", f"actor returned {type(action).__name__}, not an Action")
                action.validate()
            except (ActionParseError, ValueError, TypeError, KeyError, IndexError) as e:   # 不可解析 / 不合法
                err = e if isinstance(e, ActionParseError) else ActionParseError("invalid", f"{type(e).__name__}: {e}")
                feedback = err.feedback()
                self.mem.add_step(StepRecord(self.step_no, sg.id, "(invalid action)", "failed", str(err)[:160]))
                if self.log:
                    self.log.step(kind="parse_error", step=self.step_no, subgoal=sg.id, error=err.to_dict())
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
                self.mem.add_step(StepRecord(self.step_no, sg.id, self._view(action, before)[1],
                                             "success" if ans else "uncertain", f"answer={ans!r}"))
                continue

            # ---- 定位
            action, src = self._resolve(action, before, zoom_around)
            zoom_around = None
            if src == "grounding_failed":
                feedback = f"Could not locate {action.target!r} on screen; describe it differently, use element_id, or another path."
                self.mem.add_step(StepRecord(self.step_no, sg.id, self._view(action, before)[1], "grounding_failed", ""))
                continue

            # ---- 安全闸门 + 执行（统一出口）
            view_d, view_s = self._view(action, before)      # 在闸门之前算（闸门会更新焦点状态）
            res = self._execute_gated(action, before, "actor")
            if not res.ok and res.error.startswith("blocked_by_safety") and "off-allowlist" not in res.error:
                after, stable = before, True
            else:
                after, stable = self._settle()
            check = self.verifier.check_step(before, after, action, res, stable, expected=sg.expected,
                                             task_window=self.cfg.task_window, expect_text=sg.expect_text or None,
                                             action_desc=view_s)
            check.evidence = self._scrub(check.evidence)
            rec = StepRecord(self.step_no, sg.id, view_s, check.verdict.value, check.evidence)

            if check.verdict == Verdict.SUCCESS:
                feedback, last_failure = "", None
            else:
                if self.mem.is_looping():
                    last_failure = "repeat"
                plan = self.recovery.decide(check, action, self.cfg.task_window, last_failure)
                rec.recovery = plan.strategy.value
                last_failure = check.verdict.value
                blocked = self._run_recovery_actions(plan.actions, after, f"recovery:{plan.strategy.value}")
                if plan.strategy == Strategy.REGROUND_ZOOM and action.point is not None:
                    zoom_around = action.point
                if plan.strategy in {Strategy.REPLAN, Strategy.GIVE_UP} and self.recovery.exhausted:
                    self.mem.add_step(rec)
                    self._log_step(rec, before, after, view_d, src, check, thought)
                    return "fail", f"{check.verdict.value}: {check.evidence}"
                feedback = f"Last action => {check.verdict.value}: {check.evidence}. Recovery: {plan.note}."
                if "blocked_by_safety" in (check.signals or {}).get("exec_error", ""):
                    feedback += " That action was refused by the safety policy and will not be executed; do not retry it."
                if blocked:
                    feedback += f" Recovery action refused by safety policy: {'; '.join(blocked)}."
                if self.policy.llm_reflection and self.reflector.should_reflect(check.verdict.value) \
                        and not self.budget.exhausted():
                    note = self.reflector.reflect(task, sg.goal, self.mem.history_text(sg.id) + "\n" + rec.action,
                                                  f"{check.verdict.value}: {check.evidence}")
                    note = self._scrub(note)
                    if note:
                        self.mem.add_note(note)
                        feedback += f" Reflection: {note}"
                        if self.log:
                            self.log.step(kind="reflection", step=self.step_no, note=note)

            self.mem.add_step(rec)
            self._log_step(rec, before, after, view_d, src, check, thought)
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
            self._execute_gated(ra, before, "precheck")
        after, _ = self._settle()
        rec = StepRecord(self.step_no, sg.id, "(pre-action focus check)", check.verdict.value, check.evidence,
                         plan.strategy.value)
        self.mem.add_step(rec)
        self._log_step(rec, before, after, Action("wait").to_dict(), "-", check, "pre-action state check")
        return bool(plan.actions) or plan.strategy != Strategy.REFOCUS

    def _confirm_goal(self, sg: Subgoal) -> tuple[bool, str]:
        if not self.cfg.verify_goals:
            return True, "not verified"
        obs, stable = self._settled_for_check(self._sg_baseline)
        c: Check = self.verifier.check_goal(obs, sg.goal, sg.evidence or sg.expected, sg.expect_text or None,
                                            stable=stable, baseline=self._sg_baseline)
        return c.verdict == Verdict.SUCCESS, self._scrub(f"[{c.level}] {c.evidence}")

    def _log_step(self, rec: StepRecord, before, after, action_view: dict, src, check: Check, thought: str) -> None:
        if not self.log:
            return
        self.log.step(kind="step", step=rec.step, subgoal=rec.subgoal_id, thought=self._scrub(thought),
                      action=action_view, grounding=src, verdict=check.verdict, level=check.level,
                      evidence=check.evidence, signals=check.signals, recovery=rec.recovery,
                      window=after.active_window, url=after.url,
                      before=self.log.shot(rec.step, "before", before.screenshot),
                      after=self.log.shot(rec.step, "after", after.screenshot),
                      calls_so_far=self.budget.calls)

    def _finish(self, status: str, claimed: bool, replans: int, msg: str) -> RunResult:
        assert status in TERMINAL_STATUSES, status
        claimed = claimed and status == "done"          # 只有 done 才算“宣称完成”
        r = RunResult(status, claimed, self.step_no, replans, list(self.recovery.history), self.budget,
                      time.time() - self._t0, self._scrub(msg), self._scrub(self._answer),
                      self.scrubber.scrub_obj(list(self.guard.log)), self.policy.to_dict())
        if self.log:
            self.log.meta(result=r)
        return r


def _redact(a: Action, obs: Optional[Observation]) -> dict:
    """兼容 v0.3 的旧接口：等价于 gua.sensitive.safe_view（脱敏由输入目标决定）。"""
    return safe_view(a, obs)

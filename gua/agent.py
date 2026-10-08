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

from .actions import TOOL_ACTIONS, Action, ActionParseError, modality_of
from .coords import to_pixel_action
from .env.base import Env, ExecResult, Observation
from .errors import PrivacyBlocked, UserAbort
from .grounding import Grounder
from .hybrid import HybridConfig, HybridExecutor, uncertain_activation, wrap_fallback_result
from .llm.base import Budget, BudgetExceeded, EgressGate
from .logger import TrajectoryLogger
from .memory import Memory, Milestone, StepRecord
from .planner import Actor, Planner, Subgoal, UITarsActor
from .policy import CapabilityPolicy
from .performance import Performance
from .recovery import RecoveryPolicy, Strategy
from .reflection import Reflector
from .safety import SafetyGuard
from .sensitive import (SECRET_RE, Scrubber, focus_target, is_password_el, is_sensitive_type, safe_view)
from .verify import Check, Verdict, Verifier
from .verify.receipts import make_receipt, summarize_postconditions

TERMINAL_STATUSES = {"done", "fail", "uncertain", "step_limit", "budget_exhausted", "time_limit", "user_abort",
                     "privacy_blocked", "invalid_checkpoint"}


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
    performance: dict = field(default_factory=dict)
    modality: dict = field(default_factory=dict)       # v0.6：各执行模态次数 / 回退 / 后台侵入 / 后台生效核验
    receipts: list = field(default_factory=list)       # v0.6：每个子目标的“已验证完成”凭据（含证据）


StepHook = Callable[[int, "GUIAgent"], None]


class GUIAgent:
    def __init__(self, env: Env, planner: Planner, actor: Actor, grounder: Grounder, verifier: Verifier,
                 recovery: RecoveryPolicy, cfg: AgentConfig, budget: Budget,
                 logger: Optional[TrajectoryLogger] = None, memory: Optional[Memory] = None,
                 reflector: Optional[Reflector] = None, guard: Optional[SafetyGuard] = None,
                 policy: Optional[CapabilityPolicy] = None, hybrid: Optional[HybridExecutor] = None,
                 tools=None):
        self.env, self.planner, self.actor, self.grounder = env, planner, actor, grounder
        self.verifier, self.recovery, self.cfg, self.budget = verifier, recovery, cfg, budget
        self.log = logger
        self.mem = memory or Memory()
        self.reflector = reflector or Reflector(None, enabled=False)
        self.guard = guard or SafetyGuard(mode="deny")
        # v0.6 混合执行器：工具通道 + 语义（后台）动作 + 已验证的前台回退
        # 直接构造（未传 hybrid）时保持 v0.5 的恢复行为：不自动换模态；build_agent 按配置打开
        self.hybrid = hybrid or HybridExecutor(env, tools, HybridConfig(modality_recovery=False))
        if self.hybrid.observe is None:
            self.hybrid.observe = lambda: self._observe()
        if tools is not None and self.hybrid.tools is None:
            self.hybrid.tools = tools
        if self.guard.tools is None:
            self.guard.tools = self.hybrid.tools
        self.policy = policy or CapabilityPolicy(
            a11y_grounding=grounder.use_a11y, a11y_rules=verifier.use_a11y,
            a11y_in_prompts=getattr(getattr(actor, "policy", None), "a11y_in_prompts", True),
            step_trigger=verifier.trigger, llm_step_verify=getattr(verifier, "llm_step", True),
            llm_goal_verify=getattr(verifier, "llm_goal", True), llm_reflection=self.reflector.enabled,
            goal_check=cfg.verify_goals, final_check=cfg.final_check, final_l2=cfg.final_l2,
            on_uncertain=cfg.on_uncertain,
            recovery="none" if not recovery.enabled else "fixed_retry" if recovery.fixed_retry else "classified")
        self.step_no = 0
        self.receipts: list[dict] = []
        self._sg_steps: list[dict] = []
        self._last_goal: Optional[tuple] = None
        self._done_ids: list[int] = []
        self._subgoals: list = []
        self.before_step: list[StepHook] = []
        self._t0 = time.monotonic()
        self._answer = ""
        self.performance = Performance()
        # 秘密清洗器与日志 / 安全闸门共用（日志落盘、评测结果行、RunResult、确认回调都经过它）。
        # 配置里的秘密以 explicit=True 登记：即便短于 min_len（例如 4 位 PIN）也清洗、并立即置敏感
        # → 该次运行后续不再向任何模型发送截图（严格阻断，见 _note_obs / EgressGate）。
        self.scrubber = logger.scrubber if logger is not None else Scrubber()
        self.scrubber.extend((str(v) for v in (cfg.secrets or {}).values()), explicit=True)
        self.guard.scrubber = self.scrubber
        self._install_egress_gates()
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
        with self.performance.measure("settle"):
            obs, stable = self.env.wait_until_stable(timeout=self.cfg.settle_timeout, interval=self.cfg.settle_interval)
        self._note_obs(obs)
        return obs, stable

    def _observe(self, *args, **kwargs) -> Observation:
        """所有观察的统一入口：拿到观察就立刻登记敏感信号（早于任何模型请求与 logger.shot）。"""
        with self.performance.measure("observe"):
            obs = self.env.observe(*args, **kwargs)
        self._note_obs(obs)
        return obs

    def _note_obs(self, obs: Optional[Observation]) -> None:
        """观察里出现密码框 / 明确无法确定的焦点 → 进入严格阻断（单调，不因下一帧没有密码元素而解除）。

        秘密一旦被配置或识别，本次运行后续不再向任何模型发送截图，仅发脱敏文字 / 元素。
        """
        if obs is None or self.scrubber.images_blocked:
            return
        if focus_target(obs)[1] in {"password", "unknown"}:
            self.scrubber.mark_sensitive()
            return
        for e in getattr(obs, "elements", None) or []:
            if is_password_el(e):
                self.scrubber.mark_sensitive()
                return

    def _vision_required(self, role: str, comp) -> bool:
        """纯视觉角色在无图状态下无法可靠执行 → 被禁时抛 PrivacyBlocked，不伪装成功。"""
        if role == "grounder":
            return True                     # 定位本质是视觉；能走无障碍匹配时根本不调用模型
        if role == "actor":
            if isinstance(comp, UITarsActor) or type(comp).__name__ == "ClaudeComputerUseActor":
                return True
            if getattr(comp, "coord_space", None):
                return True                 # 直接输出坐标的 actor 依赖截图
            pol = getattr(comp, "policy", None)
            return bool(pol is not None and not getattr(pol, "a11y_in_prompts", True))
        if role == "verifier":
            pol = getattr(self, "policy", None)
            return bool(pol is not None and not getattr(pol, "a11y_in_prompts", True))
        return False                        # planner / reflector 有文字上下文，无图也能继续

    def _install_egress_gates(self) -> None:
        """给每个组件的模型套上统一出口门控（chat + post 都覆盖，含 CU 直接 llm.post 的路径）。"""
        for role, comp in (("planner", self.planner), ("actor", self.actor), ("grounder", self.grounder),
                           ("verifier", self.verifier), ("reflector", self.reflector)):
            llm = getattr(comp, "llm", None)
            if llm is None or isinstance(llm, EgressGate):
                continue
            comp.llm = EgressGate(llm, self.scrubber, role=role,
                                  vision_required=self._vision_required(role, comp))

    def _expand_secrets(self, text: str) -> tuple[str, list[str]]:
        """把 <secret>名字</secret> 展开成原文；未知名字不静默输入占位符本身，而是记录下来。"""
        missing: list[str] = []

        def sub(m):
            name = m.group(1)
            if name in (self.cfg.secrets or {}):
                return str(self.cfg.secrets[name])
            missing.append(name)
            return ""
        return SECRET_RE.sub(sub, text), missing

    def _limit(self) -> Optional[tuple[str, str]]:
        if self.step_no >= self.cfg.max_steps:
            return "step_limit", "global step limit"
        b = self.budget.exhausted()
        if b:
            return "budget_exhausted", b
        if self.cfg.max_seconds and time.monotonic() - self._t0 > self.cfg.max_seconds:
            return "time_limit", f"wall-clock limit {self.cfg.max_seconds}s"
        return None

    def _resolve(self, a: Action, obs: Observation, zoom_around=None) -> tuple[Action, str]:
        """坐标换算 + 把 target 描述 / element_id 变成截图像素坐标。"""
        w, h = obs.screenshot.size
        if a.type in TOOL_ACTIONS:
            return a, "-"
        if a.type == "invoke":
            match = self.grounder.match_a11y(obs, a.target or "", a.element_id)
            if match is None:
                return a, "grounding_failed"
            a.element_id = match[0].id
            return a, match[1]
        if a.type == "type" and (a.element_id is not None or a.target):
            if not self.env.targeted_input:
                return a, "grounding_failed"
            match = self.grounder.match_a11y(obs, a.target or "", a.element_id)
            if match is None or match[0].role != "textbox":
                return a, "grounding_failed"
            a.element_id = match[0].id
            a.x, a.y = match[0].center
            return a, match[1]
        # 动作自带的 transform（actor 回复里“实际发送尺寸”）优先，见 coords.ImageTransform
        to_pixel_action(a, w, h, getattr(self.actor, "max_pixels", getattr(self.grounder.mapper, "max_pixels",
                                                                           1280 * 28 * 28)))
        if a.is_pointer and (a.x is None or a.element_id is not None):
            g = self.grounder.ground(obs, a.target or "", a.element_id, zoom_around)
            if g is None:
                return a, "grounding_failed"
            a.x, a.y = g.x, g.y
            if g.element is not None:
                a.element_id = g.element.id
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
    def _targeted_type(self, a: Action, obs: Optional[Observation], origin: str) -> ExecResult:
        epoch = self.env.input_epoch
        element = obs.element(a.element_id) if obs is not None and a.element_id is not None else None
        identity = self.env.element_identity(element) if element is not None else None
        if not self.env.targeted_input or element is None or element.role != "textbox" or identity is None:
            now = time.time()
            return ExecResult(False, "stale_target: input target could not be identified", now, now)
        focus = Action("click", x=a.x, y=a.y, element_id=element.id, target=a.target, binding=a.binding)
        result = self._execute_gated(focus, obs, origin + ":focus")
        if not result.ok:
            return result
        fresh = self._observe()
        focused, _ = focus_target(fresh)
        if (self.env.input_epoch != epoch or focused is None or self.env.element_identity(focused) != identity
                or focused.is_password != element.is_password or focused.role != "textbox"):
            now = time.time()
            return ExecResult(False, "stale_target: intended field did not retain focus; no text sent", now, now)
        # Input and submit get a second, independent consent check using the
        # actual focus. Never batch across a navigation or an unverified focus.
        return self._execute_gated(replace(a, element_id=None, target=None, x=None, y=None, binding=None),
                                   fresh, origin + ":input")

    def _execute_gated(self, a: Action, obs: Optional[Observation], origin: str = "actor") -> ExecResult:
        """所有真正发往环境的动作都从这里走：先过安全闸门，拒绝即返回 blocked_by_safety（终止性，不重试）。

        次序（第三轮审查「秘密替换与闸门次序」）：
          1) 先把 <secret>名字</secret> 展开成 exec_a（同一个真实动作）；
          2) 用 exec_a 过 SafetyGuard.gate —— 控制字符 / 危险模式 / 激活提交语义都按**真实内容**检查
             （反例：secret="rm -rf /"、带换行的真实密码都必须被真实内容命中）；
          3) 只有放行才把 exec_a 发往环境。
        原动作 a 与安全摘要保持脱敏；未知占位符不静默输入原文，而是返回 blocked_by_safety 并说明缺少配置。
        """
        if (a.binding and obs is not None and obs.snapshot_id
                and a.binding.get("snapshot_id") != obs.snapshot_id):
            now = time.time()
            return ExecResult(False, "stale_target: action belongs to another observation", now, now)
        if a.type == "type" and a.element_id is not None:
            return self._targeted_type(a, obs, origin)
        if obs is not None and a.binding is None:
            a = self.env.bind_action(a, obs)
        state = self.guard.focus(obs)[1]
        view, _ = self._view(a, obs)                 # 原动作的安全摘要（占位符 / 敏感文本一律脱敏）
        exec_a = a
        missing: list[str] = []
        if (a.type == "type" or (a.type == "invoke" and a.method == "set_value")) and a.text:
            if SECRET_RE.search(a.text):
                expanded, missing = self._expand_secrets(a.text)
                exec_a = replace(a, text=expanded)
            elif is_sensitive_type(a, obs, state):
                self.scrubber.add(a.text, explicit=True)   # 真实敏感输入登记（短 PIN 也登记）
        if missing:
            why = ("secret placeholder(s) not configured: " + ", ".join(sorted(set(missing)))
                   + "; refusing to type the placeholder literally")
            if self.log:
                self.log.step(kind="safety", step=self.step_no, origin=origin, action=view,
                              reason=why, approved=False)
            t = time.time()
            return ExecResult(False, f"blocked_by_safety: {why}", t, t)
        approved, why = self.guard.gate(exec_a, obs)
        why = self._scrub(why)
        if (why or not approved) and self.log:
            self.log.step(kind="safety", step=self.step_no, origin=origin, action=view,
                          reason=why, approved=approved)
        if not approved:
            t = time.time()
            return ExecResult(False, f"blocked_by_safety: {why}", t, t)
        with self.performance.measure("execute"):
            if exec_a.type in TOOL_ACTIONS:
                res = self.hybrid.run_tool(exec_a)
            elif exec_a.type == "invoke":
                element = obs.element(exec_a.element_id) if obs is not None else None
                identity = self.env.element_identity(element) if element is not None else None
                res, fallback = self.hybrid.run_semantic(exec_a, obs)
                if fallback is not None:
                    self.hybrid.stats["fallbacks"] += 1
                    if self.log:
                        self.log.step(kind="modality_fallback", step=self.step_no, origin=origin,
                                      reason=self._scrub(res.error), route=res.route)
                    fresh = self._observe()
                    candidates = [e for e in fresh.elements if identity is not None
                                  and self.env.element_identity(e) == identity]
                    # Test-only backends without stable bindings keep their
                    # old grounding; real backends must prove the same node.
                    if identity is None and not fresh.snapshot_id and element is not None:
                        candidates = [e for e in fresh.elements if e.name == element.name and e.role == element.role]
                    if len(candidates) != 1 or element is None or candidates[0].is_password != element.is_password:
                        now = time.time()
                        second = ExecResult(False, "stale_target: fallback target changed; observe again", now, now)
                    else:
                        current = candidates[0]
                        fallback = replace(fallback, element_id=current.id, x=current.center[0], y=current.center[1],
                                           binding=None)
                        second = self._execute_gated(fallback, fresh, origin + ":fallback")
                    res = wrap_fallback_result(res, second)
                    self.hybrid._count(fallback, res)
            else:
                res = self.env.execute(exec_a)
                res.signals.setdefault("modality", modality_of(exec_a, res.route))
        self.performance.execution(res.route)
        res.error, res.output = self._scrub(res.error), self._scrub(res.output)   # 执行层报错可能回显输入
        if uncertain_activation(exec_a, obs) and any(s in res.error for s in ("background_no_effect", "native_action_error", "background_intrusion")):
            self.guard.remember_uncertain(exec_a, obs)
        if not res.ok and "blocked_by_safety" in (res.error or ""):
            self.guard.remember_denial(a, obs, res.error)      # 环境层（例如 Web 白名单）拦截也是终止性的
        return res

    def _remember_unconfirmed(self, a: Action, before: Observation, res: ExecResult, check: Check) -> None:
        if (res.ok and uncertain_activation(a, before)
                and check.verdict != Verdict.SUCCESS):
            self.guard.remember_uncertain(a, before)

    def _settled_for_check(self, baseline: Optional[Observation] = None) -> tuple[Observation, bool]:
        """收尾核验用的观察：未稳定或仍有忙碌指示时再等待复查（最多 busy_rechecks 次）。返回 (观察, 是否已稳定)。

        v0.3.1：Verifier.busy 收口为 busy(obs)（忙碌是当前屏幕的绝对信号，不再用 baseline 做豁免）。
        """
        obs, stable = self._settle()
        for _ in range(max(0, self.cfg.busy_rechecks)):
            if stable and not self.verifier.busy(obs):
                break
            obs, stable = self._settle()
        return obs, stable

    def _run_recovery_actions(self, actions: list[Action], obs: Observation, origin: str) -> list[str]:
        """执行恢复动作（每个都过安全闸门）；返回被拦下的动作说明。被拦后不再继续执行后续恢复动作。"""
        blocked = []
        for ra in actions:
            if ra.type in {"invoke", "click"} and ra.element_id is None and ra.target and ra.x is None:
                m = self.grounder.match_a11y(obs, ra.target)        # 换模态恢复：在当前观察上重新绑定元素
                if m is None:
                    continue
                ra.element_id = m[0].id
                if ra.type == "click":
                    ra.x, ra.y = m[0].center
            shown = self._view(ra, obs)[1]
            r = self._execute_gated(ra, obs, origin)
            if not r.ok and "blocked_by_safety" in r.error:
                blocked.append(f"{shown} ({r.error})")
                break
        if actions:
            self._settle()
        return blocked

    # ------------------------------------------------------------------ main
    def run(self, task: str, resume: Optional[dict] = None) -> RunResult:
        """resume：上一次运行的 checkpoint.json 内容。已完成的子目标先在当前屏幕上**重新验证**，
        通过才跳过（凭据标注 re_verified_on_resume），否则从该子目标开始重新执行。"""
        self._t0 = time.monotonic()
        self.performance = Performance()
        self._replans = 0
        self._resume = resume
        try:
            if resume:
                if resume.get("task", "").strip() != task.strip():
                    return self._finish("invalid_checkpoint", False, 0, "checkpoint belongs to another task")
                state = resume.get("safety_state") or {}
                self.guard.denied.update(state.get("denied") or {})
                self.guard.uncertain.update(state.get("uncertain") or {})
                if state.get("privacy_blocked"):
                    self.scrubber.mark_sensitive()
                    # The checkpoint deliberately does not serialize private
                    # plaintext needed to reconstruct every redaction rule.
                    raise PrivacyBlocked("sensitive checkpoint needs its original private session; refusing unsafe resume")
            return self._run(task)
        except BudgetExceeded as e:
            if self.log:
                self.log.step(kind="budget_exhausted", step=self.step_no, reason=e.reason, resource=e.resource)
            return self._finish("budget_exhausted", False, self._replans, e.reason)
        except UserAbort as e:
            if self.log:
                self.log.step(kind="user_abort", step=self.step_no, reason=str(e))
            return self._finish("user_abort", False, self._replans, f"user abort: {e}")
        except PrivacyBlocked as e:
            # 纯视觉路径（CU / UI-TARS / 视觉 grounding / vision-only）在截图被禁后无法可靠执行：
            # 明确以受限状态终止，绝不伪装成功。
            if self.log:
                self.log.step(kind="privacy_blocked", step=self.step_no, reason=str(e))
            return self._finish("privacy_blocked", False, self._replans, str(e))

    def _resume_plan(self, obs: Observation) -> tuple[list, int]:
        """按 checkpoint 恢复：子目标列表 + 第一个需要执行的下标（已完成的先重新验证）。"""
        cp = self._resume or {}
        subgoals = [Subgoal(**{k: v for k, v in d.items() if k in Subgoal.__dataclass_fields__})
                    for d in cp.get("subgoals", [])]
        done = set(cp.get("done_ids", []))
        idx = 0
        for sg in subgoals:
            if sg.id not in done:
                break
            fobs, stable = self._settled_for_check(None)
            with self.performance.measure("verify"):
                c = self.verifier.check_goal(fobs, sg.goal, sg.evidence or sg.expected, sg.expect_text or None,
                                             stable=stable, baseline=None,
                                             postconditions=getattr(sg, "postconditions", None))
            if c.verdict != Verdict.SUCCESS:
                if self.log:
                    self.log.step(kind="resume_reverify_failed", subgoal=sg, evidence=self._scrub(c.evidence))
                break
            self._done_ids.append(sg.id)
            self.receipts.append(make_receipt("subgoal", sg.goal, "re_verified_on_resume", c.level,
                                              self._scrub(c.evidence), fobs, [],
                                              {"subgoal_id": sg.id, "expect_text": sg.expect_text}))
            idx += 1
        return subgoals, idx

    def _checkpoint(self, task: str) -> None:
        if not self.log:
            return
        from dataclasses import asdict
        self.log.write_json("checkpoint.json", {"task": task, "subgoals": [asdict(s) for s in self._subgoals],
                                                "done_ids": list(self._done_ids), "step_no": self.step_no,
                                                "replans": self._replans, "saved_at": time.time(),
                                                "safety_state": {"denied": dict(self.guard.denied),
                                                                 "uncertain": dict(self.guard.uncertain),
                                                                 "privacy_blocked": self.scrubber.images_blocked}})

    def _run(self, task: str) -> RunResult:
        self._replans = 0
        obs = self._observe()
        self._task_baseline = obs
        idx = 0
        if getattr(self, "_resume", None) and self._resume.get("subgoals"):
            subgoals, idx = self._resume_plan(obs)
        else:
            with self.performance.measure("plan"):
                subgoals = self.planner.plan(task, obs)
        self._subgoals = subgoals
        self._task = task
        if self.log:
            self.log.meta(task_text=task, platform=self.cfg.platform, task_window=self.cfg.task_window,
                          policy=self.policy.to_dict(), resumed=bool(getattr(self, "_resume", None)))
            self.log.step(kind="plan", subgoals=subgoals, resumed_from=idx)
        final_retries = 0

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
                obs = self._observe()
                self.mem.invalidate_after(sg.id)
                rest = self.planner.replan(task, obs, sg, self._scrub(notes), self.mem.milestones_text(), sg.id,
                                           self._scrub(self.mem.notes_text()))
                subgoals = subgoals[:idx] + rest
                self._subgoals = subgoals
                if self.log:
                    self.log.step(kind="replan", failed=sg, notes=notes, subgoals=rest)

            # 任务收尾核验：只看当前屏幕；聚合整个任务 + 全部子目标；明确 SUCCESS 才算完成
            if not self.cfg.final_check:
                self.receipts.append(make_receipt("task", task, "unverified", "off", "final check disabled",
                                                  None, []))
                return self._finish("done", True, self._replans, "final check disabled")
            fobs, fstable = self._settled_for_check(self._task_baseline)
            with self.performance.measure("verify"):
                final = self.verifier.check_final(fobs, task, subgoals, self.cfg.final_l2, stable=fstable,
                                                  baseline=self._task_baseline,
                                                  exempt_ids={r["subgoal_id"] for r in self.receipts
                                                              if r.get("verdict") == "re_verified_on_resume"})
            final.evidence = self._scrub(final.evidence)
            if self.log:
                self.log.step(kind="final_check", verdict=final.verdict, evidence=final.evidence, level=final.level,
                              signals=final.signals)
            self.receipts.append(make_receipt(
                "task", task, "verified_done" if final.verdict == Verdict.SUCCESS else final.verdict.value,
                final.level, final.evidence, fobs, [],
                {"subgoals": [r.get("subject") for r in self.receipts if r.get("kind") == "subgoal"]}))
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
            obs = self._observe()
            failed = Subgoal(0, f"final check of the whole task: {task}", "all task requirements hold on screen")
            rest = self.planner.replan(task, obs, failed, self._scrub(note), self.mem.milestones_text(),
                                       max((sg.id for sg in subgoals), default=0) + 1,
                                       self._scrub(self.mem.notes_text()))
            idx = len(subgoals)
            subgoals = subgoals + rest
            self._subgoals = subgoals
            if self.log:
                self.log.step(kind="replan", failed=failed, notes=note, subgoals=rest)

    # ------------------------------------------------------------------
    def _run_subgoal(self, task: str, sg: Subgoal, total: int) -> tuple[str, str]:
        feedback, last_failure = "", None
        zoom_around = None
        self._sg_baseline = None
        self._sg_steps = []
        actor_task = self._actor_task(task)
        for _ in range(self.cfg.max_steps_per_subgoal):
            lim = self._limit()
            if lim:
                return lim
            self.step_no += 1
            for hook in self.before_step:
                hook(self.step_no, self)
            before = self._observe()
            if self._sg_baseline is None:
                self._sg_baseline = before          # 子目标开始时的界面：旧证据基线
            if self._precheck_focus(sg, before):
                feedback = "The task window had lost focus; it was restored before acting."
                continue
            try:
                with self.performance.measure("decide"):
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
                    self.mem.add_milestone(Milestone(sg.id, sg.goal, ev, time.time(), self._observe(False).screenshot))
                    gc, gobs = self._last_goal or (None, None)
                    receipt = make_receipt("subgoal", sg.goal, "verified_done" if self.cfg.verify_goals else "unverified",
                                           gc.level if gc is not None else "off", ev, gobs, self._sg_steps,
                                           {"subgoal_id": sg.id, "expect_text": sg.expect_text,
                                            "signals": self.scrubber.scrub_obj(dict(gc.signals)) if gc is not None else {}})
                    self.receipts.append(receipt)
                    self._done_ids.append(sg.id)
                    self._checkpoint(task)
                    if self.log:
                        self.log.step(kind="milestone", subgoal=sg, evidence=ev, answer=action.text)
                        self.log.step(kind="receipt", receipt=receipt)
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
            with self.performance.measure("ground"):
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
            with self.performance.measure("verify"):
                check = self.verifier.check_step(before, after, action, res, stable, expected=sg.expected,
                                                 task_window=self.cfg.task_window, expect_text=sg.expect_text or None,
                                                 action_desc=view_s)
            self._remember_unconfirmed(action, before, res, check)
            if hasattr(self.actor, "record_outcome"):      # v0.7：批量动作适配器据此“停止于首个失败”
                self.actor.record_outcome(action, res, check)
            check.signals["execution_route"] = res.route
            check.signals["modality"] = res.signals.get("modality") or modality_of(action, res.route)
            for k in ("background", "pointer_moved", "focus_stolen", "fallback_reason", "background_effect",
                      "fallback_from"):
                if k in res.signals:
                    check.signals[k] = res.signals[k]
            if res.output and action.type in TOOL_ACTIONS:
                check.signals["tool_output"] = self._scrub(res.output)[:400]
            check.evidence = self._scrub(check.evidence)
            rec = StepRecord(self.step_no, sg.id, view_s, check.verdict.value, check.evidence)

            if check.verdict == Verdict.SUCCESS:
                feedback, last_failure = "", None
            else:
                if self.mem.is_looping():
                    last_failure = "repeat"
                if (self.hybrid.cfg.modality_recovery and self.hybrid.cfg.mode != "gui_only"
                        and getattr(self.env, "semantic_actions", False)):
                    plan = self.recovery.decide(check, action, self.cfg.task_window, last_failure, obs=before,
                                                hybrid=True)
                else:      # 兼容自定义 RecoveryPolicy 子类的旧签名
                    plan = self.recovery.decide(check, action, self.cfg.task_window, last_failure)
                rec.recovery = plan.strategy.value
                last_failure = check.verdict.value
                blocked = self._run_recovery_actions(plan.actions, after, f"recovery:{plan.strategy.value}")
                if plan.strategy == Strategy.SWITCH_MODALITY and plan.actions and not blocked:
                    # 换模态恢复后立即用同一套规则重新验证“原来的意图”是否已达成；达成才算恢复成功
                    obs2, st2 = self._settle()
                    t_now = time.time()
                    c2 = self.verifier.check_step(before, obs2, action, ExecResult(True, "", t_now, t_now,
                                                                                    route="recovery:switch_modality"),
                                                  st2, expected=sg.expected, task_window=self.cfg.task_window,
                                                  expect_text=sg.expect_text or None, action_desc=view_s)
                    c2.evidence = self._scrub(c2.evidence)
                    if c2.verdict == Verdict.SUCCESS:
                        check.signals["recovered_by"] = "switch_modality"
                        rec.verdict, rec.evidence = "success", f"recovered via modality switch: {c2.evidence}"[:300]
                        feedback = (f"The previous action had no visible effect; the same intent was completed via "
                                    f"another modality and verified ({c2.evidence[:160]}). Continue with the next step.")
                        last_failure = None
                        self.mem.add_step(rec)
                        self._log_step(rec, before, obs2, view_d, src, check, thought)
                        continue
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
        with self.performance.measure("verify"):
            c: Check = self.verifier.check_goal(obs, sg.goal, sg.evidence or sg.expected, sg.expect_text or None,
                                                stable=stable, baseline=self._sg_baseline,
                                                postconditions=getattr(sg, "postconditions", None))
        self._last_goal = (c, obs)
        return c.verdict == Verdict.SUCCESS, self._scrub(f"[{c.level}] {c.evidence}")

    def _log_step(self, rec: StepRecord, before, after, action_view: dict, src, check: Check, thought: str) -> None:
        sig = check.signals or {}
        self._sg_steps.append({"step": rec.step, "action": action_view, "verdict": rec.verdict, "level": check.level,
                               "modality": sig.get("modality", "gui" if rec.action.startswith("{") else "control"),
                               "route": sig.get("execution_route", ""),
                               "background": sig.get("background", False),
                               "fallback": sig.get("fallback_reason", ""),
                               "postconditions": summarize_postconditions(sig.get("postconditions")),
                               "recovery": rec.recovery or ""})
        if not self.log:
            return
        self.log.step(kind="step", step=rec.step, subgoal=rec.subgoal_id, thought=self._scrub(thought),
                      action=action_view, grounding=src, verdict=check.verdict, level=check.level,
                      evidence=check.evidence, signals=check.signals, recovery=rec.recovery,
                      window=after.active_window, url=after.url,
                      before=self.log.shot(rec.step, "before", before.screenshot),
                      after=self.log.shot(rec.step, "after", after.screenshot),
                      calls_so_far=self.budget.calls, performance=self.performance.summary())

    def _finish(self, status: str, claimed: bool, replans: int, msg: str) -> RunResult:
        assert status in TERMINAL_STATUSES, status
        claimed = claimed and status == "done"          # 只有 done 才算“宣称完成”
        r = RunResult(status, claimed, self.step_no, replans, list(self.recovery.history), self.budget,
                      time.monotonic() - self._t0, self._scrub(msg), self._scrub(self._answer),
                      self.scrubber.scrub_obj(list(self.guard.log)), self.policy.to_dict(), self.performance.summary(),
                      self.scrubber.scrub_obj(dict(self.hybrid.stats, mode=self.hybrid.cfg.mode,
                                                   dispatch=self.hybrid.cfg.dispatch)),
                      self.scrubber.scrub_obj(list(self.receipts)))
        if self.log:
            self.log.meta(result=r)
            self.log.write_json("receipts.json", r.receipts)
            if getattr(self, "_task", None) is not None:
                self._checkpoint(self._task)
        return r


def _redact(a: Action, obs: Optional[Observation]) -> dict:
    """兼容 v0.3 的旧接口：等价于 gua.sensitive.safe_view（脱敏由输入目标决定）。"""
    return safe_view(a, obs)

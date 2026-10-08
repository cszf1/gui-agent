"""从 YAML 构建整套 agent。消融实验只需要换配置文件；模型配置可以单独一个文件叠加（--model）。"""
from __future__ import annotations

import os

from pathlib import Path
from typing import Any, Optional

import yaml

from .agent import AgentConfig, GUIAgent
from .coords import CoordMapper
from .env import detect_platform, make_env
from .env.base import Env
from .grounding import Grounder
from .hybrid import HybridConfig, HybridExecutor
from .llm import Budget, BudgetGate, make_llm
from .logger import TrajectoryLogger
from .planner import PLATFORM_DESC, Actor, Planner, UITarsActor
from .policy import CapabilityPolicy
from .recovery import RecoveryPolicy
from .reflection import Reflector
from .safety import SafetyGuard
from .tools import ToolRegistry
from .verify import Verifier


def load_config(path: str | Path, overrides: Optional[dict[str, Any]] = None,
                extra_files: Optional[list[str | Path]] = None) -> dict[str, Any]:
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    base = cfg.pop("extends", None)
    if base:
        cfg = merge(load_config(Path(path).parent / base), cfg)
    for f in extra_files or []:
        cfg = merge(cfg, load_config(f))
    if overrides:
        cfg = merge(cfg, overrides)
    return cfg


def merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def resolve_platform(cfg: dict) -> str:
    p = (cfg.get("env") or {}).get("platform", "auto")
    return detect_platform() if p in (None, "", "auto") else p


def build_env(cfg: dict, platform: Optional[str] = None) -> Env:
    e = dict(cfg.get("env") or {})
    p = platform or resolve_platform(cfg)
    e.update(e.pop(p, {}) or {})            # 平台专属小节，例如 env.web.headless
    for k in ("windows", "macos", "linux", "android", "web", "mock", "remote", "platform"):
        e.pop(k, None)
    if p == "web" and not e.get("allowed_domains"):
        # v0.3：safety.allowed_domains 也在浏览器层强制执行（不只是 navigate 检查）
        e["allowed_domains"] = list((cfg.get("safety") or {}).get("allowed_domains") or [])
    return make_env(p, **e)


def build_agent(cfg: dict, env: Env, logger: Optional[TrajectoryLogger] = None,
                llms: Optional[dict] = None, task_window: str = "",
                confirm_fn=None, ask_fn=None) -> GUIAgent:
    """llms 可注入任意 chat 对象（测试 / 脚本策略），键：planner / actor / grounder / verifier / reflector。

    v0.3：
    - 先由配置推导唯一的 CapabilityPolicy，所有组件只读它（审查条目 6）；
    - 每个模型对象都包一层 BudgetGate：调用前强制检查预算（审查条目 3），注入的模型也不例外；
    - 策略禁止的模型角色（例如纯规则基线的 verifier / reflector）根本不会被构造。
    """
    platform = getattr(env, "platform", "mock")
    policy = CapabilityPolicy.from_config(cfg)
    budget = Budget()
    m = cfg.get("models") or {}
    llms = dict(llms or {})

    def get(role: str, fallback=None):
        if role in llms:
            return llms[role]
        spec = m.get(role)
        if spec is None:
            return fallback
        return make_llm(spec, role, budget)

    planner_llm = get("planner")
    actor_llm = get("actor", planner_llm)
    ground_llm = get("grounder", None)
    ver_llm = get("verifier", actor_llm) if (policy.llm_step_verify or policy.llm_goal_verify) else None
    refl_llm = get("reflector", ver_llm if ver_llm is not None else actor_llm) if policy.llm_reflection else None

    gated: dict[int, BudgetGate] = {}

    def gate(l, role):
        if l is None:
            return None
        if id(l) not in gated:
            gated[id(l)] = BudgetGate(l, budget, role)
        return gated[id(l)]
    planner_g, actor_g = gate(planner_llm, "planner"), gate(actor_llm, "actor")
    ground_g, ver_g, refl_g = gate(ground_llm, "grounder"), gate(ver_llm, "verifier"), gate(refl_llm, "reflector")

    g = cfg.get("grounding") or {}
    max_pixels = g.get("max_pixels", 1280 * 28 * 28)
    grounder = Grounder(ground_g, CoordMapper(g.get("coord", "norm1000"), max_pixels),
                        use_a11y=policy.a11y_grounding, zoom_factor=g.get("zoom_factor", 2.5),
                        platform_desc=PLATFORM_DESC.get(platform, platform), fuzzy=g.get("fuzzy", True))
    v = cfg.get("verification") or {}
    verifier = Verifier(ver_g, platform=platform, policy=policy)
    r = cfg.get("recovery") or {}
    recovery = RecoveryPolicy(max_waits=r.get("max_waits", 3),
                              max_recoveries_per_subgoal=r.get("max_recoveries_per_subgoal", 4),
                              allow_undo=r.get("allow_undo", False), enabled=policy.recovery != "none",
                              fixed_retry=policy.recovery == "fixed_retry", platform=platform,
                              scroll_unit_px=getattr(env, "scroll_unit_px", 100))
    reflector = Reflector(refl_g, enabled=policy.llm_reflection)
    s = cfg.get("safety") or {}
    web_domains = ((cfg.get("env") or {}).get("web") or {}).get("allowed_domains") or []
    guard = SafetyGuard(enabled=s.get("enabled", True), mode=s.get("mode", "confirm"),
                        allowed_domains=s.get("allowed_domains") or web_domains,
                        extra_risky_words=s.get("extra_risky_words") or [], confirm_fn=confirm_fn, ask_fn=ask_fn,
                        allowed_apps=s.get("allowed_apps") or [])
    tools = ToolRegistry.from_config(cfg)
    guard.tools = tools
    hcfg = HybridConfig.from_config(cfg)
    hybrid = HybridExecutor(env, tools, hcfg)
    a = cfg.get("agent") or {}
    acfg = AgentConfig(max_steps=a.get("max_steps", 50), max_steps_per_subgoal=a.get("max_steps_per_subgoal", 15),
                       max_replans=a.get("max_replans", 2), settle_timeout=a.get("settle_timeout", 5.0),
                       settle_interval=a.get("settle_interval", 0.4),
                       task_window=task_window or a.get("task_window", ""),
                       verify_goals=policy.goal_check, final_check=policy.final_check, final_l2=policy.final_l2,
                       on_uncertain=policy.on_uncertain, max_budget_calls=a.get("max_budget_calls", 200),
                       max_tokens=a.get("max_tokens"), max_cost_usd=a.get("max_cost_usd"),
                       max_seconds=a.get("max_seconds"), platform=platform,
                       # v0.3.1：秘密只从环境变量读取（agent.secrets_env: {名字: 环境变量名}），配置文件里不写明文
                       secrets={n: os.environ[v] for n, v in (a.get("secrets_env") or {}).items() if os.environ.get(v)})
    act = cfg.get("actor") or {}
    kind = act.get("kind", "json")
    act_pixels = act.get("max_pixels", max_pixels)
    if kind == "uitars":
        actor = UITarsActor(actor_g, platform, coord_space=act.get("coord_space", "resized"),
                            language=act.get("language", "Chinese"), max_pixels=act_pixels, policy=policy)
    elif kind == "claude_computer_use":
        from .llm.anthropic import ClaudeComputerUseActor
        # 直接用 AnthropicLLM（它的 post() 在请求前自行检查预算；BudgetGate 不能代理属性赋值）
        actor_llm.budget = budget
        actor = ClaudeComputerUseActor(actor_llm, platform)
    else:
        actor = Actor(actor_g, platform, act.get("max_elements_in_prompt", 80), act.get("coord_space"),
                      policy=policy, max_pixels=act_pixels)
        if hcfg.mode != "gui_only":
            actor.semantic_hint = env.semantic_methods if getattr(env, "semantic_actions", False) else None
            actor.extra_docs = tools.prompt_docs()
    return GUIAgent(env, Planner(planner_g, platform, policy=policy), actor, grounder, verifier, recovery, acfg,
                    budget, logger, reflector=reflector, guard=guard, policy=policy, hybrid=hybrid, tools=tools)

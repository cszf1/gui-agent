"""从 YAML 构建整套 agent。消融实验只需要换配置文件；模型配置可以单独一个文件叠加（--model）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

from .agent import AgentConfig, GUIAgent
from .coords import CoordMapper
from .env import detect_platform, make_env
from .env.base import Env
from .grounding import Grounder
from .llm import Budget, make_llm
from .logger import TrajectoryLogger
from .planner import PLATFORM_DESC, Actor, Planner, UITarsActor
from .recovery import RecoveryPolicy
from .reflection import Reflector
from .safety import SafetyGuard
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
    for k in ("windows", "macos", "linux", "android", "web", "mock", "platform"):
        e.pop(k, None)
    return make_env(p, **e)


def build_agent(cfg: dict, env: Env, logger: Optional[TrajectoryLogger] = None,
                llms: Optional[dict] = None, task_window: str = "",
                confirm_fn=None, ask_fn=None) -> GUIAgent:
    """llms 可注入任意 chat 对象（测试 / 脚本策略），键：planner / actor / grounder / verifier / reflector。"""
    platform = getattr(env, "platform", "mock")
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
    ver_llm = get("verifier", actor_llm)
    refl_llm = get("reflector", ver_llm)
    for l in (planner_llm, actor_llm, ground_llm, ver_llm, refl_llm):
        if l is not None:
            l.budget = budget

    g = cfg.get("grounding") or {}
    grounder = Grounder(ground_llm, CoordMapper(g.get("coord", "norm1000"), g.get("max_pixels", 1280 * 28 * 28)),
                        use_a11y=g.get("use_a11y", g.get("use_uia", True)), zoom_factor=g.get("zoom_factor", 2.5),
                        platform_desc=PLATFORM_DESC.get(platform, platform), fuzzy=g.get("fuzzy", True))
    v = cfg.get("verification") or {}
    verifier = Verifier(ver_llm, trigger=v.get("trigger", "on_event"), use_a11y=v.get("use_a11y", True),
                        platform=platform)
    r = cfg.get("recovery") or {}
    recovery = RecoveryPolicy(max_waits=r.get("max_waits", 3),
                              max_recoveries_per_subgoal=r.get("max_recoveries_per_subgoal", 4),
                              allow_undo=r.get("allow_undo", False), enabled=r.get("enabled", True),
                              fixed_retry=r.get("fixed_retry", False), platform=platform,
                              scroll_unit_px=getattr(env, "scroll_unit_px", 100))
    rf = cfg.get("reflection") or {}
    reflector = Reflector(refl_llm, enabled=rf.get("enabled", True))
    s = cfg.get("safety") or {}
    web_domains = ((cfg.get("env") or {}).get("web") or {}).get("allowed_domains") or []
    guard = SafetyGuard(enabled=s.get("enabled", True), mode=s.get("mode", "confirm"),
                        allowed_domains=s.get("allowed_domains") or web_domains,
                        extra_risky_words=s.get("extra_risky_words") or [], confirm_fn=confirm_fn, ask_fn=ask_fn)
    a = cfg.get("agent") or {}
    acfg = AgentConfig(max_steps=a.get("max_steps", 50), max_steps_per_subgoal=a.get("max_steps_per_subgoal", 15),
                       max_replans=a.get("max_replans", 2), settle_timeout=a.get("settle_timeout", 5.0),
                       settle_interval=a.get("settle_interval", 0.4),
                       task_window=task_window or a.get("task_window", ""),
                       verify_goals=v.get("verify_goals", True), max_budget_calls=a.get("max_budget_calls", 200),
                       max_tokens=a.get("max_tokens"), max_seconds=a.get("max_seconds"), platform=platform)
    act = cfg.get("actor") or {}
    kind = act.get("kind", "json")
    if kind == "uitars":
        actor = UITarsActor(actor_llm, platform, coord_space=act.get("coord_space", "resized"),
                            language=act.get("language", "Chinese"))
    elif kind == "claude_computer_use":
        from .llm.anthropic import ClaudeComputerUseActor
        actor = ClaudeComputerUseActor(actor_llm, platform)
    else:
        actor = Actor(actor_llm, platform, act.get("max_elements_in_prompt", 80), act.get("coord_space"))
    return GUIAgent(env, Planner(planner_llm, platform), actor, grounder, verifier, recovery, acfg, budget, logger,
                    reflector=reflector, guard=guard)

"""模态消融基准（v0.6）：gui_only vs hybrid、前台 vs 后台、注入故障下的验证驱动回退。

!!! 这不是模型能力证据 !!!
所有决策来自任务 JSON 里的固定脚本（ScriptedPolicy），没有任何真实模型调用。它测量的是**执行层**：
同一串意图分别用真实输入（前台）和无障碍语义动作（后台）执行时，判分结果、步数、耗时、指针 / 前台
被打扰的次数，以及“后台调用返回成功但没生效”时验证与回退是否把结果拉回正确。

第 1 部分（真实沙箱）：本机 Xvfb + openbox + AT-SPI + GTK 应用（gua/sandbox），任务 tasks/sandbox/
  form_save、slow_save；判分只读应用自己写出的 result.json。
第 2 部分（mock 注入故障）：后台调用被静默丢弃（background_drop）/ 真实点击被透明层吃掉（pointer_dead），
  对比 gui_only、hybrid（换模态恢复开 / 关）。

用法：python scripts/benchmark_modalities.py --repeats 3 --output docs/benchmarks/modalities.json
"""
from __future__ import annotations

import argparse
import copy
import json
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gua.config import load_config  # noqa: E402

LABEL = "scripted policy (fixed demo steps), NOT model evidence"


def _median(xs):
    return round(statistics.median(xs), 3) if xs else None


class CountingEnv:
    """包一层 RemoteEnv：统计每次执行前后沙箱指针是否移动、前台窗口是否变化（侵入性指标）。"""

    def __init__(self, env):
        self._env = env
        self.pointer_moves = 0
        self.foreground_changes = 0
        self.actions = 0

    def __getattr__(self, k):
        return getattr(self._env, k)

    def execute(self, a):
        p0, f0 = self._env.pointer_position(), self._env.foreground_token()
        r = self._env.execute(a)
        p1, f1 = self._env.pointer_position(), self._env.foreground_token()
        self.actions += 1
        self.pointer_moves += int(p0 != p1)
        self.foreground_changes += int(bool(f0) and bool(f1) and f0 != f1)
        return r


def sandbox_part(repeats: int, runs: str) -> list[dict]:
    from gua.eval.runner import load_tasks, run_task
    from gua.sandbox.local import LocalSandbox, requirements
    req = requirements()
    if not all(req[k] for k in ("Xvfb", "xdotool", "pyatspi", "gtk")):
        return [{"skipped": f"sandbox prerequisites missing: {req}"}]
    tasks = [t for t in load_tasks(ROOT / "tasks" / "sandbox") if t["id"] in {"sandbox_form_save", "sandbox_slow_save"}]
    base = load_config(str(ROOT / "configs" / "default.yaml"), {"safety": {"mode": "deny"},
                                                                "agent": {"settle_timeout": 2.0}})
    configs = {
        "gui_only/foreground": {"hybrid": {"mode": "gui_only"}},
        "hybrid/foreground": {"hybrid": {"mode": "hybrid", "dispatch": "foreground"}},
        "hybrid/background": {"hybrid": {"mode": "hybrid", "dispatch": "background"}},
    }
    rows = []
    with LocalSandbox(liveview=False) as sb:
        raw = sb.remote_env()
        raw.snapshot("pristine")
        for rep in range(repeats):
            for name, over in configs.items():           # 交替运行，减少顺序偏差
                for task in tasks:
                    cfg = copy.deepcopy(base)
                    for k, v in over.items():
                        cfg.setdefault(k, {}).update(v)
                    env = CountingEnv(raw)
                    t0 = time.monotonic()
                    row = run_task(cfg, task, env, runs, rep, False, f"bench-{name.replace('/', '-')}", "scripted")
                    rows.append({"part": "sandbox", "config": name, "task": task["id"], "rep": rep,
                                 "passed": row["passed"], "claimed_done": row["claimed_done"],
                                 "false_done": row["claimed_done"] and not row["passed"], "steps": row["steps"],
                                 "seconds": round(time.monotonic() - t0, 2), "pointer_moves": env.pointer_moves,
                                 "foreground_changes": env.foreground_changes, "modality": row["modality"],
                                 "fallbacks": row["fallbacks"], "status": row["status"]})
                    print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    return rows


def mock_part(repeats: int) -> list[dict]:
    from gua.agent import AgentConfig, GUIAgent
    from gua.coords import CoordMapper
    from gua.env.mock import MockButton, MockEnv
    from gua.grounding import Grounder
    from gua.hybrid import HybridConfig, HybridExecutor
    from gua.llm.base import Budget, ScriptedLLM
    from gua.planner import Actor, Planner
    from gua.recovery import RecoveryPolicy
    from gua.safety import SafetyGuard
    from gua.verify import Verifier

    plan = json.dumps({"subgoals": [{"goal": "save", "expected": "saved", "expect_text": "saved=True"}]})
    # (控件角色, actor 发出的意图, 注入的故障)。actor 只看提示词里的元素列表决定是否 done。
    scenarios = {
        "toggle-intent/normal": ("checkbox", {"type": "invoke", "method": "toggle", "target": "Save"}, {}),
        "toggle-intent/background_drop": ("checkbox", {"type": "invoke", "method": "toggle", "target": "Save"},
                                          {"background_drop": True}),
        "invoke-intent/background_drop": ("button", {"type": "invoke", "method": "invoke", "target": "Save"},
                                          {"background_drop": True}),
        "click-intent/normal": ("button", {"type": "click", "target": "Save"}, {}),
        "click-intent/pointer_dead": ("button", {"type": "click", "target": "Save"}, {"pointer_dead": True}),
        "select-intent/background_drop": ("radio", {"type": "invoke", "method": "select", "target": "Save"},
                                          {"background_drop": True}),
        "select-click-intent/pointer_dead": ("radio", {"type": "click", "target": "Save"}, {"pointer_dead": True}),
    }
    configs = {
        "gui_only": HybridConfig(mode="gui_only", settle=0.0, modality_recovery=False),
        "hybrid+modality_recovery": HybridConfig(mode="hybrid", settle=0.0, modality_recovery=True),
        "hybrid-no_modality_recovery": HybridConfig(mode="hybrid", settle=0.0, modality_recovery=False),
    }
    rows = []
    for rep in range(repeats):
        for sname, (role, intent, flags) in scenarios.items():
            for cname, hcfg in configs.items():
                def mark(e):
                    e.state["saved"] = True
                    e.state["activations"] = e.state.get("activations", 0) + 1
                    if not any(b.name == "Saved badge" for b in e.buttons):
                        e.buttons.append(MockButton("Saved badge", (300, 10, 420, 40)))
                env = MockEnv(title="Editor", buttons=[MockButton("Save", (10, 10, 200, 40), role=role,
                                                                  checked=False if role in {"checkbox", "radio"} else None,
                                                                  on_click=mark, **flags)])
                orig = env.wait_until_stable
                env.wait_until_stable = lambda timeout=0.3, **kw: orig(timeout=0.3, interval=0.01)

                def actor(s, t, i, intent=intent):
                    if "'Saved badge'" in t:
                        return '{"action":{"type":"done"}}'
                    return json.dumps({"action": intent})
                budget = Budget()
                agent = GUIAgent(env, Planner(ScriptedLLM(fn=lambda s, t, i: plan, budget=budget), "mock"),
                                 Actor(ScriptedLLM(fn=actor, budget=budget), "mock"), Grounder(None, CoordMapper("pixel")),
                                 Verifier(None), RecoveryPolicy(platform="mock"),
                                 AgentConfig(max_steps=10, max_steps_per_subgoal=6, max_replans=1, settle_timeout=0.3),
                                 budget, guard=SafetyGuard(mode="deny"), hybrid=HybridExecutor(env, None, copy.copy(hcfg)))
                t0 = time.monotonic()
                res = agent.run("save")
                # 通过 = 保存恰好发生一次（没有因为“后台无证据就重放”而保存两次）
                passed = env.state.get("saved") is True and env.state.get("activations") == 1
                rows.append({"part": "mock_fault", "scenario": sname, "config": cname, "rep": rep, "passed": passed,
                             "activations": env.state.get("activations", 0),
                             "claimed_done": res.claimed_done, "false_done": res.claimed_done and not passed,
                             "steps": res.steps, "seconds": round(time.monotonic() - t0, 3),
                             "modality": res.modality.get("modality", {}), "fallbacks": res.modality.get("fallbacks", 0),
                             "recoveries": res.recoveries, "status": res.status})
    return rows


def table(rows: list[dict], keys: tuple) -> list[dict]:
    groups: dict = {}
    for r in rows:
        if "skipped" in r:
            continue
        groups.setdefault(tuple(r[k] for k in keys), []).append(r)
    out = []
    for g, rs in groups.items():
        out.append({**dict(zip(keys, g)), "runs": len(rs), "pass": f"{sum(r['passed'] for r in rs)}/{len(rs)}",
                    "false_done": sum(r["false_done"] for r in rs), "median_steps": _median([r["steps"] for r in rs]),
                    "median_seconds": _median([r["seconds"] for r in rs]),
                    "pointer_moves_median": _median([r["pointer_moves"] for r in rs]) if "pointer_moves" in rs[0] else None,
                    "fallbacks": sum(r.get("fallbacks", 0) for r in rs)})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--output", default="")
    ap.add_argument("--skip-sandbox", action="store_true")
    a = ap.parse_args()
    runs = tempfile.mkdtemp(prefix="gua-bench-")
    sandbox = [] if a.skip_sandbox else sandbox_part(a.repeats, runs)
    mock = mock_part(a.repeats)
    result = {"label": LABEL, "date": time.strftime("%Y-%m-%d"), "host": {"python": sys.version.split()[0],
                                                                         "machine": platform.machine(),
                                                                         "system": platform.system()},
              "repeats": a.repeats,
              "sandbox_summary": table(sandbox, ("config", "task")), "mock_fault_summary": table(mock, ("scenario", "config")),
              "rows": sandbox + mock}
    print(json.dumps({k: result[k] for k in ("label", "sandbox_summary", "mock_fault_summary")}, ensure_ascii=False,
                     indent=2))
    if a.output:
        Path(a.output).parent.mkdir(parents=True, exist_ok=True)
        Path(a.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

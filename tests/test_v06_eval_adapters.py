"""v0.6 评测适配：OSWorld 风格 JSON 子集转换、Cua-Bench 风格任务（官方示例原样运行 + GUI 任务 oracle / agent）、
沙箱任务集的并行运行。需要沙箱前置条件的测试会自动跳过。"""
import json
import sys
from pathlib import Path

import pytest

from gua.eval.osworld import UnsupportedTask, convert
from gua.eval.runner import load_tasks

ROOT = Path(__file__).resolve().parents[1]


def test_osworld_subset_conversion_and_honest_rejection():
    t = json.loads((ROOT / "tasks" / "osworld_subset" / "gua_form_pro.json").read_text(encoding="utf-8"))
    g = convert(t)
    assert g["platform"] == "remote" and g["setup"][0] == {"type": "sandbox_launch", "argv": ["gua-form"],
                                                           "wait_for": "Save", "timeout": 15}
    assert g["checks"][0]["type"] == "osworld_eval"
    for bad in ({"config": [{"type": "download", "parameters": {}}]},
                {"evaluator": {"func": "compare_table", "result": {"type": "vm_file"}}},
                {"evaluator": {"func": "exact_match", "result": {"type": "cloud_file"}}},
                {"evaluator": {"func": ["exact_match", "exact_match"]}}):
        base = {"id": "x", "instruction": "y", "config": [],
                "evaluator": {"func": "exact_match", "result": {"type": "vm_file", "path": "a"}}}
        base.update(bad)
        with pytest.raises(UnsupportedTask):
            convert(base)


def test_load_tasks_routes_osworld_json_and_keeps_existing_tasks():
    ids = {t["id"] for t in load_tasks(ROOT / "tasks")}
    assert "osworld_gua-form-pro-synthetic" in ids and "web_form_submit" in ids and "sandbox_form_save" in ids


def test_cuabench_shim_loads_official_example_registry():
    from gua.eval.cuabench_compat import load_task_module
    reg = load_task_module(ROOT / "tasks" / "cuabench" / "hello_file_env")
    assert set(reg["train"]) == {"tasks_config", "setup_task", "solve_task", "evaluate_task"}
    variants = reg["train"]["tasks_config"]()
    assert [v.metadata["word"] for v in variants] == ["hello", "bench"]
    assert "cua_bench" not in sys.modules or not getattr(sys.modules["cua_bench"], "__gua_shim__", False)


# ----------------------------------------------------------------------------- 真实沙箱
def _sandbox_ok():
    if not sys.platform.startswith("linux"):
        return False
    from gua.sandbox.local import requirements
    r = requirements()
    return all(r[k] for k in ("Xvfb", "xdotool", "openbox", "dbus-launch", "pyatspi", "gtk"))


needs_sandbox = pytest.mark.skipif(not _sandbox_ok(), reason="sandbox prerequisites missing")


@pytest.fixture(scope="module")
def shell_box():
    if not _sandbox_ok():
        pytest.skip("sandbox prerequisites missing")
    from gua.sandbox.local import LocalSandbox
    sb = LocalSandbox(shell=True, liveview=False).start()
    env = sb.remote_env()
    env.snapshot("pristine")
    yield sb, env
    sb.stop()


@needs_sandbox
@pytest.mark.sandbox
def test_official_cuabench_example_runs_unchanged_via_oracle(shell_box):
    from gua.eval.cuabench_compat import run_task_dir
    _, env = shell_box
    rows = run_task_dir(ROOT / "tasks" / "cuabench" / "hello_file_env", env, mode="oracle")
    assert [r["reward"] for r in rows] == [1.0, 1.0]


@needs_sandbox
@pytest.mark.sandbox
def test_cuabench_gui_task_oracle_and_gua_agent(shell_box):
    from gua.agent import AgentConfig, GUIAgent
    from gua.coords import CoordMapper
    from gua.eval.cuabench_compat import run_task_dir
    from gua.grounding import Grounder
    from gua.hybrid import HybridConfig, HybridExecutor
    from gua.llm.base import Budget, ScriptedLLM
    from gua.planner import Actor, Planner
    from gua.recovery import RecoveryPolicy
    from gua.safety import SafetyGuard
    from gua.verify import Verifier
    _, env = shell_box
    task_dir = ROOT / "tasks" / "cuabench" / "gua_form_subscribe"
    oracle = run_task_dir(task_dir, env, mode="oracle")
    assert [r["passed"] for r in oracle] == [True, True]

    def factory(env, task):
        name = task.metadata["name"]
        steps = iter([{"type": "invoke", "method": "set_value", "target": "Name", "text": name},
                      {"type": "invoke", "method": "toggle", "target": "Subscribe"},
                      {"type": "invoke", "method": "invoke", "target": "Save"}] + [{"type": "done"}] * 4)
        plan = json.dumps({"subgoals": [{"goal": "fill and save", "expect_text": f"Saved: {name}"}]})
        b = Budget()
        return GUIAgent(env, Planner(ScriptedLLM(fn=lambda s, t, i: plan, budget=b), "remote"),
                        Actor(ScriptedLLM(fn=lambda s, t, i: json.dumps({"action": next(steps)}), budget=b), "remote"),
                        Grounder(None, CoordMapper("pixel")), Verifier(None), RecoveryPolicy(platform="remote"),
                        AgentConfig(max_steps=8, settle_timeout=2.0, platform="remote"), b,
                        guard=SafetyGuard(mode="deny"), hybrid=HybridExecutor(env, None, HybridConfig(settle=0.15)))
    agent_rows = run_task_dir(task_dir, env, mode="agent", agent_factory=factory)
    assert [r["passed"] for r in agent_rows] == [True, True]
    assert all(r["status"] == "done" and r["verified_receipts"] >= 1 for r in agent_rows)


@needs_sandbox
@pytest.mark.sandbox
def test_osworld_subset_task_runs_in_sandbox(shell_box, tmp_path):
    from gua.config import load_config
    from gua.eval.runner import run_task
    _, env = shell_box
    task = load_tasks(ROOT / "tasks" / "osworld_subset")[0]
    task["demo"] = {"subgoals": [{"goal": "fill", "expect_text": "Saved: Kai", "steps": [
        {"set_value": "Name", "text": "Kai"}, {"invoke": "Pro", "method": "select"},
        {"invoke": "Save", "method": "invoke"}]}]}
    cfg = load_config(str(ROOT / "configs" / "default.yaml"), {"safety": {"mode": "deny"},
                                                               "agent": {"settle_timeout": 2.0}})
    row = run_task(cfg, task, env, str(tmp_path), policy="scripted", with_disturbance=False)
    assert row["passed"] and row["status"] == "done", row["check_notes"]


@needs_sandbox
@pytest.mark.sandbox
def test_parallel_eval_with_sandbox_pool(tmp_path):
    from gua.config import load_config
    from gua.eval.runner import run_suite_parallel
    tasks = [t for t in load_tasks(ROOT / "tasks" / "sandbox") if t["id"] in {"sandbox_form_save", "sandbox_slow_save"}]
    cfg = load_config(str(ROOT / "configs" / "default.yaml"), {"safety": {"mode": "deny"},
                                                               "agent": {"settle_timeout": 2.0}})
    s = run_suite_parallel(cfg, tasks, str(tmp_path), policy="scripted", workers=2, quiet=True)
    assert s["runs"] == 2 and s["success_rate"] == 1.0 and s["false_done_rate"] == 0.0
    assert sorted(r["worker"] for r in s["_rows"]) == [0, 1]
    assert s["modality_totals"].get("semantic", 0) >= 4

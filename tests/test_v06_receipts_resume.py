"""v0.6：已验证完成凭据、流式轨迹事件、断点续跑（先在当前屏幕重新验证再跳过）。"""
import json

from conftest import fast
from gua.agent import AgentConfig, GUIAgent
from gua.coords import CoordMapper
from gua.env.mock import MockButton, MockEnv
from gua.grounding import Grounder
from gua.hybrid import HybridConfig, HybridExecutor
from gua.llm.base import Budget, ScriptedLLM
from gua.logger import TrajectoryLogger
from gua.planner import Actor, Planner
from gua.recovery import RecoveryPolicy
from gua.safety import SafetyGuard
from gua.verify import Verifier

PLAN = json.dumps({"subgoals": [
    {"goal": "save", "expected": "saved", "expect_text": "saved=True"},
    {"goal": "publish", "expected": "published", "expect_text": "published=True"}]})


def make_env():
    def save(e):
        e.state["saved"] = True

    def publish(e):
        e.state["published"] = True
    return fast(MockEnv(title="Editor", buttons=[MockButton("Save", (10, 10, 120, 40), on_click=save),
                                                 MockButton("Continue", (10, 60, 120, 90), on_click=publish)]))


def actor_for(env, crash_after_save=False):
    def fn(s, t, i):
        if "Current sub-goal (1/" in t or "sub-goal (1/2): save" in t:
            if env.state.get("saved"):
                return '{"action":{"type":"done"}}'
            return '{"action":{"type":"invoke","method":"invoke","target":"Save"}}'
        if crash_after_save:
            raise KeyboardInterrupt("simulated crash")
        if env.state.get("published"):
            return '{"action":{"type":"done"}}'
        return '{"action":{"type":"click","target":"Continue"}}'
    return fn


def build(env, actor_fn, log=None, planner_fn=None):
    budget = Budget()
    planner = ScriptedLLM(fn=planner_fn or (lambda s, t, i: PLAN), budget=budget)
    return GUIAgent(env, Planner(planner, "mock"), Actor(ScriptedLLM(fn=actor_fn, budget=budget), "mock"),
                    Grounder(None, CoordMapper("pixel")), Verifier(None), RecoveryPolicy(platform="mock"),
                    AgentConfig(max_steps=12, settle_timeout=0.2), budget, log, guard=SafetyGuard(mode="deny"),
                    hybrid=HybridExecutor(env, None, HybridConfig(settle=0.0)))


def test_receipts_carry_evidence_steps_modalities_and_screen_digest(tmp_path):
    env = make_env()
    log = TrajectoryLogger(tmp_path, "r1", save_images=False)
    res = build(env, actor_for(env), log).run("save and publish")
    log.close(report=False)
    assert res.status == "done"
    kinds = [(r["kind"], r["verdict"]) for r in res.receipts]
    assert kinds == [("subgoal", "verified_done"), ("subgoal", "verified_done"), ("task", "verified_done")]
    sub = res.receipts[0]
    assert "L1" in sub["level"] and "saved=True" in sub["evidence"]
    assert sub["modalities"] == {"semantic": 1}
    assert sub["steps"][0]["route"] == "semantic:mock:invoke"
    assert len(sub["screen"]["screenshot_sha256"]) == 32 and sub["screen"]["window"] == "Editor"
    on_disk = json.loads((tmp_path / "r1" / "receipts.json").read_text())
    assert on_disk == json.loads(json.dumps(res.receipts))


def test_streaming_events_reach_subscribers_scrubbed(tmp_path):
    env = make_env()
    log = TrajectoryLogger(tmp_path, "r2", save_images=False)
    events = []
    log.subscribe(events.append)
    log.subscribe(lambda e: 1 / 0)          # 出错的订阅者不影响执行
    res = build(env, actor_for(env), log).run("save and publish")
    log.close(report=False)
    assert res.status == "done"
    kinds = [e.get("kind", "step") for e in events]
    assert kinds[0] == "plan" and "receipt" in kinds and "final_check" in kinds or "milestone" in kinds


def test_resume_reverifies_completed_subgoals_before_skipping(tmp_path):
    env = make_env()
    log = TrajectoryLogger(tmp_path, "r3", save_images=False)
    try:
        build(env, actor_for(env, crash_after_save=True), log).run("save and publish")
    except KeyboardInterrupt:
        pass
    log.close(report=False)
    cp = json.loads((tmp_path / "r3" / "checkpoint.json").read_text())
    assert cp["done_ids"] == [1] and len(cp["subgoals"]) == 2
    planned = []
    res = build(env, actor_for(env), planner_fn=lambda s, t, i: planned.append(1) or PLAN).run("save and publish",
                                                                                              resume=cp)
    assert res.status == "done" and not planned                # 不重新规划
    assert res.receipts[0]["verdict"] == "re_verified_on_resume"
    assert sum('"Save"' in x for x in env.log) == 1          # 已完成的子目标没有被重做


def test_resume_reruns_subgoal_whose_evidence_is_gone(tmp_path):
    env = make_env()
    cp = {"task": "save and publish", "subgoals": json.loads(PLAN)["subgoals"], "done_ids": [1]}
    for i, s in enumerate(cp["subgoals"], 1):
        s["id"] = i
    res = build(env, actor_for(env)).run("save and publish", resume=cp)   # 屏幕上没有 saved=True：必须重做
    assert res.status == "done" and env.state.get("saved")
    assert res.receipts[0]["verdict"] == "verified_done"

"""端到端（mock 平台，三大 OS 的 CI 都跑）：静默失败 / 弹窗 / 屏幕外目标 / 失焦 / 安全闸门 / ask_user / 预算。"""
import json

from conftest import fast
from gua.agent import AgentConfig, GUIAgent
from gua.coords import CoordMapper
from gua.env.mock import MockButton, MockEnv
from gua.grounding import Grounder
from gua.llm.base import Budget, ScriptedLLM
from gua.planner import Actor, Planner
from gua.recovery import RecoveryPolicy
from gua.reflection import Reflector
from gua.safety import SafetyGuard
from gua.verify import Verifier

PLAN = '{"subgoals":[{"goal":"click Save","expected":"state shows saved","evidence":"saved=True","expect_text":"saved=True"}]}'


def make_env(**kw):
    def save(env):
        env.state["saved"] = True
    btn = MockButton("Save", (100, 100, 220, 150), on_click=save, **kw)
    return fast(MockEnv(title="Mock Editor", buttons=[btn]))


def build(env, actor_fn, verifier_fn=None, cfg=None, plan=PLAN, guard=None, reflector_fn=None, recovery=True):
    budget = Budget()
    planner = ScriptedLLM(fn=lambda s, t, i: plan, budget=budget)
    actor = ScriptedLLM(fn=actor_fn, budget=budget)
    ver = ScriptedLLM(fn=verifier_fn or (lambda s, t, i: json.dumps(
        {"verdict": "success" if env.state.get("saved") else "no_effect", "evidence": str(env.state)})), budget=budget)
    grounder_llm = ScriptedLLM(fn=lambda s, t, i: '{"x": 160, "y": 125}', budget=budget)
    refl = Reflector(ScriptedLLM(fn=reflector_fn, budget=budget)) if reflector_fn else None
    return GUIAgent(env, Planner(planner, "mock"), Actor(actor, "mock"), Grounder(grounder_llm, CoordMapper("pixel")),
                    Verifier(ver), RecoveryPolicy(enabled=recovery, platform="mock"),
                    cfg or AgentConfig(max_steps=12, task_window="Mock", settle_timeout=0.3), budget,
                    guard=guard, reflector=refl)


def click_save_until_saved(env):
    def fn(s, t, i):
        if env.state.get("saved"):
            return '{"thought":"done","action":{"type":"done"}}'
        return '{"thought":"click save","action":{"type":"click","target":"Save"}}'
    return fn


def test_unacknowledged_click_is_not_replayed_or_claimed_done():
    env = make_env(flaky_first=True, delay_frames=1)
    res = build(env, click_save_until_saved(env)).run("save the document")
    assert res.status != "done" and not res.claimed_done
    assert not env.state.get("saved")
    assert any(r.startswith("no_effect") for r in res.recoveries)


def test_goal_check_blocks_false_done():
    env = fast(MockEnv(title="Mock Editor", buttons=[]))
    agent = build(env, lambda s, t, i: '{"action":{"type":"done"}}',
                  verifier_fn=lambda s, t, i: '{"verdict":"failed","evidence":"no saved marker"}',
                  plan='{"subgoals":[{"goal":"save","expected":"saved"}]}',
                  cfg=AgentConfig(max_steps=6, max_steps_per_subgoal=3, max_replans=1, settle_timeout=0.2))
    res = agent.run("save")
    assert res.status != "done" and not res.claimed_done


def test_unexpected_popup_is_blocked_then_handled():
    env = make_env()
    env.popup = None
    fired = []

    def actor_fn(s, t, i):
        if env.state.get("saved"):
            return '{"action":{"type":"done"}}'
        if "dialog" in t and "OK" in t:
            return '{"thought":"close popup","action":{"type":"click","target":"OK"}}'
        return '{"action":{"type":"click","target":"Save"}}'

    agent = build(env, actor_fn)

    def hook(step, ag):  # 第 1 步动作执行后才弹窗：用包装 execute 模拟“点击触发了意外弹窗”
        if step == 1 and not fired:
            orig = env.execute

            def wrapped(a):
                r = orig(a)
                if a.type == "click" and not fired:
                    fired.append(1)
                    env.state.pop("saved", None)
                    env.popup = "Unexpected update dialog"
                elif a.type == "click" and a.target == "OK":
                    # The dialog completes the pending save; re-clicking Save
                    # after an uncertain activation would replay the intent.
                    env.state["saved"] = True
                return r
            env.execute = wrapped
    agent.before_step.append(hook)
    res = agent.run("save")
    assert res.status == "done" and env.state.get("saved")
    assert any(r.startswith("blocked->dismiss") for r in res.recoveries), res.recoveries


def test_offscreen_target_scrolls_into_view():
    env = fast(MockEnv(title="Mock Editor", buttons=[
        MockButton("Save", (100, 1500, 220, 1550), on_click=lambda e: e.state.update(saved=True))]))
    res = build(env, click_save_until_saved(env)).run("save")
    assert res.status == "done" and env.state.get("saved")
    assert "failed->scroll" in res.recoveries and env.scroll_y > 0


def test_precheck_restores_focus_without_calling_actor():
    env = make_env()
    env.focused = False
    calls = []

    def actor_fn(s, t, i):
        calls.append(t)
        return click_save_until_saved(env)(s, t, i)
    res = build(env, actor_fn).run("save")
    assert res.status == "done" and "failed->refocus" in res.recoveries
    assert "Foreground: 'Mock Editor'" in calls[0]   # actor 第一次被调用时焦点已恢复


def test_safety_gate_denies_dangerous_click():
    env = fast(MockEnv(title="Mock Editor", buttons=[
        MockButton("Delete account", (100, 100, 300, 150), on_click=lambda e: e.state.update(deleted=True))]))
    asked = []
    guard = SafetyGuard(mode="confirm", confirm_fn=lambda a, why: asked.append(why) or False)
    agent = build(env, lambda s, t, i: '{"action":{"type":"click","target":"Delete account"}}',
                  plan='{"subgoals":[{"goal":"delete","expect_text":"deleted=True"}]}', guard=guard,
                  cfg=AgentConfig(max_steps=4, max_steps_per_subgoal=2, max_replans=0, settle_timeout=0.2,
                                  task_window="Mock"))
    res = agent.run("delete my account")
    assert not env.state.get("deleted") and asked and res.status == "fail"
    assert any(e["decision"] == "confirm" and not e["approved"] for e in res.safety_events)


def test_ask_user_answer_is_fed_back():
    env = make_env()
    seen = []

    def actor_fn(s, t, i):
        seen.append(t)
        if "The user answered: yes" not in t:
            return '{"action":{"type":"ask_user","text":"Save to default folder?"}}'
        return click_save_until_saved(env)(s, t, i)
    guard = SafetyGuard(mode="allow", ask_fn=lambda q: "yes")
    res = build(env, actor_fn, guard=guard).run("save")
    assert res.status == "done"


def test_budget_limit_stops_run():
    env = make_env()
    agent = build(env, lambda s, t, i: '{"action":{"type":"wait","seconds":0}}',
                  cfg=AgentConfig(max_steps=50, max_budget_calls=4, settle_timeout=0.1, task_window="Mock"))
    res = agent.run("save")
    # v0.3：状态名改为 budget_exhausted，且调用数是硬上限（== 4，绝不超过）
    assert res.status == "budget_exhausted" and res.budget.calls == 4 and not res.claimed_done


def test_unparseable_actor_output_gets_feedback():
    env = make_env()
    replies = iter(["I think I should click", '{"action":{"type":"click","target":"Save"}}'])

    def fn(s, t, i):
        if env.state.get("saved"):
            return '{"action":{"type":"done"}}'
        return next(replies)
    res = build(env, fn).run("save")
    assert res.status == "done"


def test_reflection_note_reaches_actor():
    env = make_env(flaky_first=True)
    prompts = []

    def actor_fn(s, t, i):
        prompts.append(t)
        return click_save_until_saved(env)(s, t, i)
    res = build(env, actor_fn, reflector_fn=lambda s, t, i: '{"diagnosis":"click ignored","advice":"click again"}').run("save")
    assert res.status != "done" and not res.claimed_done
    assert not env.state.get("saved")
    assert any("click again" in p for p in prompts)

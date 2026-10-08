"""v0.6 真实 Chromium：DOM 语义（后台）动作、遮挡时拒绝后台、后台生效核验与前台回退、混合 agent 端到端。"""
import json

import pytest

pw = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.web

from gua.actions import Action
from gua.agent import AgentConfig, GUIAgent
from gua.coords import CoordMapper
from gua.env.web import WebEnv
from gua.grounding import Grounder
from gua.hybrid import HybridConfig, HybridExecutor
from gua.llm.base import Budget, ScriptedLLM
from gua.planner import Actor, Planner
from gua.recovery import RecoveryPolicy
from gua.safety import SafetyGuard
from gua.verify import Verifier

PAGE = """<title>Prefs</title>
<label><input type=checkbox id=sub onchange="st.textContent='Subscribed: '+this.checked"> Subscribe</label>
<input id=name aria-label="Name" oninput="echo.textContent='Hello '+this.value">
<button id=save onclick="st2.textContent='Saved'">Save</button>
<p id=st>Subscribed: false</p><p id=echo></p><p id=st2></p>
<input type=password aria-label="Password" id=pw>
"""


@pytest.fixture
def env():
    instance = WebEnv(viewport=(800, 600))
    try:
        try:
            instance._ensure()
        except pw.Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("Chromium not installed")
            raise
        instance.page.set_content(PAGE)
        yield instance
    finally:
        instance.close()


def _el(obs, name, role=None):
    return next(e for e in obs.elements if e.name == name and (role is None or e.role == role))


def _bound(env, name, method, role=None, **kw):
    obs = env.observe()
    e = _el(obs, name, role)
    return obs, env.bind_action(Action("invoke", element_id=e.id, method=method, **kw), obs)


def test_dom_semantic_toggle_set_value_invoke_without_pointer(env):
    env.cursor = (700, 500)
    obs, a = _bound(env, "Subscribe", "toggle", "checkbox")
    r = env.execute(a)
    assert r.ok and r.route == "dom_semantic:toggle"
    assert env.page.evaluate("sub.checked") and "Subscribed: true" in env.page.inner_text("body")
    obs, a = _bound(env, "Name", "set_value", "textbox", text="Alice 中文")
    assert env.execute(a).ok
    assert env.page.evaluate("document.getElementById('name').value") == "Alice 中文" and "Hello Alice 中文" in env.page.inner_text("body")
    obs, a = _bound(env, "Save", "invoke", "button")
    assert env.execute(a).ok and "Saved" in env.page.inner_text("body")
    assert env.cursor == (700, 500)               # 页面指针从未移动


def test_dom_semantic_refuses_password_and_covered_targets(env):
    obs, a = _bound(env, "password field", "set_value", text="hunter2")
    r = env.execute(a)
    assert not r.ok and "blocked_by_safety" in r.error and env.page.evaluate("pw.value") == ""
    obs, a = _bound(env, "Save", "invoke", "button")
    env.page.evaluate("document.body.insertAdjacentHTML('beforeend','<div style=\"position:fixed;inset:0;"
                      "background:rgba(0,0,0,.3);z-index:9\">Modal</div>')")
    r = env.execute(a)
    assert not r.ok and r.error.startswith("background_unavailable") and "Saved" not in env.page.inner_text("body")


def test_dom_semantic_stale_binding_is_refused(env):
    obs, a = _bound(env, "Save", "invoke", "button")
    env.page.evaluate("save.textContent='Delete account'")
    r = env.execute(a)
    assert not r.ok and r.error.startswith("stale_target") and "Saved" not in env.page.inner_text("body")


def test_hybrid_executor_verifies_background_effect_on_real_dom(env):
    hy = HybridExecutor(env, None, HybridConfig(settle=0.05), observe=lambda: env.observe())
    obs, a = _bound(env, "Subscribe", "toggle", "checkbox")
    r, fb = hy.run_semantic(a, obs)
    assert r.ok and fb is None and r.signals["pointer_moved"] is False
    assert "checked=True" in r.signals["background_effect"]
    # 事件处理器吞掉 DOM click（模拟“后台投递被应用丢弃”）→ 没有生效证据 → toggle 改走前台
    env.page.evaluate("sub.addEventListener('click', e => { if (!e.isTrusted) e.preventDefault(); })")
    obs, a = _bound(env, "Subscribe", "toggle", "checkbox")
    r, fb = hy.run_semantic(a, obs)
    assert not r.ok and r.error.startswith("background_no_effect") and fb is not None and fb.type == "click"


def test_hybrid_agent_end_to_end_on_chromium(env):
    plan = json.dumps({"subgoals": [{"goal": "subscribe and enter name", "expected": "checked, greeting shown",
                                     "expect_text": "Hello Bob"}]})
    steps = iter([
        {"type": "invoke", "method": "toggle", "target": "Subscribe",
         "expect": [{"kind": "element_state", "name": "Subscribe", "checked": True}]},
        {"type": "invoke", "method": "set_value", "target": "Name", "text": "Bob",
         "expect": [{"kind": "text_appears", "text": "Hello Bob"}]},
        {"type": "done"},
    ])
    budget = Budget()
    agent = GUIAgent(env, Planner(ScriptedLLM(fn=lambda s, t, i: plan, budget=budget), "web"),
                     Actor(ScriptedLLM(fn=lambda s, t, i: json.dumps({"action": next(steps)}), budget=budget), "web"),
                     Grounder(None, CoordMapper("pixel")), Verifier(None), RecoveryPolicy(platform="web"),
                     AgentConfig(max_steps=6, settle_timeout=1.0, platform="web"), budget,
                     guard=SafetyGuard(mode="deny"), hybrid=HybridExecutor(env, None, HybridConfig(settle=0.05)))
    res = agent.run("subscribe as Bob")
    assert res.status == "done", res.message
    assert res.modality["modality"] == {"semantic": 2} and res.modality["background_verified"] == 2
    assert env.page.evaluate("sub.checked && document.getElementById('name').value === 'Bob'")

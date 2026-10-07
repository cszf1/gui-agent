"""Real Chromium: stale identities, focus-and-type and event-aware waits."""
import time
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.web

from gua.actions import Action
from gua.config import build_agent, load_config
from gua.env.web import WebEnv
from gua.scripted import ScriptedPolicy
from gua.verify import Verdict

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def env():
    instance = WebEnv(viewport=(800, 600))
    try:
        try:
            instance._ensure()
        except pw.Error as exc:
            if "Executable doesn't exist" in str(exc): pytest.skip("Chromium not installed")
            raise
        yield instance
    finally:
        instance.close()


def bound_click(env, name="Continue"):
    obs = env.observe()
    element = next(e for e in obs.elements if e.name == name)
    return env.bind_action(Action("click", element_id=element.id, x=element.center[0], y=element.center[1]), obs)


HTML = '<button id="target" onclick="window.correct=(window.correct||0)+1">Continue</button>'


def test_bound_click_follows_the_original_moving_element(env):
    env.page.set_content(HTML)
    action = bound_click(env)
    env.page.evaluate("target.style.marginLeft='400px'")
    env.page.evaluate("document.body.insertAdjacentHTML('beforeend', '<button style=\"position:absolute;left:8px;top:8px\" onclick=\"window.wrong=true\">Other</button>')")
    assert env.execute(action).ok
    assert env.page.evaluate("window.correct === 1 && !window.wrong")


@pytest.mark.parametrize("mutation", [
    "target.replaceWith(target.cloneNode(true))",
    "target.textContent='Delete account'",
    "target.disabled=true",
    "target.remove()",
    "document.body.insertAdjacentHTML('beforeend','<div style=\"position:fixed;inset:0;background:white;z-index:999\">Overlay</div>')",
])
def test_changed_or_covered_bound_target_never_activates_a_replacement(env, mutation):
    env.page.set_content(HTML)
    action = bound_click(env)
    env.page.evaluate(mutation)
    assert not env.execute(action).ok
    assert env.page.evaluate("!window.correct")


def test_hover_relabel_is_rechecked_before_activation(env):
    env.page.set_content(HTML.replace('id="target"', 'id="target" onmouseenter="this.textContent=\'Delete account\'"'))
    assert not env.execute(bound_click(env)).ok
    assert env.page.evaluate("!window.correct")


def test_checkbox_state_change_does_not_toggle_it_back(env):
    env.page.set_content('<label><input id="target" type="checkbox">Enabled</label>')
    action = bound_click(env, "Enabled")
    env.page.evaluate("target.checked=true")
    assert not env.execute(action).ok and env.page.locator('#target').is_checked()


def test_new_tab_and_new_observation_invalidate_old_binding(env):
    env.page.set_content(HTML)
    action = bound_click(env)
    env.observe()
    assert not env.execute(action).ok
    action = bound_click(env)
    env._ctx.new_page().set_content(HTML)
    assert not env.execute(action).ok
    assert env.page.evaluate("!window.correct")


def agent_for(env, **overrides):
    cfg = load_config(ROOT / "configs/web_local.yaml", {"reflection": {"enabled": False},
        "verification": {"llm": False}, "agent": {"max_replans": 0}, **overrides})
    return build_agent(cfg, env, llms=ScriptedPolicy({"subgoals": [{"goal": "fill"}]}).llms())


def test_targeted_type_enters_chinese_into_the_intended_field(env):
    env.page.set_content('<label for="a">Name</label><input id="a"><label for="b">Other</label><input id="b">')
    agent = agent_for(env)
    obs = agent._observe()
    action, _ = agent._resolve(Action("type", target="Name", text="测试用户", clear=True), obs)
    result = agent._execute_gated(action, obs)
    assert result.ok and env.page.locator('#a').input_value() == "测试用户"
    assert env.page.locator('#b').input_value() == ""


def test_targeted_type_sends_no_text_after_focus_is_stolen(env):
    env.page.set_content('<input id="a" aria-label="Name" onfocus="document.getElementById(\'b\').focus()"><input id="b" aria-label="Other">')
    agent = agent_for(env)
    obs = agent._observe()
    action, _ = agent._resolve(Action("type", target="Name", text="private payload"), obs)
    result = agent._execute_gated(action, obs)
    assert not result.ok
    assert env.page.locator('#a').input_value() == env.page.locator('#b').input_value() == ""


def test_pause_between_focus_and_type_discards_remaining_input(env, tmp_path, monkeypatch):
    from gua.desktop import ControlledEnv, Control, StreamingLogger
    control = Control(lambda *args, **kw: None)
    logger = StreamingLogger(tmp_path, "pause", lambda *args, **kw: None)
    env.page.set_content('<input aria-label="Name">')
    wrapped = ControlledEnv(env, control, logger)
    execute = env.execute
    def pause_after_focus(action):
        result = execute(action)
        control.command({"command": "pause"})
        control.command({"command": "resume"})
        return result
    monkeypatch.setattr(env, "execute", pause_after_focus)
    try:
        agent = agent_for(wrapped)
        obs = agent._observe()
        action, _ = agent._resolve(Action("type", target="Name", text="discard this"), obs)
        assert not agent._execute_gated(action, obs).ok
        assert env.page.locator('input').input_value() == ""
    finally:
        logger.close(report=False)


def test_targeted_submit_uses_actual_form_target_and_preserves_refusal(env):
    env.page.set_content('<form onsubmit="window.sent=true;return false"><input aria-label="Address"><button>Send</button></form>')
    agent = agent_for(env)
    obs = agent._observe()
    action, _ = agent._resolve(Action("type", target="Address", text="user@example.com", submit=True), obs)
    result = agent._execute_gated(action, obs)
    assert not result.ok and "blocked_by_safety" in result.error
    assert env.page.evaluate("!window.sent") and env.page.locator('input').input_value() == ""


def test_stability_works_on_focused_fields_without_screenshot_mutation(env):
    env.page.set_content('<input aria-label="Name">')
    env.page.locator('input').fill("Alice")
    obs, stable = env.wait_until_stable(timeout=2)
    assert stable and obs.snapshot_id and any(e.value == "Alice" for e in obs.elements)


def test_css_animation_and_canvas_changes_are_not_reported_stable(env):
    env.page.set_content('<canvas width="700" height="500"></canvas><script>let n=0;setInterval(()=>{let c=document.querySelector("canvas").getContext("2d");c.fillStyle=++n%2?"black":"white";c.fillRect(0,0,700,500)},80)</script>')
    _, stable = env.wait_until_stable(timeout=0.45, interval=0.05)
    assert not stable
    env.page.set_content('<style>@keyframes move{to{transform:translateX(400px)}}button{animation:move 3s linear}</style>' + HTML)
    _, stable = env.wait_until_stable(timeout=0.4)
    assert not stable


def test_still_busy_can_be_visually_stable_but_cannot_claim_completion(env):
    env.page.set_content('<p>Loading...</p><div aria-busy="true">Saved</div>')
    obs, stable = env.wait_until_stable(timeout=2)
    check = agent_for(env).verifier.check_goal(obs, "save", "Saved", "Saved", stable=stable)
    assert check.verdict != Verdict.SUCCESS


def test_reloaded_document_cannot_reuse_an_input_identity(env):
    env.page.set_content('<input aria-label="Name">')
    first = env.element_identity(env.observe().elements[0])
    env.page.reload()
    env.page.set_content('<input aria-label="Name">')
    assert env.element_identity(env.observe().elements[0]) != first

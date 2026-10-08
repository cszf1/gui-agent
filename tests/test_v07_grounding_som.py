"""v0.7：Set-of-Mark 观察融合与“放大复核”置信度定位。"""
from __future__ import annotations

import json
import time

from PIL import Image

from gua.coords import CoordMapper
from gua.env.base import Observation, UIElement
from gua.grounding import Grounder
from gua.llm.base import ScriptedLLM
from gua.planner import Actor, Subgoal
from gua.som import render_som, select_marks


def obs(elements):
    return Observation(Image.new("RGB", (400, 300), "white"), time.time(), (400, 300), elements=elements,
                       platform="mock", active_window="App")


ELS = [UIElement(3, "Save", "button", (50, 50, 150, 90)), UIElement(4, "secret", "textbox", (50, 150, 250, 180),
                                                                    is_password=True),
       UIElement(5, "Title", "text", (10, 10, 100, 30)), UIElement(6, "Hidden", "button", (0, 400, 50, 450),
                                                                   offscreen=True)]


def test_som_marks_only_visible_interactive_controls_and_draws_ids():
    o = obs(ELS)
    assert [e.id for e in select_marks(o)] == [3, 4]
    img = render_som(o)
    assert img.size == o.screenshot.size                 # geometry unchanged: coordinates stay valid
    assert img.getpixel((100, 90)) != (255, 255, 255)    # box border drawn
    assert img.getpixel((100, 70)) == (255, 255, 255)    # inside of the box untouched
    assert o.screenshot.getpixel((100, 90)) == (255, 255, 255)   # original observation not modified


def test_actor_sends_the_set_of_mark_image_when_enabled():
    seen = []
    llm = ScriptedLLM(fn=lambda s, t, i: (seen.append((t, i)), '{"action":{"type":"click","element_id":3}}')[1])
    actor = Actor(llm, "mock")
    actor.som = True
    o = obs(ELS)
    a, _ = actor.next_action("t", Subgoal(1, "save"), 1, o, "", "")
    prompt, images = seen[0]
    assert a.element_id == 3 and "numbered boxes" in prompt
    assert images[0].getpixel((100, 90)) != (255, 255, 255)


def _grounder(points):
    calls = []

    def fn(s, t, imgs):
        calls.append(imgs[0].size)
        return json.dumps(points[len(calls) - 1])
    return Grounder(ScriptedLLM(fn=fn), CoordMapper("pixel"), use_a11y=True, refine=True, zoom_factor=2.0), calls


def test_refined_grounding_agreeing_points_are_high_confidence():
    # full-screen point (100, 70); crop is 200x150 around it resized to 400x300 → point (200,140) maps back to ~(100,70)
    g, calls = _grounder([{"x": 100, "y": 70}, {"x": 200, "y": 140}])
    r = g.ground(obs([]), "Save button")
    assert r.source == "vlm_refined" and r.confidence >= 0.9 and abs(r.x - 100) <= 2 and abs(r.y - 70) <= 2
    assert len(calls) == 2


def test_refined_grounding_disagreement_is_low_confidence_and_agent_refuses_to_click():
    from conftest import fast
    from gua.agent import AgentConfig, GUIAgent
    from gua.env.mock import MockEnv
    from gua.llm.base import Budget
    from gua.planner import Planner
    from gua.recovery import RecoveryPolicy
    from gua.verify import Verifier
    g, _ = _grounder([{"x": 100, "y": 70}, {"x": 20, "y": 280}] * 20)
    r = g.ground(obs([]), "Save button")
    assert r.confidence < 0.5
    g.min_confidence = 0.6
    env = fast(MockEnv(title="Mock", buttons=[]))
    plan = '{"subgoals":[{"goal":"click save","expect_text":"never"}]}'
    agent = GUIAgent(env, Planner(ScriptedLLM(fn=lambda s, t, i: plan), "mock"),
                     Actor(ScriptedLLM(fn=lambda s, t, i: '{"action":{"type":"click","target":"Save button"}}'), "mock"),
                     g, Verifier(None), RecoveryPolicy(platform="mock"),
                     AgentConfig(max_steps=3, settle_timeout=0.2), Budget())
    agent.run("click save")
    assert not [line for line in env.log if json.loads(line)["type"] == "click"]

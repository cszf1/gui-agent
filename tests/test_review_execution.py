"""第四轮审查回归：指定位置的 Android 滚动与纯视觉步骤验证。"""
import shlex

import pytest
from PIL import Image

from gua.actions import Action
from gua.env.android import AndroidEnv
from gua.env.base import ExecResult, Observation
from gua.llm.base import ScriptedLLM
from gua.policy import CapabilityPolicy
from gua.verify import Verifier


@pytest.mark.parametrize("direction,endpoint", [
    ("down", (500, 700)), ("up", (500, 1300)),
    ("left", (800, 1000)), ("right", (200, 1000)),
])
@pytest.mark.parametrize("explicit", [True, False], ids=["positioned", "screen_center"])
def test_android_scroll_builds_numeric_endpoints_and_executes(direction, endpoint, explicit):
    calls = []
    env = AndroidEnv(runner=lambda args, binary: calls.append(args) or "")
    env._size = (1000, 2000)
    action = Action("scroll", direction=direction, amount=1,
                    **({"x": 500, "y": 1000} if explicit else {}))
    result = env.execute(action)
    assert result.ok, result.error
    assert len(calls) == 1
    assert shlex.split(calls[0][1]) == ["input", "swipe", "500", "1000",
                                       str(endpoint[0]), str(endpoint[1]), "300"]


@pytest.mark.parametrize("trigger", ["on_event", "every_step"])
@pytest.mark.parametrize("observation,include_text", [
    ({"use_a11y": False}, False),
    ({"a11y_in_prompts": False}, False),
    ({"use_a11y": True}, True),
])
def test_step_verifier_obeys_text_prompt_policy(trigger, observation, include_text):
    llm = ScriptedLLM(replies=['{"verdict":"success","evidence":"ok"}'])
    policy = CapabilityPolicy.from_config({"observation": observation,
                                           "verification": {"trigger": trigger}})
    verifier = Verifier(llm=llm, policy=policy)
    before = Observation(Image.new("RGB", (100, 100), "white"), 0, (100, 100),
                         text="DOM_ONLY_SENTINEL_BEFORE")
    after = Observation(Image.new("RGB", (100, 100), "black"), 1, (100, 100),
                        text="DOM_ONLY_SENTINEL_AFTER")
    verifier.check_step(before, after, Action("click", x=30, y=30), ExecResult(True),
                        True, expected="button clicked")
    assert len(llm.prompts) == 1
    for sentinel in ("DOM_ONLY_SENTINEL_BEFORE", "DOM_ONLY_SENTINEL_AFTER"):
        assert (sentinel in llm.prompts[0]) is include_text

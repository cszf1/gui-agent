"""Control identity, effect evidence and removal of fixed provider delays."""
from dataclasses import replace

import pytest
from PIL import Image

from gua.actions import Action
from gua.coords import CoordMapper
from gua.env.base import Observation, UIElement
from gua.env.uia_execution import activate_control, immediate_call, semantic_control
from gua.grounding import Grounder
from gua.hybrid import HybridConfig, HybridExecutor
from gua.verify.diff import _changed_ratio
from test_execution_routes import Control, observed


def observation(elements):
    return Observation(Image.new("RGB", (400, 300), "white"), 0, (400, 300), elements=elements)


@pytest.mark.parametrize("target,name,role", [
    ("Save button", "Save", "button"), ("保存按钮", "保存", "button"),
    ("button: Save", "Save", "button"), ("姓名输入框", "姓名", "textbox"),
    ("Subscribe check box", "Subscribe", "checkbox"), ("链接：设置", "设置", "link"),
])
def test_role_qualified_labels_resolve_without_a_model(target, name, role):
    wrong = "textbox" if role != "textbox" else "button"
    obs = observation([UIElement(1, name, wrong, (0, 0, 60, 30)),
                       UIElement(2, name, role, (100, 100, 160, 130))])
    grounder = Grounder(None, CoordMapper("pixel"))
    result = grounder.ground(obs, target)
    assert result is not None and result.element.id == 2 and result.confidence == 1.0
    assert grounder.match_a11y(obs, target, 1) is None


def test_role_labels_refuse_ambiguity_and_partial_or_disabled_targets():
    grounder = Grounder(None, CoordMapper("pixel"))
    save = UIElement(1, "Save", "button", (0, 0, 60, 30))
    assert grounder.ground(observation([save, replace(save, id=2)]), "Save button") is None
    assert grounder.ground(observation([replace(save, name="Save all")]), "Save button") is None
    assert grounder.ground(observation([replace(save, enabled=False)]), "Save button") is None


@pytest.mark.parametrize("semantic", [False, True])
def test_native_invoke_omits_default_sleep_and_never_replays_a_partial_error(semantic):
    class Immediate(Control):
        def Invoke(self, waitTime=0.5):
            self.wait_time = waitTime
            self.activations += 1
            if self.partial_failure:
                raise TypeError("provider failed after applying action")
    for failing in (False, True):
        control = Immediate(partial_failure=failing)
        binding = observed(control)
        result = semantic_control(binding, "invoke") if semantic else activate_control(binding)[0]
        assert control.wait_time == 0 and control.activations == 1
        assert result.ok is not failing


def test_provider_without_wait_argument_is_called_once():
    calls = []
    assert immediate_call(lambda: calls.append(1) or True)
    assert calls == [1]


def test_verified_background_toggle_returns_without_a_fixed_sleep(monkeypatch):
    import gua.hybrid as hybrid
    before = observation([UIElement(1, "Subscribe", "checkbox", (0, 0, 80, 30), checked=False)])
    after = observation([replace(before.elements[0], checked=True)])
    waits = []
    monkeypatch.setattr(hybrid.time, "sleep", waits.append)
    executor = HybridExecutor(None, cfg=HybridConfig(settle=0.25), observe=lambda: after)
    ok, evidence = executor._effect(Action("invoke", element_id=1, method="toggle"), before)
    assert ok is True and "checked=True" in evidence and waits == []


def test_background_verification_waits_for_delayed_effect_without_activating_again(monkeypatch):
    import gua.hybrid as hybrid
    before = observation([UIElement(1, "Name", "textbox", (0, 0, 80, 30), value="")])
    after = observation([replace(before.elements[0], value="Alice")])
    samples = iter([before, after])
    waits = []
    monkeypatch.setattr(hybrid.time, "sleep", waits.append)
    executor = HybridExecutor(None, cfg=HybridConfig(settle=0.25), observe=lambda: next(samples))
    assert executor._effect(Action("invoke", element_id=1, method="set_value", text="Alice"), before)[0] is True
    assert waits == [0.25]


def test_unrelated_text_change_does_not_verify_a_failed_value_write(monkeypatch):
    import gua.hybrid as hybrid
    before = observation([UIElement(1, "Name", "textbox", (0, 0, 80, 30), value="")])
    after = replace(before, text="Unrelated notification")
    monkeypatch.setattr(hybrid.time, "sleep", lambda seconds: None)
    executor = HybridExecutor(None, cfg=HybridConfig(settle=0.25), observe=lambda: after)
    assert executor._effect(Action("invoke", element_id=1, method="set_value", text="Alice"), before)[0] is False


def test_pixel_threshold_matches_original_for_every_gray_pair():
    first = Image.frombytes("L", (256, 256), bytes(i for i in range(256) for _ in range(256)))
    second = Image.frombytes("L", (256, 256), bytes(j for _ in range(256) for j in range(256)))
    expected = sum(abs(i / 255 - j / 255) > 0.08 for i in range(256) for j in range(256)) / 65536
    assert _changed_ratio(first, second) == expected

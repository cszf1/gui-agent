"""v0.6 子目标级后置条件：规划器解析 + 子目标核验 + 任务收尾核验（纯单元测试，无需沙箱）。"""
from PIL import Image

from gua.env.base import Observation, UIElement
from gua.planner import Planner, Subgoal
from gua.verify import Verdict, Verifier


def _obs(elements=(), text="", window="Gua Form", size=(400, 300)):
    return Observation(Image.new("RGB", size, (255, 255, 255)), 0.0, size, 1.0, window, "p", [window],
                       list(elements), "mock", "", text)


def _box(checked):
    return UIElement(1, "Subscribe", "checkbox", (10, 10, 60, 30), checked=checked)


PC = [{"kind": "element_state", "name": "Subscribe", "checked": True}]


def test_planner_keeps_valid_and_drops_invalid_postconditions():
    p = Planner.__new__(Planner)
    sgs = p._parse('{"subgoals": [{"goal": "tick", "postconditions": ['
                   '{"kind": "element_state", "name": "Subscribe", "checked": true},'
                   '{"kind": "bogus"}, "nope"]}, {"goal": "plain"}]}')
    assert sgs[0].postconditions == PC
    assert sgs[1].postconditions == []


def test_check_goal_uses_subgoal_postconditions():
    v = Verifier(None)
    ok = v.check_goal(_obs([_box(True)]), "tick", "", postconditions=PC)
    assert ok.verdict == Verdict.SUCCESS and ok.level == "goal-L1"
    bad = v.check_goal(_obs([_box(False)]), "tick", "", postconditions=PC)
    assert bad.verdict == Verdict.FAILED and "postcondition" in bad.evidence


def test_check_goal_postconditions_do_not_bypass_expect_text():
    # 后置条件成立，但还要求 expect_text：不能仅凭后置条件判成功
    c = Verifier(None).check_goal(_obs([_box(True)], text="nothing"), "tick", "", "Saved",
                                  postconditions=PC)
    assert c.verdict != Verdict.SUCCESS


def test_final_check_revalidates_subgoal_postconditions():
    sgs = [Subgoal(1, "tick", postconditions=PC)]
    v = Verifier(None)
    assert v.check_final(_obs([_box(True)]), "t", sgs).verdict == Verdict.SUCCESS
    c = v.check_final(_obs([_box(False)]), "t", sgs)
    assert c.verdict == Verdict.FAILED and c.signals.get("failed_subgoals") == [1]

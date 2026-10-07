"""核心纯逻辑：动作解析、坐标、记忆、恢复决策、验证规则。"""
import pytest
from PIL import Image

from gua.actions import Action, parse_action
from gua.coords import CoordMapper, smart_resize, to_pixel_action
from gua.env.base import ExecResult, Observation, UIElement
from gua.env.mock import MockButton, MockEnv
from gua.llm.base import Budget
from gua.memory import Memory, StepRecord
from gua.parsing import extract_json, parse_model_action, parse_point, parse_uitars
from gua.recovery import RecoveryPolicy, Strategy
from gua.verify import Check, Verdict, Verifier


def obs(elements=(), text="", window="App", img=None, url=""):
    img = img or Image.new("RGB", (800, 600), (255, 255, 255))
    return Observation(img, 0.0, img.size, 1.0, window, "p", [window], list(elements), "mock", url, text)


# ---------------------------------------------------------------- parsing
def test_extract_json_fenced():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('thought... {"type": "click", "target": "OK"} end')["type"] == "click"


def test_parse_action_aliases_and_hotkey_string():
    assert parse_action({"type": "hotkey", "keys": "ctrl+s"}).keys == ["ctrl", "s"]
    assert parse_action({"action": "left_click", "coordinate": [10, 20]}).type == "click"
    a = parse_action({"action_type": "input_text", "text": "hi", "index": 3})
    assert a.type == "type" and a.element_id == 3
    assert parse_action({"action_type": "navigate_back"}).type == "back"
    assert parse_action({"action_type": "status", "goal_status": "infeasible"}).type == "fail"
    assert parse_action({"type": "open_app", "app_name": "Settings"}).app == "Settings"
    s = parse_action({"type": "scroll", "amount": -5})
    assert s.direction == "down" and s.amount == 5
    with pytest.raises(ValueError):
        Action("teleport")


def test_parse_point_variants():
    assert parse_point('{"x": 10, "y": 20}') == (10, 20)
    assert parse_point("click(start_box='(500,300)')") == (500, 300)
    assert parse_point("<point>12 34</point>") == (12, 34)
    assert parse_point('{"bbox": [0, 0, 100, 50]}') == (50, 25)
    assert parse_point("<|box_start|>(7,8)<|box_end|>") == (7, 8)


def test_parse_uitars_actions():
    a, th = parse_uitars("Thought: 点保存\nAction: click(start_box='<|box_start|>(100,200)<|box_end|>')")
    assert a.type == "click" and (a.x, a.y) == (100, 200) and a.coord_space == "resized" and th == "点保存"
    a, _ = parse_uitars("Thought: x\nAction: type(content='hello\\n')")
    assert a.type == "type" and a.text == "hello" and a.submit
    a, _ = parse_uitars("Action: hotkey(key='ctrl c')")
    assert a.keys == ["ctrl", "c"]
    a, _ = parse_uitars("Action: drag(start_box='(1,2)', end_box='(3,4)')")
    assert (a.x, a.y, a.x2, a.y2) == (1, 2, 3, 4)
    a, _ = parse_uitars("Action: scroll(point='<point>5 6</point>', direction='up')")
    assert a.direction == "up" and (a.x, a.y) == (5, 6)
    assert parse_uitars("Action: press_back()")[0].type == "back"
    assert parse_uitars("Action: finished(content='ok')")[0].text == "ok"
    # parse_model_action：JSON 优先，失败退回 UI-TARS
    assert parse_model_action('{"thought":"t","action":{"type":"wait","seconds":1}}')[0].type == "wait"
    assert parse_model_action("Action: click(start_box='(1,1)')")[0].type == "click"


# ---------------------------------------------------------------- coords
def test_coord_mapper_roundtrip():
    m = CoordMapper("norm1000")
    assert m.to_image(500, 500, 1920, 1080) == (960, 540)
    rh, rw = smart_resize(1080, 1920, max_pixels=1003520)
    m2 = CoordMapper("resized", max_pixels=1003520)
    assert m2.to_image(rw / 2, rh / 2, 1920, 1080) == (960, 540)
    for conv in ("norm1000", "norm1", "resized", "pixel"):
        mm = CoordMapper(conv, 1003520)
        x, y = mm.from_image(300, 200, 1280, 800)
        assert abs(mm.to_image(x, y, 1280, 800)[0] - 300) <= 1


def test_to_pixel_action():
    a = to_pixel_action(Action("drag", x=100, y=500, x2=900, y2=1000, coord_space="norm1000"), 1280, 800)
    assert (a.x, a.y, a.x2, a.y2, a.coord_space) == (128, 400, 1152, 800, "pixel")


# ---------------------------------------------------------------- memory / budget
def test_loop_detection_and_notes():
    m = Memory()
    for i in range(3):
        m.add_step(StepRecord(i, 1, '{"type":"click"}', "no_effect", ""))
    assert m.is_looping()
    for i in range(8):
        m.add_note(f"lesson {i}")
    assert len(m.notes) == m.max_notes and "lesson 7" in m.notes_text()


def test_budget_exhausted():
    b = Budget(max_calls=2)
    b.add("x")
    assert b.exhausted() is None
    b.add("x")
    assert "exhausted" in b.exhausted()
    assert Budget(max_tokens=10, prompt_tokens=11).exhausted()


# ---------------------------------------------------------------- recovery
def test_recovery_wait_then_budget():
    p = RecoveryPolicy(max_waits=2, max_recoveries_per_subgoal=1)
    a = Action("click", x=1, y=1)
    assert p.decide(Check(Verdict.IN_PROGRESS), a).strategy == Strategy.WAIT
    assert p.decide(Check(Verdict.IN_PROGRESS), a).strategy == Strategy.WAIT
    assert p.decide(Check(Verdict.NO_EFFECT), a).strategy == Strategy.REGROUND_ZOOM
    assert p.decide(Check(Verdict.NO_EFFECT), a).strategy in {Strategy.REPLAN, Strategy.GIVE_UP}


def test_focus_lost_triggers_platform_refocus():
    c = Check(Verdict.FAILED, signals={"focus_lost": True})
    plan = RecoveryPolicy().decide(c, Action("click", x=1, y=1), task_window="Mock")
    assert plan.strategy == Strategy.REFOCUS and plan.actions[0].type == "focus_window"
    plan = RecoveryPolicy(platform="android").decide(c, Action("click", x=1, y=1), task_window="com.app")
    assert plan.actions[0].type == "open_app" and plan.actions[0].app == "com.app"


def test_scroll_toward_offscreen_target():
    p = RecoveryPolicy(scroll_unit_px=100)
    c = Check(Verdict.FAILED, signals={"exec_error": "out_of_bounds (640,2400)", "point": [640, 2400],
                                       "screen": [1280, 800]})
    plan = p.decide(c, Action("click", x=640, y=2400))
    assert plan.strategy == Strategy.SCROLL_INTO_VIEW
    s = plan.actions[0]
    assert s.direction == "down" and s.amount == 20


def test_dismiss_is_platform_specific():
    c = Check(Verdict.FAILED)
    assert RecoveryPolicy().decide(c, Action("click", x=1, y=1)).actions[0].keys == ["esc"]
    assert RecoveryPolicy(platform="android").decide(c, Action("click", x=1, y=1)).actions[0].type == "back"
    p = RecoveryPolicy(platform="macos", allow_undo=True)
    assert p.decide(c, Action("type", text="x")).actions[0].keys == ["cmd", "z"]


def test_fixed_retry_and_disabled():
    assert RecoveryPolicy(enabled=False).decide(Check(Verdict.FAILED), Action("wait")).strategy == Strategy.REPLAN
    a = Action("click", x=3, y=3)
    plan = RecoveryPolicy(fixed_retry=True).decide(Check(Verdict.NO_EFFECT), a)
    assert plan.actions == [a]


# ---------------------------------------------------------------- verifier rules
def test_rule_check_no_effect_and_exec_error():
    env = MockEnv(buttons=[MockButton("Save", (100, 100, 200, 140))])
    v = Verifier(None)
    o1 = env.observe()
    a = Action("click", x=600, y=600)
    env.execute(a)
    o2 = env.observe()
    assert v.rule_check(o1, o2, a, ExecResult(True), True).verdict == Verdict.NO_EFFECT
    c = v.rule_check(o1, o2, a, ExecResult(False, "out_of_bounds"), True)
    assert c.verdict == Verdict.FAILED and c.level == "L0" and c.signals["point"] == [600, 600]


def test_rule_new_dialog_is_blocked():
    v = Verifier(None)
    after = obs([UIElement(0, "Terms update", "dialog", (100, 100, 500, 400))])
    c = v.rule_check(obs(), after, Action("click", x=10, y=10), ExecResult(True), True)
    assert c.verdict == Verdict.BLOCKED and c.signals["new_dialog"] == "Terms update"


def test_rule_typed_text_and_toggle_success():
    v = Verifier(None)
    after = obs([UIElement(0, "Name", "textbox", (0, 0, 100, 30), value="Alice")])
    c = v.rule_check(obs(), after, Action("type", text="Alice"), ExecResult(True), True)
    assert c.verdict == Verdict.SUCCESS and "typed" in c.evidence
    b = obs([UIElement(0, "Bold", "checkbox", (0, 0, 20, 20), checked=False)])
    a2 = obs([UIElement(0, "Bold", "checkbox", (0, 0, 20, 20), checked=True)])
    assert v.rule_check(b, a2, Action("click", x=10, y=10), ExecResult(True), True).verdict == Verdict.SUCCESS


def test_rule_busy_and_unstable_mean_wait():
    v = Verifier(None)
    c = v.rule_check(obs(text="Ready"), obs(text="Loading..."), Action("click", x=1, y=1), ExecResult(True), True)
    assert c.verdict == Verdict.IN_PROGRESS
    c = v.rule_check(obs(text="加载中 old"), obs(text="加载中 old"), Action("click", x=1, y=1), ExecResult(True), False)
    assert c.verdict == Verdict.IN_PROGRESS and "changing" in c.evidence


def test_rule_focus_lost():
    v = Verifier(None)
    c = v.rule_check(obs(window="Editor"), obs(window="Distractor"), Action("click", x=1, y=1), ExecResult(True),
                     True, task_window="Editor")
    assert c.signals["focus_lost"] and c.verdict == Verdict.BLOCKED


def test_goal_check_rule_first():
    v = Verifier(None)
    assert v.check_goal(obs(text="Export started"), "g", "e", "export started").verdict == Verdict.SUCCESS
    assert v.check_goal(obs(text="nothing"), "g", "e", "Export started").verdict == Verdict.FAILED
    assert Verifier(None, trigger="none").check_goal(obs(), "g", "e").verdict == Verdict.SUCCESS


def test_on_event_only_calls_model_when_uncertain():
    from gua.llm.base import ScriptedLLM
    llm = ScriptedLLM(fn=lambda s, t, i: '{"verdict":"success","evidence":"ok"}')
    v = Verifier(llm, trigger="on_event")
    white = Image.new("RGB", (800, 600), (255, 255, 255))
    black = Image.new("RGB", (800, 600), (0, 0, 0))
    c = v.check_step(obs(img=white), obs(img=black), Action("click", x=5, y=5), ExecResult(True), True, "x")
    assert c.verdict == Verdict.SUCCESS and c.level == "L2" and llm.budget.calls == 1
    v.check_step(obs(img=white), obs(img=white), Action("click", x=5, y=5), ExecResult(True), True, "x")
    assert llm.budget.calls == 1   # NO_EFFECT 由规则定论，不调用模型
    Verifier(llm, trigger="every_step").check_step(obs(img=white), obs(img=white), Action("click", x=5, y=5),
                                                   ExecResult(True), True, "x")
    assert llm.budget.calls == 2

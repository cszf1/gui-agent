"""v0.3 代码审查回归测试：每个审查条目 iNN 都先在 v0.2 上复现失败，再修复（见 docs/review-fixes.md）。

新 API 在测试函数内部导入，这样在 v0.2 上每条测试单独失败，而不是整个文件导入失败。
"""
import base64
import io
import json
import random
import shlex
import sys

import pytest
from PIL import Image

from conftest import fast
from fakes import fake_pyautogui, import_platform_module
from gua.actions import Action
from gua.agent import AgentConfig, GUIAgent
from gua.coords import CoordMapper, smart_resize
from gua.env.base import ExecResult, Observation, UIElement
from gua.env.mock import MockButton, MockEnv
from gua.grounding import Grounder
from gua.llm.base import Budget, ScriptedLLM
from gua.planner import Actor, Planner, Subgoal
from gua.recovery import RecoveryPlan, RecoveryPolicy, Strategy
from gua.safety import SafetyGuard
from gua.verify import Verifier

DONE = '{"action":{"type":"done"}}'


def _agent(env, plan, actor_fn, verifier_fn=None, recovery=None, guard=None, cfg=None, grounder_llm=None,
           budget=None, planner_fn=None):
    budget = budget or Budget()
    planner = ScriptedLLM(fn=planner_fn or (lambda s, t, i: plan), budget=budget, role="planner")
    actor = ScriptedLLM(fn=actor_fn, budget=budget, role="actor")
    ver = ScriptedLLM(fn=verifier_fn, budget=budget, role="verifier") if verifier_fn else None
    agent = GUIAgent(env, Planner(planner, "mock"), Actor(actor, "mock"),
                     Grounder(grounder_llm, CoordMapper("pixel")), Verifier(ver),
                     recovery or RecoveryPolicy(platform="mock"),
                     cfg or AgentConfig(max_steps=12, task_window="Mock", settle_timeout=0.2), budget,
                     guard=guard or SafetyGuard(mode="deny"))
    agent._test_llms = (planner, actor, ver)
    return agent


def _obs(elements=(), text="", window="App", size=(800, 600)):
    return Observation(Image.new("RGB", size, (255, 255, 255)), 0.0, size, 1.0, window, "p", [window],
                       list(elements), "mock", "", text)


# =====================================================================================  1 safety × recovery
def test_i01_fixed_retry_never_executes_denied_action():
    env = fast(MockEnv(title="Mock Editor", buttons=[
        MockButton("Delete account", (100, 100, 300, 150), on_click=lambda e: e.state.update(deleted=True))]))
    asked = []
    guard = SafetyGuard(mode="confirm", confirm_fn=lambda a, why: asked.append(why) or False)
    agent = _agent(env, '{"subgoals":[{"goal":"delete","expect_text":"deleted=True"}]}',
                   lambda s, t, i: '{"action":{"type":"click","target":"Delete account"}}',
                   recovery=RecoveryPolicy(fixed_retry=True, platform="mock"), guard=guard,
                   cfg=AgentConfig(max_steps=6, max_steps_per_subgoal=3, max_replans=0, settle_timeout=0.2,
                                   task_window="Mock"))
    res = agent.run("delete my account")
    assert not env.state.get("deleted"), "a human-rejected action was executed by the fixed-retry baseline"
    assert len(asked) == 1, "a rejection must be terminal for that action (never re-asked / retried)"
    assert res.status != "done" and not res.claimed_done


def test_i01_recovery_actions_pass_safety_gate():
    class EvilRecovery(RecoveryPolicy):
        def decide(self, check, action, task_window="", last_failure=None):
            self._used += 1
            return RecoveryPlan(Strategy.DISMISS, [Action("hotkey", keys=["alt", "f4"])], "close it")

    env = fast(MockEnv(title="Mock Editor", buttons=[MockButton("Save", (100, 100, 220, 150))]))
    agent = _agent(env, '{"subgoals":[{"goal":"save","expect_text":"never"}]}',
                   lambda s, t, i: '{"action":{"type":"click","target":"Save"}}',
                   recovery=EvilRecovery(platform="mock"),
                   cfg=AgentConfig(max_steps=3, max_steps_per_subgoal=3, max_replans=0, settle_timeout=0.2,
                                   task_window="Mock"))
    agent.run("save")
    assert not any("alt+f4" in entry for entry in env.log), env.log
    assert any(e["decision"] == "confirm" and not e["approved"] for e in agent.guard.log)


# =====================================================================================  2 final verification
def _toggle_env():
    def a_on(e):
        e.state["A"] = "on"

    def b_on(e):  # 打开 B 会把 A 恢复成 off（“A recovered to off”）
        e.state["B"] = "on"
        e.state["A"] = "off"
    env = fast(MockEnv(title="Mock Settings", buttons=[MockButton("Toggle A", (100, 100, 220, 150), on_click=a_on),
                                                       MockButton("Toggle B", (300, 100, 420, 150), on_click=b_on)]))
    env.state.update(A="off", B="off")
    return env


def test_i02_final_check_rechecks_every_subgoal():
    env = _toggle_env()
    plan = json.dumps({"subgoals": [{"goal": "turn A on", "expect_text": "A=on"},
                                    {"goal": "turn B on", "expect_text": "B=on"}]})

    def actor(s, t, i):
        if "turn A on" in t.split("Current sub-goal", 1)[1].split("\n")[0]:
            return DONE if env.state["A"] == "on" else '{"action":{"type":"click","target":"Toggle A"}}'
        return DONE if env.state["B"] == "on" else '{"action":{"type":"click","target":"Toggle B"}}'
    agent = _agent(env, plan, actor, cfg=AgentConfig(max_steps=10, max_replans=0, settle_timeout=0.2,
                                                    task_window="Mock"))
    res = agent.run("turn on both A and B")
    assert env.state == {"A": "off", "B": "on"}
    assert res.status != "done" and not res.claimed_done, res


def _two_step_uncertain_agent(final_reply, nsub=2):
    env = fast(MockEnv(title="Mock Editor", buttons=[
        MockButton("Save", (100, 100, 220, 150), on_click=lambda e: e.state.update(saved=True))]))
    goals = ["click Save", "check it is saved"][:nsub]
    plan = json.dumps({"subgoals": [{"goal": g} for g in goals]})
    seen = {}

    def actor(s, t, i):
        g = t.split("Current sub-goal", 1)[1].split("\n")[0]
        seen[g] = seen.get(g, 0) + 1
        return DONE if seen[g] > 1 else '{"action":{"type":"click","target":"Save"}}'

    def verifier(s, t, i):
        if "FINALTASK" in t:
            return final_reply
        return '{"verdict":"success","evidence":"looks fine"}'
    plans = []

    def planner(s, t, i):
        plans.append(t)
        return plan
    agent = _agent(env, plan, actor, verifier, planner_fn=planner,
                   cfg=AgentConfig(max_steps=20, settle_timeout=0.2, task_window="Mock"))
    return agent, plans


def test_i02_uncertain_or_unparseable_final_is_not_done():
    agent, plans = _two_step_uncertain_agent("I am not sure, maybe?")      # 不可解析
    res = agent.run("save the doc FINALTASK")
    assert res.status == "uncertain" and not res.claimed_done, res
    assert len(plans) == 2          # 先重规划一次，再报告 uncertain
    agent, _ = _two_step_uncertain_agent('{"verdict":"uncertain","evidence":"cannot tell"}')
    res = agent.run("save the doc FINALTASK")
    assert res.status == "uncertain" and not res.claimed_done


def test_i02_final_check_runs_for_single_subgoal_and_sees_whole_task():
    agent, _ = _two_step_uncertain_agent('{"verdict":"failed","evidence":"not saved"}', nsub=1)
    agent.cfg.max_replans = 0
    res = agent.run("save the doc FINALTASK")
    assert res.status == "fail" and not res.claimed_done, res
    final_prompts = [p for p in agent._test_llms[2].prompts if "FINALTASK" in p]
    assert final_prompts and "click Save" in final_prompts[-1]   # 聚合：整个任务 + 所有子目标的预期


def test_i02_final_check_can_be_disabled_explicitly():
    agent, _ = _two_step_uncertain_agent('{"verdict":"failed","evidence":"x"}', nsub=1)
    agent.cfg.final_check = False
    assert agent.run("save the doc FINALTASK").status == "done"


# =====================================================================================  3 budget hard cap
def test_i03_budget_raises_before_call():
    from gua.llm.base import BudgetExceeded
    b = Budget(max_calls=2)
    llm = ScriptedLLM(fn=lambda s, t, i: "ok", budget=b)
    llm.chat("s", "t")
    llm.chat("s", "t")
    with pytest.raises(BudgetExceeded):
        llm.chat("s", "t")
    assert b.calls == 2 and len(llm.prompts) == 2


def test_i03_token_and_cost_caps():
    from gua.llm.base import BudgetExceeded
    b = Budget(max_tokens=10)
    b.add("x", 8, 3)
    with pytest.raises(BudgetExceeded):
        b.before_call("x")
    c = Budget(max_cost_usd=0.01)
    c.add("x", 1000, 1000, cost_usd=0.02)
    with pytest.raises(BudgetExceeded):
        c.before_call("x")


def test_i03_agent_budget_is_terminal_and_never_exceeded():
    env = fast(MockEnv(title="Mock Editor", buttons=[
        MockButton("Save", (100, 100, 220, 150), on_click=lambda e: e.state.update(saved=True))]))
    agent = _agent(env, '{"subgoals":[{"goal":"click Save"}]}',
                   lambda s, t, i: '{"action":{"type":"click","target":"Save"}}',
                   lambda s, t, i: '{"verdict":"success","evidence":"ok"}',
                   cfg=AgentConfig(max_steps=10, max_budget_calls=2, settle_timeout=0.2, task_window="Mock"))
    res = agent.run("save")
    assert res.budget.calls <= 2, res.budget
    assert res.status == "budget_exhausted" and not res.claimed_done


def test_i03_eval_records_budget_exhausted(tmp_path):
    from pathlib import Path
    from gua.config import load_config
    from gua.eval.runner import run_task, summarize
    cfg = load_config(Path(__file__).resolve().parent.parent / "configs" / "mock.yaml",
                      {"agent": {"max_budget_calls": 1, "settle_timeout": 0.1, "settle_interval": 0.02}})
    task = {"id": "t", "instruction": "wait", "platform": "mock", "checks": [],
            "demo": {"subgoals": [{"goal": "g", "steps": [{"wait": 0}]}]}}
    row = run_task(cfg, task, MockEnv(), str(tmp_path), policy="scripted")
    assert row["status"] == "budget_exhausted" and row["budget_exhausted"] is True and row["calls"] <= 1
    s = summarize([row])
    assert s["budget_exhausted_runs"] == 1


# =====================================================================================  4 coordinate transform chain
class _FakeOpenAI:
    """假的 OpenAI 客户端：像真实 VLM 一样只“看到”实际发送的图像，再按坐标约定回答目标点。"""

    def __init__(self, target_xy_orig, orig_size, convention, max_pixels):
        self.t, self.orig, self.conv, self.maxp = target_xy_orig, orig_size, convention, max_pixels
        self.sent = []
        self.chat = self
        self.completions = self

    def create(self, **kw):
        url = kw["messages"][1]["content"][1]["image_url"]["url"]
        img = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
        sw, sh = img.size
        self.sent.append((sw, sh))
        fx, fy = self.t[0] / self.orig[0], self.t[1] / self.orig[1]
        if self.conv == "resized":
            rh, rw = smart_resize(sh, sw, max_pixels=self.maxp)
            x, y = fx * rw, fy * rh
        elif self.conv == "pixel":
            x, y = fx * sw, fy * sh
        else:
            x, y = fx * 1000, fy * 1000
        msg = type("M", (), {"content": json.dumps({"x": x, "y": y})})
        return type("R", (), {"choices": [type("C", (), {"message": msg})], "usage": None})


def _vlm(target, orig, conv, maxp, max_side):
    from gua.llm.openai_compat import OpenAICompatLLM
    llm = OpenAICompatLLM(model="ui-tars-1.5-7b", image_max_side=max_side)
    llm.client = _FakeOpenAI(target, orig, conv, maxp)
    return llm


@pytest.mark.parametrize("conv", ["resized", "pixel", "norm1000"])
def test_i04_grounder_uses_actually_sent_image_size(conv):
    maxp = 16384 * 28 * 28          # UI-TARS 默认 max_pixels：不会再被 max_pixels 二次压缩
    llm = _vlm((960, 300), (1920, 1080), conv, maxp, 1280)
    g = Grounder(llm, CoordMapper(conv, maxp))
    x, y = g.ground_vlm(Image.new("RGB", (1920, 1080)), "Save")
    assert llm.client.sent == [(1280, 720)]
    assert abs(x - 960) <= 2 and abs(y - 300) <= 2, (conv, x, y)


def test_i04_actor_coordinates_use_sent_image_size():
    llm = _vlm((960, 300), (1920, 1080), "pixel", 0, 1280)
    llm.client.create_orig = llm.client.create

    def create(**kw):  # actor 回复 JSON 动作
        r = llm.client.create_orig(**kw)
        p = json.loads(r.choices[0].message.content)
        r.choices[0].message.content = json.dumps({"action": {"type": "click", "x": p["x"], "y": p["y"]}})
        return r
    llm.client.create = create
    env = MockEnv(size=(1920, 1080))
    agent = _agent(env, "{}", lambda s, t, i: DONE)
    agent.actor = Actor(llm, "mock", coord_space="pixel")
    o = Observation(Image.new("RGB", (1920, 1080)), 0, (1920, 1080))
    a, _ = agent.actor.next_action("t", Subgoal(1, "g"), 1, o, "", "")
    a, _src = agent._resolve(a, o)
    assert abs(a.x - 960) <= 2 and abs(a.y - 300) <= 2, (a.x, a.y)


def test_i04_transform_roundtrip_property():
    from gua.coords import ImageTransform
    rnd = random.Random(1234)
    for _ in range(400):
        ow, oh = rnd.randint(320, 5120), rnd.randint(240, 2880)
        side = rnd.choice([None, 768, 1024, 1280, 1568, 1600])
        sent = ImageTransform.sent_size_for((ow, oh), side)
        conv = rnd.choice(["norm1000", "norm1", "resized", "pixel"])
        dpi = rnd.choice([1.0, 1.25, 1.5, 2.0])
        tf = ImageTransform((ow, oh), sent, conv, max_pixels=rnd.choice([1003520, 12845056]), dpi_scale=dpi)
        x, y = rnd.uniform(0, ow - 1), rnd.uniform(0, oh - 1)
        mx, my = tf.screenshot_to_model(x, y)
        bx, by = tf.model_to_screenshot(mx, my)
        assert abs(bx - x) <= 1.0 and abs(by - y) <= 1.0, (tf, x, y, bx, by)
        ix, iy = tf.screenshot_to_input(bx, by)            # Retina / DPI：截图像素 → 输入坐标
        assert abs(ix * dpi - bx) <= dpi and abs(iy * dpi - by) <= dpi


def test_i04_claude_scaling_through_transform():
    from gua.coords import ImageTransform
    from gua.llm.anthropic import scaling_target
    tw, th = scaling_target(2880, 1800)
    tf = ImageTransform((2880, 1800), (tw, th), "pixel", dpi_scale=2.0)
    assert (tw, th) == (1280, 800)
    assert tf.model_to_screenshot(640, 400) == (1440, 900)
    assert tf.screenshot_to_input(1440, 900) == (720, 450)


# =====================================================================================  5 malformed actions
@pytest.mark.parametrize("reply", [
    '{"action": null}',
    '{"action": {"type": "click", "x": 10}}',
    '{"action": {"type": "teleport"}}',
    '{"action": {"type": "scroll", "direction": "sideways"}}',
    '{"action": {"type": "click", "x": 5000, "y": 10, "coord_space": "norm1000"}}',
    '{"action": {"type": "hotkey", "keys": 5}}',
    '{"action": {"type": "wait", "seconds": "abc"}}',
    '{"action": {"type": "type"}}',
    '{"action": {"type": "click", "x": "left", "y": 3}}',
    '{"action": [1, 2]}',
    '[]',
])
def test_i05_malformed_actions_raise_structured_parse_error(reply):
    from gua.parsing import ActionParseError, parse_model_action
    with pytest.raises(ActionParseError) as ei:
        parse_model_action(reply)
    assert ei.value.code and ei.value.feedback()


def test_i05_agent_survives_malformed_actions():
    env = fast(MockEnv(title="Mock Editor", buttons=[
        MockButton("Save", (100, 100, 220, 150), on_click=lambda e: e.state.update(saved=True))]))
    bad = iter(['{"action": null}', '{"action": {"type": "click", "x": 10}}', '{"action":{"type":"teleport"}}'])
    prompts = []

    def actor(s, t, i):
        prompts.append(t)
        if env.state.get("saved"):
            return DONE
        return next(bad, '{"action":{"type":"click","target":"Save"}}')
    agent = _agent(env, '{"subgoals":[{"goal":"save","expect_text":"saved=True"}]}', actor)
    res = agent.run("save")
    assert res.status == "done"
    assert any("missing_field" in p or "invalid" in p for p in prompts)


def test_i05_fuzz_parse_never_crashes():
    from gua.parsing import ActionParseError, parse_model_action
    rnd = random.Random(7)
    vals = [None, True, 0, -1, 3.5, 1e9, float("nan"), "", "x", "left", [], [1], [1, 2], {}, {"a": 1}]
    keys = ["type", "action", "x", "y", "x2", "y2", "keys", "text", "direction", "amount", "seconds",
            "element_id", "coord_space", "url", "app", "target", "coordinate", "end_point", "index", "clear"]
    types_ = ["click", "drag", "scroll", "type", "hotkey", "wait", "navigate", "open_app", "done", "fail",
              "ask_user", "key_down", "teleport", None, 3]
    for _ in range(600):
        d = {k: rnd.choice(vals) for k in rnd.sample(keys, rnd.randint(0, 6))}
        d["type"] = rnd.choice(types_)
        try:
            s = json.dumps({"thought": "t", "action": d})
        except ValueError:
            continue
        try:
            a, _ = parse_model_action(s)
        except ActionParseError:
            continue
        assert isinstance(a, Action)


# =====================================================================================  6 capability policy / ablations
def _cfg(name, **over):
    from pathlib import Path
    from gua.config import load_config
    base = {"env": {"platform": "mock"}, "safety": {"mode": "deny"},
            "agent": {"settle_timeout": 0.2, "settle_interval": 0.02, "max_steps": 10}}
    for k, v in over.items():
        base[k] = v
    return load_config(Path(__file__).resolve().parent.parent / "configs" / name, base)


def test_i06_vision_only_prompts_contain_no_a11y_text():
    from gua.config import build_agent
    env = fast(MockEnv(title="Mock Editor", buttons=[
        MockButton("Zebra Unicorn Save", (100, 100, 220, 150), on_click=lambda e: e.state.update(secretflag=1))]))
    n = {"a": 0}

    def actor(s, t, i):
        n["a"] += 1
        return DONE if n["a"] > 1 else '{"action":{"type":"click","target":"the save button"}}'
    llms = {"planner": ScriptedLLM(fn=lambda s, t, i: '{"subgoals":[{"goal":"save it"}]}'),
            "actor": ScriptedLLM(fn=actor),
            "grounder": ScriptedLLM(fn=lambda s, t, i: '{"x": 160, "y": 125}'),
            "verifier": ScriptedLLM(fn=lambda s, t, i: '{"verdict":"success","evidence":"ok"}')}
    cfg = _cfg("ablations/vision_only.yaml", grounding={"coord": "pixel"})
    agent = build_agent(cfg, env, llms=llms)
    res = agent.run("save the document")
    assert agent.policy.a11y_in_prompts is False and agent.policy.a11y_grounding is False
    prompts = [p for l in llms.values() for p in l.prompts]
    assert prompts
    for p in prompts:
        assert "Zebra" not in p and "secretflag" not in p and "[0]" not in p, p
    assert env.state.get("secretflag") == 1 and res.status == "done"


def test_i06_rules_only_never_calls_llm_verifier():
    from gua.config import build_agent
    env = fast(MockEnv(title="Mock Editor", buttons=[
        MockButton("Save", (100, 100, 220, 150), on_click=lambda e: e.state.update(saved=True))]))
    n = {"a": 0}

    def actor(s, t, i):
        n["a"] += 1
        return DONE if n["a"] > 2 else '{"action":{"type":"click","target":"Save"}}'
    ver = ScriptedLLM(fn=lambda s, t, i: '{"verdict":"success","evidence":"ok"}')
    llms = {"planner": ScriptedLLM(fn=lambda s, t, i: '{"subgoals":[{"goal":"save"}]}'),
            "actor": ScriptedLLM(fn=actor), "verifier": ver, "grounder": None}
    agent = build_agent(_cfg("ablations/fixed_retry.yaml"), env, llms=llms)
    agent.run("save")
    assert ver.prompts == [], "rules-only baseline called an LLM verifier/reflector"
    assert agent.policy.llm_step_verify is False and agent.policy.llm_reflection is False


def test_i06_every_ablation_maps_to_documented_policy():
    from gua.policy import CapabilityPolicy
    expect = {
        "default.yaml": dict(a11y_grounding=True, a11y_in_prompts=True, a11y_rules=True, llm_step_verify=True,
                             llm_goal_verify=True, llm_reflection=True, recovery="classified", final_check=True),
        "ablations/raw_loop.yaml": dict(llm_step_verify=False, llm_goal_verify=False, llm_reflection=False,
                                        recovery="none", final_check=False, goal_check=False),
        "ablations/fixed_retry.yaml": dict(llm_step_verify=False, llm_goal_verify=False, llm_reflection=False,
                                           recovery="fixed_retry", a11y_rules=True),
        "ablations/every_step_verify.yaml": dict(step_trigger="every_step", llm_step_verify=True),
        "ablations/no_goal_check.yaml": dict(goal_check=False, final_check=False),
        "ablations/vision_only.yaml": dict(a11y_grounding=False, a11y_in_prompts=False, a11y_rules=False),
        "ablations/no_reflection.yaml": dict(llm_reflection=False, llm_step_verify=True),
    }
    for f, want in expect.items():
        p = CapabilityPolicy.from_config(_cfg(f))
        for k, v in want.items():
            assert getattr(p, k) == v, (f, k, getattr(p, k))


# =====================================================================================  7 command injection
EVIL = ["x; reboot", "x && rm -rf /", "$(reboot)", "`reboot`", "x\nreboot", "x | sh", "a'b\"c", "-e evil"]
_OPS = {";", "&&", "||", "|", "&", ">", "<", ">>", "(", ")", "\n"}


def _tokens(cmd):
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.whitespace = " \t"          # 换行在 sh 里是命令分隔符，必须当作运算符而不是空白
    return list(lex)


def _android():
    from gua.env.android import AndroidEnv
    calls = []

    def runner(args, binary=False):
        calls.append(args)
        if args[:2] == ["shell", "wm size"]:
            return "Physical size: 1080x2400"
        return ""
    return AndroidEnv(runner=runner), calls


@pytest.mark.parametrize("evil", EVIL)
def test_i07_android_type_text_is_one_argv(evil):
    env, calls = _android()
    env.execute(Action("type", text=evil, submit=True))
    sent = [c[1] for c in calls if c[0] == "shell" and c[1] != "wm size"]
    assert sent
    for cmd in sent:
        toks = _tokens(cmd)
        assert not (set(toks) & _OPS), (evil, cmd, toks)
    text_cmd = next(c for c in sent if c.startswith("input text"))
    assert shlex.split(text_cmd)[:2] == ["input", "text"] and len(shlex.split(text_cmd)) == 3


@pytest.mark.parametrize("evil", EVIL)
def test_i07_android_open_app_and_keys_validated(evil):
    env, calls = _android()
    r = env.execute(Action("open_app", app=evil))
    assert not r.ok and "invalid" in r.error
    r = env.execute(Action("hotkey", keys=[evil]))
    assert not r.ok
    assert all(c[0] != "shell" or c[1] == "wm size" for c in calls), calls


def test_i07_macos_activate_passes_app_via_argv(monkeypatch):
    mac = import_platform_module(monkeypatch, "macos", "darwin")
    ran = []
    monkeypatch.setattr(mac.subprocess, "run", lambda args, **k: ran.append(args) or type("R", (), {"returncode": 0})())
    env = mac.MacOSEnv.__new__(mac.MacOSEnv)
    evil = 'Evil" to quit\ndo shell script "rm -rf ~'
    env.focus_window(evil)
    script_args = ran[-1]
    assert script_args[0] == "osascript"
    assert all("rm -rf" not in a for a in script_args[:-1]) and script_args[-1] == evil.split(" - ")[0]
    ran.clear()
    env.points, env.scale = (1440, 900), 2.0
    monkeypatch.setitem(sys.modules, "pyautogui", fake_pyautogui())
    from gua.env.desktop import PyAutoGUIInput
    env.input = PyAutoGUIInput("macos", 2.0)
    assert not env.execute(Action("open_app", app="-e evil")).ok and not ran


def test_i07_windows_open_app_no_cmd_shell(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyautogui", fake_pyautogui())
    win = import_platform_module(monkeypatch, "windows", "win32")
    popen = []
    monkeypatch.setattr(win.subprocess, "Popen", lambda args, **k: popen.append((args, k)))
    started = []
    monkeypatch.setattr(win, "_startfile", lambda p: started.append(p), raising=False)
    env = win.WindowsEnv.__new__(win.WindowsEnv)
    env._mon = {"left": 0, "top": 0, "width": 1920, "height": 1080}
    from gua.env.desktop import PyAutoGUIInput
    env.input = PyAutoGUIInput("windows", 1.0)
    for evil in ["notepad & calc", "notepad | evil", "x\" & calc", "%COMSPEC%", "notepad^&calc"]:
        r = env.execute(Action("open_app", app=evil))
        assert not r.ok and "invalid" in r.error, (evil, r)
    assert not popen and not started
    env.execute(Action("open_app", app="notepad"))
    for args, kw in popen:
        assert "cmd" not in [str(a).lower() for a in args] and not kw.get("shell")


def test_i07_linux_open_app_validated(monkeypatch):
    from gua.env import linux
    popen = []
    monkeypatch.setattr(linux.subprocess, "Popen", lambda args, **k: popen.append(args))
    monkeypatch.setitem(sys.modules, "pyautogui", fake_pyautogui())
    env = linux.LinuxEnv.__new__(linux.LinuxEnv)
    env._mon = {"left": 0, "top": 0, "width": 1920, "height": 1080}
    from gua.env.desktop import PyAutoGUIInput
    env.input = PyAutoGUIInput("linux", 1.0)
    assert not env.execute(Action("open_app", app="gedit; rm -rf ~")).ok and not popen


# =====================================================================================  8 hotkey aliases
@pytest.mark.parametrize("keys", [["shift", "del"], ["Shift", "Delete"], ["control", "alt", "del"],
                                  ["ctrl", "shift", "alt", "delete"], ["super", "q"], ["meta", "q"],
                                  ["command", "q"], ["Alt", "F4"], ["cmd", "option", "escape"], ["shift+del"]])
def test_i08_dangerous_hotkey_aliases(keys):
    assert SafetyGuard().assess(Action("hotkey", keys=keys)).verdict == "confirm", keys


def test_i08_key_down_sequences_are_checked():
    g = SafetyGuard(mode="deny")
    assert g.gate(Action("key_down", keys=["shift"]))[0] is True
    assert g.gate(Action("hotkey", keys=["del"]))[0] is False          # shift 仍按着
    g2 = SafetyGuard(mode="deny")
    assert g2.gate(Action("key_down", keys=["alt"]))[0] is True
    assert g2.gate(Action("key_down", keys=["f4"]))[0] is False
    assert g2.gate(Action("key_up", keys=["alt"]))[0] is True
    assert g2.gate(Action("hotkey", keys=["f4"]))[0] is True           # alt 已松开


def test_i08_control_sequences_in_typed_text():
    g = SafetyGuard()
    for t in ["abc\x7f", "\x1b[3~", "x\x04", "a\x08\x08"]:
        assert g.assess(Action("type", text=t)).verdict == "confirm", repr(t)
    assert g.assess(Action("type", text="hello\nworld\t!")).verdict == "allow"


def test_i08_canonical_key_names_and_android_delete():
    from gua.keys import canonical_key
    assert canonical_key("Del") == canonical_key("delete") == "delete"
    assert canonical_key("Control") == canonical_key("ctrl_l") == "ctrl"
    assert {canonical_key(k) for k in ["cmd", "win", "super", "meta", "command", "Super_L"]} == {"meta"}
    assert canonical_key("option") == "alt" and canonical_key("Escape") == "esc"
    env, _ = _android()
    assert env.command_for(Action("hotkey", keys=["del"])) == "input keyevent 112"
    assert env.command_for(Action("hotkey", keys=["backspace"])) == "input keyevent 67"


# =====================================================================================  10 fail-safe = user abort
def _desktop_envs(monkeypatch):
    pg = fake_pyautogui(raise_on=("click",))
    monkeypatch.setitem(sys.modules, "pyautogui", pg)
    from gua.env.desktop import PyAutoGUIInput
    from gua.env import linux
    mac = import_platform_module(monkeypatch, "macos", "darwin")
    win = import_platform_module(monkeypatch, "windows", "win32")
    lx = linux.LinuxEnv.__new__(linux.LinuxEnv)
    lx._mon = {"left": 0, "top": 0, "width": 1920, "height": 1080}
    lx.input = PyAutoGUIInput("linux", 1.0)
    mc = mac.MacOSEnv.__new__(mac.MacOSEnv)
    mc.points, mc.scale = (1440, 900), 2.0
    mc.input = PyAutoGUIInput("macos", 2.0)
    wn = win.WindowsEnv.__new__(win.WindowsEnv)
    wn._mon = {"left": 0, "top": 0, "width": 1920, "height": 1080}
    wn.input = PyAutoGUIInput("windows", 1.0)
    return {"linux": lx, "macos": mc, "windows": wn}


@pytest.mark.parametrize("plat", ["linux", "macos", "windows"])
def test_i10_failsafe_propagates_as_user_abort(monkeypatch, plat):
    from gua.errors import UserAbort
    env = _desktop_envs(monkeypatch)[plat]
    with pytest.raises(UserAbort):
        env.execute(Action("click", x=10, y=10))


def test_i10_agent_reports_user_abort():
    from gua.errors import UserAbort

    class AbortEnv(MockEnv):
        def execute(self, a):
            if a.type == "click":
                raise UserAbort("pyautogui fail-safe")
            return super().execute(a)
    env = fast(AbortEnv(title="Mock Editor", buttons=[MockButton("Save", (100, 100, 220, 150))]))
    agent = _agent(env, '{"subgoals":[{"goal":"save","expect_text":"saved"}]}',
                   lambda s, t, i: '{"action":{"type":"click","target":"Save"}}')
    res = agent.run("save")
    assert res.status == "user_abort" and not res.claimed_done


# =====================================================================================  11 is_password
def test_i11_is_password_from_every_platform():
    from gua.env import a11y
    ctrl = type("C", (), dict(ControlTypeName="EditControl", Name="PIN", AutomationId="pin", IsOffscreen=False,
                              IsEnabled=True, HasKeyboardFocus=True, IsPassword=True,
                              BoundingRectangle=type("R", (), dict(left=10, top=10, right=110, bottom=40,
                                                                   width=lambda s: 100, height=lambda s: 30))(),
                              GetValuePattern=lambda s: None))()
    raw = a11y.uia_raw(ctrl, (0, 0))
    els, _ = a11y.finalize([raw], (800, 600))
    assert els[0].is_password, "Windows UIA IsPassword dropped"
    ax = {"AXRole": "AXTextField", "AXSubrole": "AXSecureTextField", "AXTitle": "Code",
          "AXPosition": [0, 0], "AXSize": [100, 20]}
    assert a11y.ax_tree_to_elements(ax, (800, 600))[0][0].is_password
    xml = ('<hierarchy><node class="android.widget.EditText" text="" resource-id="a:id/pin" password="true" '
           'clickable="true" bounds="[0,0][100,50]"/></hierarchy>')
    assert a11y.android_xml_to_elements(xml, (800, 600))[0][0].is_password
    web = [{"tag": "input", "type": "password", "name": "PIN", "rect": [0, 0, 100, 20], "gid": 0}]
    assert a11y.finalize(a11y.web_raws(web), (800, 600))[0][0].is_password
    at = {"role": "password text", "name": "PIN", "extents": [0, 0, 100, 20], "states": ["showing", "enabled"]}
    assert a11y.atspi_tree_to_elements(at, (800, 600))[0][0].is_password


def test_i11_safety_uses_flag_not_name():
    el = UIElement(0, "PIN code", "textbox", (0, 0, 100, 30), focused=True, is_password=True)
    assert SafetyGuard().assess(Action("type", text="1234"), _obs([el])).verdict == "confirm"
    plain = UIElement(0, "PIN code", "textbox", (0, 0, 100, 30), focused=True)
    assert SafetyGuard().assess(Action("type", text="1234"), _obs([plain])).verdict == "allow"


# =====================================================================================  12 Claude drag / GPT-5 params
def _cu_resp(inp):
    return {"content": [{"type": "tool_use", "name": "computer", "input": inp}]}


def test_i12_claude_drag_starts_at_cursor():
    from gua.llm.anthropic import AnthropicLLM, ClaudeComputerUseActor
    from gua.parsing import ActionParseError
    actor = ClaudeComputerUseActor(AnthropicLLM(), "linux")
    scale = (1.5, 1.5)
    with pytest.raises(ActionParseError):           # 光标位置未知时不能猜（不再退化成零长度拖拽）
        actor.parse_response(_cu_resp({"action": "left_click_drag", "coordinate": [500, 400]}), scale)
    actor.parse_response(_cu_resp({"action": "mouse_move", "coordinate": [100, 100]}), scale)
    a, _ = actor.parse_response(_cu_resp({"action": "left_click_drag", "coordinate": [500, 400]}), scale)
    assert (a.x, a.y, a.x2, a.y2) == (150, 150, 750, 600)
    a, _ = actor.parse_response(_cu_resp({"action": "left_click_drag", "start_coordinate": [10, 20],
                                          "coordinate": [30, 40]}), scale)
    assert (a.x, a.y, a.x2, a.y2) == (15, 30, 45, 60)
    a, _ = actor.parse_response(_cu_resp({"action": "left_click_drag", "coordinate": [0, 0]}), scale)
    assert (a.x, a.y) == (45, 60)                   # 上一次拖拽的终点就是当前光标


def test_i12_gpt5_and_o_series_request_params():
    from gua.llm.openai_compat import OpenAICompatLLM
    for m in ["gpt-5", "gpt-5-mini", "o3", "o4-mini", "o1-preview"]:
        kw = OpenAICompatLLM(model=m, max_tokens=321).request_kwargs([])
        assert kw["max_completion_tokens"] == 321 and "max_tokens" not in kw and "temperature" not in kw, m
    kw = OpenAICompatLLM(model="gpt-4o", max_tokens=321).request_kwargs([])
    assert kw["max_tokens"] == 321 and kw["temperature"] == 0.0
    kw = OpenAICompatLLM(model="qwen2.5-vl-72b-instruct").request_kwargs([])
    assert "max_tokens" in kw

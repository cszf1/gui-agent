"""第三轮审查「模型出口集成」回归：严格截图阻断 + 统一模型请求清洗。

覆盖模型出口组负责的条目：
 - 配置秘密 / 观察到密码框（或明确无法确定的焦点）/ 敏感输入 → 该次运行后续不再向任何模型发送截图，
   首次 planner 请求之前就已阻断，且单调（不因下一帧没有密码元素而解除）；
 - 所有最终模型请求出口统一清洗：chat 的 system/text，以及 Anthropic post 的结构化 body（含 dict key /
   额外字段 / 嵌套 message）；覆盖 planner / actor / grounder / verifier / reflector 与直接注入 GUIAgent 的模型；
 - 普通 JSON actor 有文字元素时可无图（+明确提示）继续；纯视觉路径（UI-TARS / computer-use / 视觉
   grounding / vision-only 策略）明确以 "privacy_blocked" 终止，绝不伪装成功，也不把原图藏在别的字段传走；
 - 秘密替换与闸门次序：先把 <secret>占位符展开成真实动作，再用真实内容过 SafetyGuard.gate（危险内容、
   带换行的真实密码都按真实文本检查）；未知占位符不静默输入，返回 blocked_by_safety 并说明缺配置；
 - 门控包装后 BudgetGate 预算记账与 LLMReply.transforms 坐标变换保持正确；
 - 敏感状态后日志不再落盘截图（避免 HTML 回放泄漏）；非敏感运行日志照常保存。

本文件不依赖 Playwright / 网络；只在本机用 mock 环境运行。
"""
import json
from dataclasses import asdict
from pathlib import Path

import pytest
from PIL import Image

from gua.actions import Action
from gua.agent import AgentConfig, GUIAgent
from gua.coords import CoordMapper
from gua.env.base import Env, ExecResult, Observation, UIElement
from gua.errors import PrivacyBlocked
from gua.grounding import Grounder
from gua.llm.base import Budget, BudgetGate, EgressGate, ScriptedLLM, reply_transform
from gua.llm.anthropic import ClaudeComputerUseActor
from gua.logger import TrajectoryLogger
from gua.planner import Actor, Planner, UITarsActor
from gua.policy import CapabilityPolicy
from gua.recovery import RecoveryPolicy
from gua.reflection import Reflector
from gua.safety import SafetyGuard
from gua.sensitive import Scrubber
from gua.verify import Verifier

SECRET = "S3cr3t-Pa55!"
PLAN = '{"subgoals":[{"goal":"do the job","expect_text":"Welcome"}]}'


# ----------------------------------------------------------------- 测试替身
class Rec:
    """最底层模型替身：记录它**真正收到**的 (system, text, 图片数)。EgressGate 在它外面。"""
    def __init__(self, reply, role="llm"):
        self.reply, self.role, self.seen = reply, role, []

    def chat(self, system, text, images=None):
        self.seen.append({"system": system, "text": text, "n_images": len(images or [])})
        return self.reply(system, text, images) if callable(self.reply) else self.reply

    # ---- 断言辅助
    def blob(self) -> str:
        return "\n".join(c["system"] + "\n" + c["text"] for c in self.seen)

    def image_counts(self) -> list[int]:
        return [c["n_images"] for c in self.seen]


class RecEnv(Env):
    platform = "mock"

    def __init__(self, elements=(), text="", frames=None, err=None):
        self.elements, self.text, self.executed, self.err = list(elements), text, [], err
        self.frames = list(frames or [])          # 依次替换的元素列表（每帧一个）；用完后沿用最后一个
        self.n = 0

    def _elements(self):
        if self.frames:
            self.n += 1
            return list(self.frames[min(self.n - 1, len(self.frames) - 1)])
        return list(self.elements)

    def observe(self, with_elements=True):
        return Observation(Image.new("RGB", (200, 200)), 0.0, (200, 200), 1.0, "App", "p", ["App"],
                           self._elements() if with_elements else [], "mock", "", self.text)

    def wait_until_stable(self, timeout=0.1, interval=0.0, **kw):
        return self.observe(), True

    def execute(self, a):
        self.executed.append(a)
        if self.err:
            return ExecResult(False, self.err(a))
        return ExecResult(True)


def pw_el(name="Password"):
    return UIElement(0, name, "textbox", (10, 10, 200, 40), focused=True, is_password=True)


def button(name="OK", eid=1):
    return UIElement(eid, name, "button", (10, 60, 120, 90))


def done_reply(_s=None, _t=None, _i=None):
    return json.dumps({"thought": "done", "action": {"type": "done", "text": "ok"}})


class Harness:
    def __init__(self, env, cfg=None, guard=None, actor_reply=None, verifier_reply=None,
                 reflector_reply=None, planner_reply=PLAN, logger=None, grounder_llm=None):
        self.budget = Budget()
        self.planner = Rec(planner_reply, "planner")
        self.actor = Rec(actor_reply or done_reply, "actor")
        self.verifier = Rec(verifier_reply or '{"verdict":"uncertain","evidence":"x"}', "verifier")
        self.reflector = Rec(reflector_reply or '{"diagnosis":"d","advice":"a"}', "reflector")
        self.grounder = grounder_llm
        self.agent = GUIAgent(
            env, Planner(self.planner, "mock"), Actor(self.actor, "mock"),
            Grounder(grounder_llm, CoordMapper("pixel")), Verifier(self.verifier), RecoveryPolicy(platform="mock"),
            cfg or AgentConfig(max_steps=5, max_steps_per_subgoal=4, max_replans=0, settle_timeout=0.02,
                               settle_interval=0.01, final_check=False, verify_goals=False),
            self.budget, logger=logger, reflector=Reflector(self.reflector),
            guard=guard or SafetyGuard(mode="allow"))


# =====================================================================  1 严格截图阻断
def test_configured_secret_blocks_images_before_first_planner():
    env = RecEnv([button()])
    h = Harness(env, AgentConfig(max_steps=3, max_replans=0, settle_timeout=0.02, final_check=False,
                                 verify_goals=False, secrets={"pw": SECRET}),
                actor_reply=lambda s, t, i: json.dumps(
                    {"thought": "type", "action": {"type": "type", "text": "<secret>pw</secret>"}}))
    res = h.agent.run("do")
    assert h.planner.seen, "planner must still be called (text context is enough)"
    assert h.planner.image_counts() == [0], "the very first planner request must already carry no screenshot"
    assert all(n == 0 for n in h.actor.image_counts())
    assert env.executed and env.executed[0].text == SECRET, "executor still receives the real secret"


def test_password_observed_blocks_images_before_first_planner():
    env = RecEnv([pw_el(), button("Login")])
    h = Harness(env, actor_reply=done_reply)
    res = h.agent.run("do")
    assert h.planner.image_counts() == [0], "a password field seen at the very first observation must block before planning"
    assert all(n == 0 for n in h.actor.image_counts())
    assert h.agent.scrubber.images_blocked


def test_image_block_is_monotonic_across_frames():
    # 第一帧有密码框，之后没有：阻断不得解除
    env = RecEnv(frames=[[pw_el(), button("Login")], [button("Login")], [button("Login")], [button("Login")]])
    seen_first = {"n": 0}

    def actor(s, t, i):
        seen_first["n"] += 1
        return done_reply()
    h = Harness(env, actor_reply=actor)
    res = h.agent.run("do")
    assert all(n == 0 for n in h.planner.image_counts() + h.actor.image_counts()), \
        "once sensitive, no later frame may re-enable screenshots"
    assert h.agent.scrubber.images_blocked


def test_non_sensitive_run_still_sends_screenshots():
    env = RecEnv([button()])
    h = Harness(env)
    res = h.agent.run("do")
    assert res.status == "done"
    assert h.planner.image_counts() == [1] and h.actor.image_counts() == [1], \
        "a run with no secret still sends screenshots"


# =====================================================================  2 请求统一清洗
def test_all_role_egress_gates_share_one_scrubber():
    h = Harness(RecEnv([button()]), AgentConfig(secrets={"pw": SECRET}),
                grounder_llm=Rec(lambda s, t, i: '{"x":1,"y":1}', "grounder"))
    for role, comp in (("planner", h.agent.planner), ("actor", h.agent.actor),
                       ("grounder", h.agent.grounder), ("verifier", h.agent.verifier),
                       ("reflector", h.agent.reflector)):
        assert isinstance(comp.llm, EgressGate), f"{role}.llm is not gated"
        assert comp.llm.scrubber is h.agent.scrubber, f"{role} uses a different scrubber"


def test_every_role_chat_egress_is_scrubbed():
    h = Harness(RecEnv([button()]), AgentConfig(secrets={"pw": SECRET}),
                grounder_llm=Rec(lambda s, t, i: '{"x":1,"y":1}', "grounder"))
    img = Image.new("RGB", (10, 10))
    h.agent.planner.llm.chat("sys " + SECRET, "text " + SECRET)                       # no image
    h.agent.actor.llm.chat("sys " + SECRET, "text " + SECRET, [img])
    h.agent.verifier.llm.chat("sys " + SECRET, "text " + SECRET, [img])
    h.agent.reflector.llm.chat("sys " + SECRET, "text " + SECRET)
    h.agent.grounder.llm.chat("sys " + SECRET, "text " + SECRET)                       # no image → not blocked
    for name, rec in (("planner", h.planner), ("actor", h.actor), ("verifier", h.verifier),
                      ("reflector", h.reflector), ("grounder", h.grounder)):
        assert rec.seen and SECRET not in rec.blob(), f"secret reached {name}"
        assert all(n == 0 for n in rec.image_counts()), f"{name} received an image after blocking"


def test_secret_echoed_in_element_value_never_reaches_model():
    env = RecEnv([UIElement(1, "Token", "textbox", (0, 0, 50, 50), value=SECRET)])
    h = Harness(env, AgentConfig(secrets={"pw": SECRET}), actor_reply=done_reply)
    res = h.agent.run("do")
    assert SECRET not in h.planner.blob() and SECRET not in h.actor.blob()
    # 观测里的敏感值也必须清洗（回放 / 结果行同理）
    assert SECRET not in json.dumps(asdict(res), default=str, ensure_ascii=False)


def test_secret_in_prompts_and_feedback_is_scrubbed_in_requests():
    # 秘密同时出现在：无障碍元素值（进 verifier 提示词）、动作描述、以及 verifier 证据（变成反馈回到 actor）
    env = RecEnv([UIElement(1, "Token", "textbox", (0, 0, 50, 50), value=SECRET), button()])
    calls = {"n": 0}

    def actor(s, t, i):
        calls["n"] += 1
        if calls["n"] == 1:
            return json.dumps({"thought": "type", "action": {"type": "type", "text": "hello"}})
        return done_reply()
    h = Harness(env, AgentConfig(secrets={"pw": SECRET}, max_steps=5, max_steps_per_subgoal=4, max_replans=0,
                                 settle_timeout=0.02, final_check=False, verify_goals=False),
                actor_reply=actor, verifier_reply='{"verdict":"no_effect","evidence":"leaked ' + SECRET + '"}')
    h.agent.verifier.trigger = "every_step"       # 强制每步 L2，捕获 verifier 请求
    res = h.agent.run("do")
    assert h.verifier.seen, "verifier L2 must have been consulted"
    assert len(h.actor.seen) >= 2, "the actor must have received the verifier feedback once"
    for name, rec in (("planner", h.planner), ("actor", h.actor), ("verifier", h.verifier)):
        assert SECRET not in rec.blob(), f"secret reached {name}"
    assert all(n == 0 for n in h.planner.image_counts() + h.actor.image_counts() + h.verifier.image_counts())


def test_secret_in_action_string_fields_does_not_reach_later_requests():
    env = RecEnv([button()])
    calls = {"n": 0}

    def actor(s, t, i):
        calls["n"] += 1
        if calls["n"] == 1:
            return json.dumps({"thought": "go", "action": {"type": "type", "text": "hi",
                                                           "target": "btn " + SECRET, "reason": "because " + SECRET}})
        return done_reply()
    h = Harness(env, AgentConfig(secrets={"pw": SECRET}), actor_reply=actor)
    h.agent.run("do")
    assert len(h.actor.seen) >= 2, "the second request carries the step history"
    assert SECRET not in h.actor.blob(), "secrets copied into target/reason must be scrubbed before reuse"


def test_post_body_nested_fields_and_keys_are_scrubbed():
    class Inner:
        role = "verifier"

        def __init__(self):
            self.received = None

        def post(self, payload, betas=None):
            self.received = payload
            return {}

    scrub = Scrubber()
    scrub.add(SECRET)
    inner = Inner()
    gate = EgressGate(inner, scrub, role="verifier", vision_required=False)
    payload = {
        "system": "sys " + SECRET,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi " + SECRET}],
                      "meta": {"note": "deep " + SECRET}}],
        "extra_" + SECRET: "x",
    }
    gate.post(payload)
    assert inner.received is not None
    assert SECRET not in json.dumps(inner.received, ensure_ascii=False)
    assert any(k.startswith("extra_***") for k in inner.received), "dict keys must be scrubbed too"


def test_post_strips_blocked_images_and_adds_notice():
    class Inner:
        role = "actor"

        def __init__(self):
            self.received = None

        def post(self, payload, betas=None):
            self.received = payload
            return {}

    scrub = Scrubber()
    scrub.add(SECRET)                                   # sensitive → images blocked
    inner = Inner()
    gate = EgressGate(inner, scrub, role="actor", vision_required=False)
    payload = {"system": "s " + SECRET,
               "messages": [{"role": "user", "content": [
                   {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
                   {"type": "text", "text": "t " + SECRET}]}]}
    gate.post(payload)
    content = inner.received["messages"][0]["content"]
    assert all(b.get("type") != "image" for b in content), "blocked images must never be sent from a direct post()"
    assert any("[privacy mode]" in (b.get("text") or "") for b in content), "must tell the model there is no screenshot"
    assert SECRET not in json.dumps(inner.received, ensure_ascii=False)


def test_short_pin_registered_explicitly_and_images_blocked():
    env = RecEnv([UIElement(0, "PIN", "textbox", (10, 10, 200, 40), focused=True, is_password=True)])
    h = Harness(env, AgentConfig(secrets={"pin": "12"}),     # 短于 min_len：explicit 仍登记
                actor_reply=lambda s, t, i: json.dumps(
                    {"thought": "t", "action": {"type": "type", "text": "12"}}))
    res = h.agent.run("do")
    assert h.agent.scrubber.images_blocked and all(n == 0 for n in h.actor.image_counts())
    assert env.executed and env.executed[0].text == "12"


# =====================================================================  3 纯视觉路径
def test_pure_vision_actor_returns_privacy_blocked_without_api_request():
    env = RecEnv([pw_el()])
    h = Harness(env)
    vision_only = CapabilityPolicy(a11y_in_prompts=False, a11y_grounding=False, a11y_rules=False)
    h.agent.actor.policy = vision_only
    h.agent.actor.llm = h.actor                    # 复位，让 _install 重新按策略判定
    h.agent._install_egress_gates()
    res = h.agent.run("do")
    assert res.status == "privacy_blocked" and not res.claimed_done
    assert h.actor.seen == [], "vision-required actor must not send a request without a screenshot"
    assert all(n == 0 for n in h.planner.image_counts())    # 文字 planner 无图继续


def test_uitars_actor_is_treated_as_vision_required():
    env = RecEnv([pw_el()])
    h = Harness(env)
    uitars = UITarsActor(h.actor, "mock", coord_space="resized")
    h.agent.actor = uitars
    h.agent._install_egress_gates()
    res = h.agent.run("do")
    assert res.status == "privacy_blocked"
    assert h.actor.seen == []


def test_computer_use_post_is_blocked_before_sending():
    class FakeAnthropic:
        role = "actor"

        def __init__(self):
            self.image_max_side = 1568
            self.posts = []
            self.budget = Budget()

        def build_payload(self, system, text, images=None, tools=None):
            content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}]
            content.append({"type": "text", "text": text})
            return {"model": "x", "system": system, "messages": [{"role": "user", "content": content}],
                    "tools": tools}

        def post(self, payload, betas=None):
            self.posts.append((payload, betas))
            return {"content": []}

    env = RecEnv([pw_el()])
    h = Harness(env, AgentConfig(secrets={"pw": SECRET}))
    fake = FakeAnthropic()
    h.agent.actor = ClaudeComputerUseActor(fake, "mock")
    h.agent._install_egress_gates()
    res = h.agent.run("do")
    assert res.status == "privacy_blocked" and not res.claimed_done
    assert fake.posts == [], "the computer-use request must be intercepted before it is sent"


def test_computer_use_post_raises_before_sending_via_gate():
    class Inner:
        def post(self, payload, betas=None):
            raise AssertionError("must not send")

    scrub = Scrubber()
    scrub.mark_sensitive()
    gate = EgressGate(Inner(), scrub, role="actor", vision_required=True)
    with pytest.raises(PrivacyBlocked):
        gate.post({"messages": [{"role": "user", "content": [{"type": "image", "source": {"data": "A"}}]}]})


def test_zoom_crop_is_not_sent_when_blocked():
    rec = Rec(lambda s, t, i: '{"x":5,"y":5}')
    scrub = Scrubber()
    scrub.add(SECRET)
    grounder = Grounder(EgressGate(rec, scrub, role="grounder", vision_required=True), CoordMapper("pixel"))
    with pytest.raises(PrivacyBlocked):
        grounder.ground_zoom(Image.new("RGB", (100, 100)), "Save", (50, 50))
    assert rec.seen == [], "the zoom crop screenshot must not be sent"


# =====================================================================  4 秘密替换与闸门次序
def test_dangerous_secret_is_checked_by_real_content():
    env = RecEnv([UIElement(1, "Terminal", "textbox", (0, 0, 50, 50), focused=True)])
    h = Harness(env, AgentConfig(secrets={"cmd": "rm -rf /"}), guard=SafetyGuard(mode="deny"),
                actor_reply=lambda s, t, i: json.dumps(
                    {"thought": "t", "action": {"type": "type", "text": "<secret>cmd</secret>"}}))
    res = h.agent.run("do")
    assert env.executed == [], "the dangerous real content must be stopped before reaching the env"
    assert h.agent.guard.denied, "the denial must be remembered (terminal, not retried)"


def test_newline_password_passes_gate_by_real_text():
    env = RecEnv([pw_el()])
    h = Harness(env, AgentConfig(secrets={"pw": "pa\nss"}),
                actor_reply=lambda s, t, i: json.dumps(
                    {"thought": "t", "action": {"type": "type", "text": "<secret>pw</secret>"}}))
    res = h.agent.run("do")
    assert env.executed and env.executed[0].text == "pa\nss", \
        "the gate must inspect the real text (with the newline), and the executor receives it"


def test_unknown_placeholder_is_blocked_not_typed():
    env = RecEnv([UIElement(1, "Terminal", "textbox", (0, 0, 50, 50), focused=True)])
    h = Harness(env, AgentConfig(secrets={"cmd": "some-real-value"}))
    obs = env.observe()
    res = h.agent._execute_gated(Action("type", text="<secret>nope</secret>"), obs)
    assert not res.ok and "blocked_by_safety" in res.error
    assert "nope" in res.error and "not configured" in res.error        # 明确缺配置（名字本身不是秘密值）
    assert env.executed == [], "the raw placeholder must not be typed literally"
    assert res.error.count("blocked_by_safety") == 1


def test_executor_receives_raw_secret_only_when_allowed():
    env = RecEnv([button()])
    h = Harness(env, AgentConfig(secrets={"pw": SECRET}),
                actor_reply=lambda s, t, i: json.dumps(
                    {"thought": "t", "action": {"type": "type", "text": "prefix <secret>pw</secret>"}}))
    res = h.agent.run("do")
    assert env.executed and env.executed[0].text == "prefix " + SECRET
    # 原动作（日志 / 记忆 / 结果）里的占位符必须保持脱敏
    assert SECRET not in h.actor.blob()


# =====================================================================  5 日志截图落盘
def test_sensitive_run_writes_no_screenshot_files(tmp_path):
    log = TrajectoryLogger(tmp_path, "sens")
    env = RecEnv([pw_el(), button("Login")])
    h = Harness(env, logger=log)
    h.agent.run("do")
    log.close(report=False)
    assert list((Path(tmp_path) / "sens" / "shots").glob("*.png")) == [], \
        "once sensitive, no screenshot may be written (HTML replay would leak it)"


def test_non_sensitive_run_writes_screenshots(tmp_path):
    log = TrajectoryLogger(tmp_path, "plain")

    def actor(s, t, i):
        if not env.executed:
            return json.dumps({"thought": "click", "action": {"type": "click", "target": "OK"}})
        return done_reply()

    env = RecEnv([button()])
    h = Harness(env, logger=log, actor_reply=actor)
    h.agent.run("do")
    log.close(report=False)
    assert list((Path(tmp_path) / "plain" / "shots").glob("*.png")), "ordinary runs still keep screenshots"


# =====================================================================  6 预算 / 坐标变换保持
def test_budgetgate_accounting_survives_egress_gate():
    b = Budget()
    inner = ScriptedLLM(["a", "b"], budget=b, role="actor")
    gate = EgressGate(BudgetGate(inner, b, "actor"), Scrubber(), role="actor")
    gate.chat("s", "t")
    gate.chat("s", "t")
    assert b.calls == 2 and b.by_role.get("actor") == 2
    # 被隐私阻断拒绝的调用不得让底层发出请求，也就不该记一次预算
    scrub = Scrubber()
    scrub.mark_sensitive()
    blocked_inner = ScriptedLLM(["never"], budget=b, role="actor")
    bgate = EgressGate(BudgetGate(blocked_inner, b, "actor"), scrub, role="actor", vision_required=True)
    with pytest.raises(PrivacyBlocked):
        bgate.chat("s", "t", [Image.new("RGB", (5, 5))])
    assert b.calls == 2 and blocked_inner.prompts == []


def test_reply_transforms_survive_egress_gate():
    inner = ScriptedLLM(["x"], image_max_side=50)
    gate = EgressGate(inner, Scrubber(), role="actor")
    out = gate.chat("s", "t", [Image.new("RGB", (400, 200))])
    tf = reply_transform(out)
    assert tf is not None and tf.orig_size == (400, 200) and tf.sent_size == (50, 25), \
        "transforms computed from the images actually sent must be preserved"
    assert inner.prompts, "inner should have received the request"


def test_blocked_chat_has_no_transform_and_carries_no_image():
    inner = ScriptedLLM(["x"], image_max_side=50)
    scrub = Scrubber()
    scrub.mark_sensitive()
    gate = EgressGate(inner, scrub, role="actor")
    out = gate.chat("s", "t", [Image.new("RGB", (400, 200))])
    assert reply_transform(out) is None, "no screenshot sent → must not fabricate a coordinate transform"
    assert "[privacy mode]" in inner.prompts[-1], "the model must be told no screenshot is available"


# =====================================================================  7 直接注入 + 复用
def test_directly_injected_and_reused_model_is_gated():
    env = RecEnv([button()])
    shared = Rec(done_reply, "shared")
    h = Harness(env, AgentConfig(secrets={"pw": SECRET}))
    # 同一个对象同时当 planner 与 actor（直接注入 GUIAgent，而不是经 build_agent）
    h.agent.planner.llm = shared
    h.agent.actor.llm = shared
    h.agent._install_egress_gates()                     # 复用同一模型：两个角色各套一层门控
    assert isinstance(h.agent.planner.llm, EgressGate) and isinstance(h.agent.actor.llm, EgressGate)
    assert h.agent.planner.llm.scrubber is h.agent.scrubber is h.agent.actor.llm.scrubber
    h.agent.planner.llm.chat("sys " + SECRET, "text " + SECRET)
    h.agent.actor.llm.chat("sys " + SECRET, "text " + SECRET)
    assert SECRET not in shared.blob()

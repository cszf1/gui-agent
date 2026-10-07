"""v0.3.1 第二轮代码审查回归测试（docs/review-fixes.md「第二轮」）。

每个审查条目 rNN 的测试都先在 v0.3.0 上确认失败，再修复。新 API 在测试函数内部导入，
这样在旧版本上每条测试单独失败，而不是整个文件导入失败。本文件不依赖 Playwright（Web 浏览器测试见
test_web_review_v031.py），可在 Windows / macOS / Linux 上运行，也兼容 `-p no:cacheprovider -B`。
"""
import io
import json
import sys

import pytest
from PIL import Image, ImageDraw

from conftest import fast
from gua.actions import Action
from gua.agent import AgentConfig, GUIAgent
from gua.coords import CoordMapper
from gua.env.base import Env, ExecResult, Observation, UIElement
from gua.env.mock import MockButton, MockEnv
from gua.grounding import Grounder
from gua.llm.base import Budget, ScriptedLLM
from gua.planner import Actor, Planner, Subgoal
from gua.recovery import RecoveryPolicy
from gua.reflection import Reflector
from gua.safety import SafetyGuard
from gua.verify import Verdict, Verifier

SECRET = "S3cr3t-Pa55!"


def _obs(elements=(), text="", window="App", size=(400, 300), **kw):
    o = Observation(Image.new("RGB", size, (255, 255, 255)), 0.0, size, 1.0, window, "p", [window],
                    list(elements), "mock", "", text)
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def _danger_focused():
    return _obs([UIElement(0, "Delete account", "button", (10, 10, 120, 40), focused=True)])


def _pw_focused(name="Password"):
    return _obs([UIElement(0, name, "textbox", (10, 10, 200, 40), focused=True, is_password=True)])


# =====================================================================  r01 键盘激活危险按钮
ACTIVATIONS = [
    Action("hotkey", keys=["enter"]), Action("hotkey", keys=["Return"]), Action("hotkey", keys=["space"]),
    Action("hotkey", keys=["ctrl", "enter"]), Action("type", text="", submit=True), Action("type", text="\n"),
    Action("type", text=" "), Action("key_down", keys=["enter"]), Action("key_down", keys=["space"]),
]


@pytest.mark.parametrize("a", ACTIVATIONS, ids=lambda a: a.short())
def test_r01_keyboard_activation_of_focused_dangerous_button_denied(a):
    g = SafetyGuard(mode="deny")
    ok, why = g.gate(a, _danger_focused())
    assert not ok, f"{a.short()} activated a focused 'Delete account' button without confirmation"
    assert "delete" in why.lower()


def test_r01_click_and_keyboard_share_one_denial():
    asked = []
    g = SafetyGuard(mode="confirm", confirm_fn=lambda a, why: asked.append(why) or False)
    obs = _danger_focused()
    assert not g.gate(Action("click", x=50, y=20), obs)[0]
    for a in (Action("hotkey", keys=["enter"]), Action("hotkey", keys=["space"]),
              Action("type", text="", submit=True), Action("key_down", keys=["enter"])):
        ok, why = g.gate(a, obs)
        assert not ok and "previously rejected" in why, a.short()
    assert len(asked) == 1, "the same target activated another way must reuse the rejection, not re-ask"


def test_r01_unknown_focus_activation_needs_confirmation():
    g = SafetyGuard(mode="deny")
    unknown = _obs([], focus_state="unknown")
    assert not g.gate(Action("hotkey", keys=["enter"]), unknown)[0]
    assert not SafetyGuard(mode="deny").gate(Action("hotkey", keys=["enter"]), None)[0]


def test_r01_enter_in_textbox_checks_form_submit_target():
    g = SafetyGuard(mode="deny")
    risky = _obs([UIElement(0, "Confirm with your name", "textbox", (10, 10, 200, 40), focused=True,
                            attrs={"form_submit": "Delete account"})], focus_state="known")
    assert not g.gate(Action("type", text="Alice", submit=True), risky)[0]
    benign = _obs([UIElement(0, "Search", "textbox", (10, 10, 200, 40), focused=True,
                             attrs={"form_submit": "Search"})], focus_state="known")
    assert SafetyGuard(mode="deny").gate(Action("type", text="cats", submit=True), benign)[0]
    assert SafetyGuard(mode="deny").gate(Action("hotkey", keys=["enter"]), benign)[0]


def test_r01_focus_after_tab_on_same_observation_is_unknown():
    """恢复动作序列共用同一个观察：Tab 之后焦点已移动，Enter 的激活目标不可知 → 保守确认。"""
    g = SafetyGuard(mode="deny")
    obs = _obs([UIElement(0, "Search", "textbox", (10, 10, 200, 40), focused=True)], focus_state="known")
    assert g.gate(Action("hotkey", keys=["tab"]), obs)[0]
    assert not g.gate(Action("hotkey", keys=["enter"]), obs)[0]


def test_r01_android_dpad_center_is_activation():
    g = SafetyGuard(mode="deny")
    assert not g.gate(Action("hotkey", keys=["dpad_center"]), _danger_focused())[0]


# =====================================================================  r02 拒绝日志 / 确认终端明文
def test_r02_repeated_denial_log_redacted():
    g = SafetyGuard(mode="deny")
    obs = _pw_focused()
    for _ in range(3):
        assert not g.gate(Action("type", text=SECRET), obs)[0]
    assert SECRET not in json.dumps(g.log, ensure_ascii=False)
    assert all(SECRET not in k for k in g.denied), "denial signatures must not keep the plaintext"


def test_r02_redaction_independent_of_which_rule_fired():
    g = SafetyGuard(mode="deny")
    obs = _pw_focused()
    g.gate(Action("type", text=SECRET + "\x1b"), obs)               # 控制字符规则先命中
    g.gate(Action("type", text="rm -rf / " + SECRET), obs)          # 破坏性文本规则先命中
    assert SECRET not in json.dumps(g.log, ensure_ascii=False)


def test_r02_confirm_terminal_and_callbacks_never_see_plaintext(monkeypatch, capsys):
    obs = _pw_focused()
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))               # 非交互
    SafetyGuard(mode="confirm").gate(Action("type", text=SECRET), obs)
    monkeypatch.setattr(sys, "stdin", type("T", (io.StringIO,), {"isatty": lambda self: True})(""))
    prompts = []
    monkeypatch.setattr("builtins.input", lambda p="": prompts.append(p) or "n")
    SafetyGuard(mode="confirm").gate(Action("type", text=SECRET), obs)
    seen = []
    SafetyGuard(mode="confirm", confirm_fn=lambda a, why: seen.append(a.short() + why) or True).gate(
        Action("type", text=SECRET), obs)
    out = capsys.readouterr()
    assert prompts and seen
    for blob in (out.out, out.err, "".join(prompts), "".join(seen)):
        assert SECRET not in blob


def test_r02_safe_summary_api():
    from gua.sensitive import is_sensitive_type, safe_short, safe_view
    a = Action("type", text=SECRET, submit=True)
    assert is_sensitive_type(a, _pw_focused())
    assert SECRET not in safe_short(a, _pw_focused()) and SECRET not in json.dumps(safe_view(a, _pw_focused()))
    assert SECRET not in a.safe_short(_pw_focused())
    normal = _obs([UIElement(0, "Search", "textbox", (0, 0, 9, 9), focused=True)], focus_state="known")
    assert "cats" in safe_short(Action("type", text="cats"), normal)      # 已知普通输入框不脱敏
    assert "cats" not in safe_short(Action("type", text="cats"), _obs([], focus_state="unknown"))


# =====================================================================  r03 秘密全出口
class PasswordEnv(Env):
    """一个登录页：用户名框 + 密码框（焦点在密码框，输入不回显）+ Login 按钮。环境本身是唯一持有明文的执行器。"""
    platform = "mock"

    def __init__(self, echo_error=False):
        self.typed, self.logged_in, self.frames = "", False, 0
        self.echo_error = echo_error

    def observe(self, with_elements=True):
        img = Image.new("RGB", (400, 300), (250, 250, 250))
        d = ImageDraw.Draw(img)
        d.rectangle((20, 15, 20 + 20 * len(self.typed), 35), fill=(0, 0, 0))     # 掩码圆点（不回显明文）
        if self.logged_in:
            d.text((20, 200), "Welcome", fill=(0, 0, 0))
        els = [UIElement(0, "Password", "textbox", (10, 10, 300, 40), focused=True, is_password=True,
                         value=self.typed or None),
               UIElement(1, "Login", "button", (10, 60, 120, 90))]
        return Observation(img, 0.0, (400, 300), 1.0, "Login Page", "p", ["Login Page"], els if with_elements else [],
                           "mock", "", "Welcome" if self.logged_in else "Please sign in")

    def wait_until_stable(self, timeout=0.1, interval=0.0, **kw):
        return self.observe(), True

    def execute(self, a):
        if a.type == "type":
            self.typed += a.text or ""
            if self.echo_error:
                return ExecResult(False, f"backend error while typing {a.text!r}")
        elif a.type == "click" and self.typed:
            self.logged_in = self.typed == SECRET
        return ExecResult(True)


def _leak_run(tmp_path, actor_text, cfg_kw=None, echo_error=False):
    from gua.logger import TrajectoryLogger
    requests = []

    def rec(name, fn):
        def f(s, t, i):
            requests.append(f"[{name}] {s}\n{t}")
            return fn(s, t, i)
        return f

    env = PasswordEnv(echo_error=echo_error)
    budget = Budget()
    state = {"n": 0}

    def actor(s, t, i):
        state["n"] += 1
        if state["n"] == 1:
            return json.dumps({"thought": "enter the password", "action": {"type": "type", "text": actor_text}})
        if state["n"] == 2:
            return json.dumps({"thought": "log in", "action": {"type": "click", "element_id": 1}})
        return '{"thought":"done","action":{"type":"done"}}'

    plan = '{"subgoals":[{"goal":"log in","expect_text":"Welcome"}]}'
    llms = {n: ScriptedLLM(fn=rec(n, f), budget=budget, role=n) for n, f in {
        "planner": lambda s, t, i: plan, "actor": actor,
        "verifier": lambda s, t, i: '{"verdict":"uncertain","evidence":"cannot tell"}',
        "reflector": lambda s, t, i: '{"diagnosis":"unclear","advice":"look again"}'}.items()}
    printed = []
    guard = SafetyGuard(mode="confirm", confirm_fn=lambda a, why: printed.append(a.short() + " " + why) or
                        print(f"CONFIRM {a.short()} {why}", file=sys.stderr) or True)
    log = TrajectoryLogger(tmp_path, "leak")
    agent = GUIAgent(env, Planner(llms["planner"], "mock"), Actor(llms["actor"], "mock"),
                     Grounder(None, CoordMapper("pixel")), Verifier(llms["verifier"]),
                     RecoveryPolicy(platform="mock"),
                     AgentConfig(max_steps=6, max_replans=0, settle_timeout=0.1, **(cfg_kw or {})), budget,
                     logger=log, reflector=Reflector(llms["reflector"]), guard=guard)
    res = agent.run("sign in to the site")
    log.close(report=True)
    from dataclasses import asdict
    blobs = {"requests": "\n".join(requests), "memory": agent.mem.history_text(),
             "notes": agent.mem.notes_text(), "result": json.dumps(asdict(res), default=str, ensure_ascii=False),
             "confirm": "\n".join(printed)}
    for p in (tmp_path / "leak").rglob("*"):
        if p.is_file() and p.suffix in {".json", ".jsonl", ".html"}:
            blobs[p.name] = p.read_text(encoding="utf-8")
    return env, res, blobs


def test_r03_secret_never_appears_in_any_output_channel(tmp_path, capsys):
    env, res, blobs = _leak_run(tmp_path, SECRET)
    assert env.typed == SECRET, "the executor must still receive the raw text"
    assert "[verifier]" in blobs["requests"] and "[reflector]" in blobs["requests"]
    err = capsys.readouterr()
    blobs["stderr"], blobs["stdout"] = err.err, err.out
    assert {"steps.jsonl", "meta.json", "report.html"} <= set(blobs)
    leaks = [k for k, v in blobs.items() if SECRET in v]
    assert not leaks, f"secret leaked into: {leaks}"


def test_r03_exec_error_echo_is_scrubbed(tmp_path, capsys):
    env, res, blobs = _leak_run(tmp_path, SECRET, echo_error=True)
    err = capsys.readouterr()
    blobs["stderr"] = err.err
    leaks = [k for k, v in blobs.items() if SECRET in v]
    assert not leaks, f"secret echoed by the backend leaked into: {leaks}"


def test_r03_secret_placeholder_resolved_only_by_executor(tmp_path, capsys):
    env, res, blobs = _leak_run(tmp_path, "<secret>site_pw</secret>", cfg_kw={"secrets": {"site_pw": SECRET}})
    assert env.typed == SECRET and env.logged_in
    blobs["stderr"] = capsys.readouterr().err
    leaks = [k for k, v in blobs.items() if SECRET in v]
    assert not leaks, f"secret leaked into: {leaks}"
    assert "site_pw" in blobs["requests"], "the actor must be told which secret names exist"


def test_r03_verifier_l2_request_uses_safe_summary():
    seen = []
    v = Verifier(ScriptedLLM(fn=lambda s, t, i: seen.append(t) or '{"verdict":"uncertain"}'))
    obs = _pw_focused()
    v.model_check(obs, obs, Action("type", text=SECRET), "typed")
    assert seen and SECRET not in seen[0]


# =====================================================================  r05 白名单预取 fail-closed（假 route，无需 Playwright）
class _Req:
    def __init__(self, url, nav=True, nav_raises=False, method="GET"):
        self.url, self._nav, self._raise, self.method = url, nav, nav_raises, method
        self.frame = type("F", (), {"page": None})()

    def is_navigation_request(self):
        if self._raise:
            raise RuntimeError("frame detached")
        return self._nav


class _Route:
    def __init__(self, req, fetch_exc=None, resp=None):
        self.request, self.calls, self._exc, self._resp = req, [], fetch_exc, resp

    def fetch(self, **kw):
        self.calls.append("fetch")
        if self._exc:
            raise self._exc
        return self._resp

    def abort(self, *a):
        self.calls.append("abort")

    def continue_(self, *a, **k):
        self.calls.append("continue")

    def fulfill(self, **k):
        self.calls.append("fulfill")


def _web(**kw):
    from gua.env.web import WebEnv
    return WebEnv(allowed_domains=["127.0.0.1"], **kw)


def test_r05_prefetch_exception_fails_closed():
    env = _web()
    r = _Route(_Req("http://127.0.0.1:8000/slow"), fetch_exc=TimeoutError("Timeout 30000ms exceeded"))
    env._route(r)
    assert "continue" not in r.calls and r.calls[-1] == "abort"
    assert env.safety_failures and "TimeoutError" in env.safety_failures[-1]
    assert env._unreported, "the failure must be reported to the agent as a safety block"


def test_r05_navigation_flag_exception_is_conservative():
    env = _web()
    r = _Route(_Req("http://evil.example/x", nav_raises=True))
    env._route(r)
    assert r.calls == ["abort"]


def test_r05_unexpected_handler_error_aborts():
    class BadResp:
        status = 302

        @property
        def headers(self):
            raise ValueError("broken headers")
    env = _web()
    r = _Route(_Req("http://127.0.0.1:8000/r"), resp=BadResp())
    env._route(r)
    assert r.calls[-1] == "abort" and "continue" not in r.calls and "fulfill" not in r.calls


def test_r05_subresource_redirects_checked_when_blocking_subresources():
    class Resp:
        status = 302
        headers = {"location": "http://evil.example/pixel.gif"}
    env = _web(block_subresources=True)
    r = _Route(_Req("http://127.0.0.1:8000/img", nav=False), resp=Resp())
    env._route(r)
    assert r.calls == ["fetch", "abort"]


# =====================================================================  r06 Android / AX / 公共序列化层
def test_r06_android_password_text_never_kept():
    from gua.env.a11y import android_xml_to_elements
    xml = f"""<?xml version='1.0' ?><hierarchy rotation="0">
<node class="android.widget.LinearLayout" clickable="true" bounds="[0,0][1080,300]" text="" content-desc="">
  <node class="android.widget.EditText" password="true" focused="true" text="{SECRET}" content-desc=""
        resource-id="com.app:id/pwd" bounds="[10,10][1000,120]" clickable="true"/>
</node>
<node class="android.widget.TextView" password="true" text="{SECRET}" bounds="[10,400][900,500]"/>
</hierarchy>"""
    els, text = android_xml_to_elements(xml, (1080, 2000))
    obs = _obs(els, text)
    assert any(e.is_password for e in els)
    assert SECRET not in obs.all_text() and SECRET not in "\n".join(e.brief() for e in els)
    assert SECRET not in json.dumps([e.name for e in els] + [e.value for e in els])


def test_r06_ax_secure_value_cleared():
    from gua.env.a11y import ax_tree_to_elements
    tree = {"AXRole": "AXWindow", "AXPosition": [0, 0], "AXSize": [800, 600], "children": [
        {"AXRole": "AXSecureTextField", "AXValue": SECRET, "AXPosition": [10, 10], "AXSize": [200, 30],
         "AXFocused": True},
        {"AXRole": "AXTextField", "AXSubrole": "AXSecureTextField", "AXValue": SECRET,
         "AXPosition": [10, 50], "AXSize": [200, 30]},
        {"AXRole": "AXStaticText", "AXSubrole": "AXSecureTextField", "AXValue": SECRET,
         "AXPosition": [10, 90], "AXSize": [200, 30]}]}
    els, text = ax_tree_to_elements(tree, (800, 600))
    obs = _obs(els, text)
    assert SECRET not in obs.all_text() and SECRET not in "".join(e.brief() for e in els)


def test_r06_web_and_atspi_password_values_cleared():
    from gua.env.a11y import atspi_tree_to_elements, finalize, web_raws
    raws = web_raws([{"tag": "input", "type": "password", "name": "pw", "value": SECRET, "rect": [0, 0, 50, 20]},
                     {"tag": "input", "type": "text", "autocomplete": "current-password", "name": "pw2",
                      "value": SECRET, "rect": [0, 30, 50, 50]}])
    els, _ = finalize(raws, (400, 300))
    assert all(e.is_password and e.value is None for e in els)
    tree = {"role": "frame", "extents": [0, 0, 800, 600], "states": ["showing"], "children": [
        {"role": "password text", "name": "", "text": SECRET, "extents": [1, 1, 100, 20], "states": ["showing"]}]}
    els, text = atspi_tree_to_elements(tree, (800, 600))
    assert SECRET not in _obs(els, text).all_text()


def test_r06_common_serialization_layer_defense():
    e = UIElement(0, "pw", "textbox", (0, 0, 10, 10), value=SECRET, is_password=True)
    assert SECRET not in e.brief()
    assert SECRET not in _obs([e]).all_text()
    from gua.env.a11y import finalize
    els, _ = finalize([{"name": "pw", "role": "textbox", "rect": (0, 0, 50, 20), "value": SECRET,
                        "is_password": True}], (400, 300))
    assert els[0].value is None


# =====================================================================  r07 忙碌 / 未稳定 / 旧证据
def _busy_obs():
    return _obs([UIElement(0, "Loading...", "other", (0, 0, 100, 10), native_role="progressbar")],
                text="Report ready\nLoading...")


def test_r07_goal_check_busy_is_not_success():
    c = Verifier(None).check_goal(_busy_obs(), "generate report", "", "Report ready")
    assert c.verdict != Verdict.SUCCESS


def test_r07_final_check_busy_is_not_success():
    sg = [Subgoal(1, "generate report", expect_text="Report ready")]
    c = Verifier(None).check_final(_busy_obs(), "generate the report", sg)
    assert c.verdict != Verdict.SUCCESS


def test_r07_unstable_screen_is_not_success():
    ok = _obs(text="Report ready")
    v = Verifier(None)
    assert v.check_goal(ok, "g", "", "Report ready", stable=False).verdict == Verdict.UNCERTAIN
    sg = [Subgoal(1, "g", expect_text="Report ready")]
    assert v.check_final(ok, "t", sg, stable=False).verdict == Verdict.UNCERTAIN


def test_r07_stale_evidence_is_not_success():
    base = _obs(text="Report ready (yesterday)")
    same = _obs(text="Report ready (yesterday)")
    v = Verifier(None)
    assert v.check_goal(same, "g", "", "Report ready", baseline=base).verdict != Verdict.SUCCESS
    changed = _obs(text="Report ready (today)")
    assert v.check_goal(changed, "g", "", "Report ready", baseline=base).verdict == Verdict.SUCCESS
    sg = [Subgoal(1, "g", expect_text="Report ready")]
    assert v.check_final(same, "t", sg, baseline=base).verdict != Verdict.SUCCESS


def test_r07_step_rule_ignores_text_already_present_before():
    v = Verifier(None)
    before = _obs(text="Saved")
    after = _obs(text="Saved")
    after.screenshot = Image.new("RGB", (400, 300), (0, 0, 0))
    c = v.rule_check(before, after, Action("click", x=10, y=10), ExecResult(True), True, expect_text="Saved")
    assert c.verdict != Verdict.SUCCESS


def test_r07_static_full_progressbar_is_not_busy():
    from gua.verify.verifier import is_busy
    full = _obs([UIElement(0, "Storage", "other", (0, 0, 100, 10), native_role="progressbar",
                           attrs={"aria-valuenow": "100", "aria-valuemax": "100"})])
    assert not is_busy(full)
    assert is_busy(_busy_obs())


class ReportEnv(MockEnv):
    """点击 Generate 后出现 Loading 进度条；旧的 "Report ready" 文本一直在。clear_after=None 表示一直加载。"""

    def __init__(self, clear_after=None):
        super().__init__(title="Mock Reports", buttons=[MockButton("Generate", (100, 100, 260, 150),
                                                                   on_click=lambda e: e.state.update(loading=0))])
        self.clear_after = clear_after

    def observe(self, with_elements=True):
        o = super().observe(with_elements)
        o.text = "Report ready (old)"
        if "loading" in self.state:
            self.state["loading"] += 1
            if self.clear_after is None or self.state["loading"] < self.clear_after:
                o.elements.append(UIElement(len(o.elements), "Loading...", "other", (300, 300, 500, 320),
                                            native_role="progressbar"))
                o.text += "\nLoading..."
            else:
                o.text = "Report ready (new)"
        return o


def _report_agent(env):
    budget = Budget()
    state = {"n": 0}

    def actor(s, t, i):
        state["n"] += 1
        return ('{"action":{"type":"click","target":"Generate"}}' if state["n"] == 1
                else '{"action":{"type":"done"}}')
    return GUIAgent(env, Planner(ScriptedLLM(fn=lambda s, t, i: '{"subgoals":[{"goal":"generate the report",'
                                                                   '"expect_text":"Report ready"}]}', budget=budget), "mock"),
                    Actor(ScriptedLLM(fn=actor, budget=budget), "mock"), Grounder(None, CoordMapper("pixel")),
                    Verifier(None), RecoveryPolicy(platform="mock"),
                    AgentConfig(max_steps=5, max_steps_per_subgoal=4, max_replans=0, settle_timeout=0.05,
                                settle_interval=0.01, task_window="Mock"), budget, guard=SafetyGuard(mode="deny"))


def test_r07_agent_never_reports_done_while_busy():
    res = _report_agent(fast(ReportEnv(clear_after=None), timeout=0.05)).run("generate the report")
    assert res.status != "done" and not res.claimed_done


def test_r07_agent_waits_for_busy_to_clear_then_succeeds():
    # Two stable 1280x800 frames can take over 50 ms on a loaded CI runner.
    # This case tests eventual completion after Loading clears, not render speed.
    res = _report_agent(fast(ReportEnv(clear_after=4), timeout=0.5)).run("generate the report")
    assert res.status == "done", res.message


# =====================================================================  同类问题（审查范围外，本轮自查发现）
def test_rx_safety_assess_exception_fails_closed(monkeypatch):
    g = SafetyGuard(mode="deny")
    monkeypatch.setattr(g, "assess", lambda a, obs=None: (_ for _ in ()).throw(RuntimeError("boom")))
    ok, why = g.gate(Action("click", x=1, y=1), _obs())
    assert not ok and "safety check error" in why


def test_rx_drag_onto_trash_needs_confirmation():
    obs = _obs([UIElement(0, "report.docx", "listitem", (10, 10, 60, 60)),
                UIElement(1, "Trash", "listitem", (300, 200, 360, 260))])
    assert not SafetyGuard(mode="deny").gate(Action("drag", x=30, y=30, x2=330, y2=230), obs)[0]


def test_rx_single_char_keys_into_password_redacted():
    from gua.sensitive import safe_short
    assert '"s"' not in safe_short(Action("hotkey", keys=["s"]), _pw_focused())
    g = SafetyGuard(mode="deny")
    g.gate(Action("key_down", keys=["x"]), _pw_focused())
    g.gate(Action("type", text=SECRET), _pw_focused())
    assert SECRET not in json.dumps(g.log)


def test_rx_obscured_web_inputs_are_password():
    from gua.env.a11y import web_raws
    r = web_raws([{"tag": "input", "type": "text", "secure": True, "name": "pin", "value": "1234",
                   "rect": [0, 0, 9, 9]}])[0]
    assert r["is_password"] and r["value"] is None


def test_rx_no_playwright_switch_hides_module(monkeypatch):
    """conftest 的 GUA_TEST_NO_PLAYWRIGHT=1 开关：用于模拟审查者的无 Playwright 环境。"""
    import conftest
    assert hasattr(conftest, "hide_playwright")
    monkeypatch.setitem(sys.modules, "playwright", None)
    with pytest.raises(ImportError):
        import playwright.sync_api  # noqa: F401


def test_rx_clipboard_typing_never_leaves_text_on_clipboard(monkeypatch):
    """非 ASCII 文本经剪贴板粘贴：读取旧剪贴板失败时，v0.3 会把（可能是密码的）文本留在系统剪贴板上。"""
    import types
    from fakes import fake_pyautogui
    from gua.env.desktop import clipboard_type
    clip = types.ModuleType("pyperclip")
    clip.copies = []

    def paste():
        raise RuntimeError("clipboard locked")
    clip.paste, clip.copy = paste, lambda t: clip.copies.append(t)
    monkeypatch.setitem(sys.modules, "pyperclip", clip)
    monkeypatch.setitem(sys.modules, "pyautogui", fake_pyautogui())
    monkeypatch.setattr("time.sleep", lambda s: None)
    clipboard_type("密码-" + SECRET, "windows")
    assert clip.copies[0] == "密码-" + SECRET and clip.copies[-1] == ""


def test_rx_busy_indicators_survive_element_conversion():
    """v0.3 的 finalize 把 progressbar（角色 other）丢掉了：真实平台上 is_busy 只能靠文字，元素信号形同虚设。"""
    from gua.env.a11y import android_xml_to_elements, ax_tree_to_elements, finalize, web_raws
    from gua.verify.verifier import is_busy
    web, _ = finalize(web_raws([{"tag": "div", "role": "progressbar", "name": "Exporting", "rect": [0, 0, 90, 9]},
                                {"tag": "section", "role": "", "busy": "true", "name": "Feed",
                                 "rect": [0, 20, 90, 60]}]), (400, 300))
    assert is_busy(_obs(web))
    xml = """<hierarchy><node class="android.widget.ProgressBar" bounds="[0,0][100,100]" text=""/></hierarchy>"""
    assert is_busy(_obs(android_xml_to_elements(xml, (1080, 1920))[0]))
    ax = {"AXRole": "AXProgressIndicator", "AXPosition": [0, 0], "AXSize": [50, 10]}
    assert is_busy(_obs(ax_tree_to_elements(ax, (800, 600))[0]))


def test_rx_secrets_come_from_environment_not_config(monkeypatch):
    from gua.config import build_agent, load_config
    from conftest import ROOT
    cfg = load_config(ROOT / "configs" / "mock.yaml")
    cfg.setdefault("agent", {})["secrets_env"] = {"site_pw": "GUA_TEST_SITE_PW"}
    monkeypatch.setenv("GUA_TEST_SITE_PW", SECRET)
    agent = build_agent(cfg, fast(MockEnv()), None, llms=None)
    assert agent.cfg.secrets == {"site_pw": SECRET}
    assert "site_pw" in agent._actor_task("t") and SECRET not in agent._actor_task("t")

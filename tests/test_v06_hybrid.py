"""v0.6：混合动作空间（语义 / 后台动作 + shell / file / api）、动作级后置条件、换模态恢复。

全部在 mock 后端上运行（三大 OS 的 CI 都能跑），不需要 GUI 或网络。真实 Chromium / Xvfb 沙箱上的
端到端验证分别在 test_v06_web_semantic.py 与 test_v06_remote.py。
"""
import json
import os
import sys

import pytest

from conftest import fast
from gua.actions import Action, ActionParseError, modality_of, parse_action
from gua.agent import AgentConfig, GUIAgent
from gua.coords import CoordMapper
from gua.env.base import Observation, UIElement
from gua.env.mock import MockButton, MockEnv
from gua.grounding import Grounder
from gua.hybrid import HybridConfig, HybridExecutor, foreground_equivalent
from gua.llm.base import Budget, ScriptedLLM
from gua.planner import Actor, Planner
from gua.recovery import RecoveryPolicy
from gua.safety import SafetyGuard
from gua.tools import FilesConfig, ShellConfig, ToolRegistry
from gua.verify import Verifier
from gua.verify.postconditions import a11y_diff, evaluate, implied_postconditions, validate_postcondition
from PIL import Image


# ----------------------------------------------------------------------------- 动作解析与校验
def test_parse_hybrid_actions_and_aliases():
    a = parse_action({"type": "invoke", "element_id": 3, "method": "toggle"})
    assert a.type == "invoke" and a.method == "toggle"
    assert parse_action({"type": "toggle", "element_id": 1}).method == "toggle"          # 别名带出方法
    assert parse_action({"type": "invoke", "target": "Save"}).method == "invoke"
    s = parse_action({"type": "shell", "command": "ls -la 'my dir'"})
    assert s.command == ["ls", "-la", "my dir"]
    f = parse_action({"type": "file", "method": "write", "path": "a.txt", "text": "hi"})
    assert f.path == "a.txt"
    api = parse_action({"type": "api", "tool": "sheet.set_cell", "args": {"cell": "A1", "value": 3}})
    assert api.args == {"cell": "A1", "value": 3}
    e = parse_action({"type": "click", "element_id": 2,
                      "expect": {"kind": "element_state", "name": "Subscribe", "checked": True}})
    assert e.expect == [{"kind": "element_state", "name": "Subscribe", "checked": True}]
    assert modality_of(a) == "semantic" and modality_of(s) == "shell" and modality_of(Action("click", x=1, y=1)) == "gui"
    assert modality_of(Action("click", x=1, y=1), "uia_invoke") == "semantic"
    assert modality_of(a, "fallback:foreground:mock") == "gui"


@pytest.mark.parametrize("obj,code", [
    ({"type": "invoke", "element_id": 1, "method": "explode"}, "bad_value"),
    ({"type": "invoke", "method": "invoke"}, "missing_field"),
    ({"type": "invoke", "element_id": 1, "method": "set_value"}, "missing_field"),
    ({"type": "shell", "command": []}, "missing_field"),
    ({"type": "shell", "command": [1, 2]}, "bad_type"),
    ({"type": "file", "method": "rm", "path": "x"}, "bad_value"),
    ({"type": "file", "method": "write", "path": "x"}, "missing_field"),
    ({"type": "api"}, "missing_field"),
    ({"type": "api", "tool": "x", "args": [1]}, "bad_type"),
    ({"type": "click", "element_id": 1, "expect": [{"kind": "telepathy"}]}, "bad_value"),
    ({"type": "click", "element_id": 1, "expect": "saved"}, "bad_type"),
    ({"type": "invoke", "element_id": 1, "dispatch": "sideways"}, "bad_value"),
])
def test_hybrid_validation_errors_are_structured(obj, code):
    with pytest.raises(ActionParseError) as ei:
        parse_action(obj)
    assert ei.value.code == code


# ----------------------------------------------------------------------------- 后置条件
def _obs(elements, text="", title="App", img=None):
    img = img or Image.new("RGB", (200, 100), (255, 255, 255))
    return Observation(img, 0.0, img.size, active_window=title, elements=elements, text=text)


def test_postconditions_text_freshness_and_element_state():
    before = _obs([UIElement(0, "Subscribe", "checkbox", (0, 0, 10, 10), checked=False)], "Status: idle")
    after = _obs([UIElement(0, "Subscribe", "checkbox", (0, 0, 10, 10), checked=True)], "Status: saved")
    rep = evaluate([{"kind": "text_appears", "text": "saved"},
                    {"kind": "element_state", "name": "Subscribe", "checked": True},
                    {"kind": "text_disappears", "text": "idle"}], before, after)
    assert rep.verdict == "success", rep.evidence()
    stale = evaluate([{"kind": "text_appears", "text": "Status"}], before, _obs([], "Status: idle\nbanner"))
    assert stale.results[0].status == "unknown" and "stale" in stale.results[0].evidence
    bad = evaluate([{"kind": "element_state", "name": "Subscribe", "checked": False}], before, after)
    assert bad.verdict == "failed"


def test_postconditions_never_read_password_and_respect_vision_only():
    pw = UIElement(0, "password field", "textbox", (0, 0, 10, 10), value="hunter2", is_password=True)
    rep = evaluate([{"kind": "element_state", "name": "password field", "value": "hunter2"}], _obs([]), _obs([pw]))
    assert rep.results[0].status == "unknown"
    assert "value" not in rep.results[0].spec            # 期望值不回显到日志
    vo = evaluate([{"kind": "text_appears", "text": "x"}], _obs([]), _obs([], "x"), use_a11y=False)
    assert vo.results[0].status == "unknown"
    img2 = Image.new("RGB", (200, 100), (0, 0, 0))
    px = evaluate([{"kind": "pixel_change", "region": [0, 0, 100, 100], "min": 0.5}], _obs([]), _obs([], img=img2),
                  use_a11y=False)
    assert px.verdict == "success"
    assert validate_postcondition({"kind": "pixel_change", "region": [0, 0, 1]})


def test_a11y_diff_reports_added_removed_changed_without_password_values():
    b = _obs([UIElement(0, "A", "button", (0, 0, 1, 1)), UIElement(1, "pw", "textbox", (0, 0, 1, 1), value="a",
                                                                  is_password=True)])
    a = _obs([UIElement(0, "B", "button", (0, 0, 1, 1)), UIElement(1, "pw", "textbox", (0, 0, 1, 1), value="b",
                                                                  is_password=True)])
    d = a11y_diff(b, a)
    assert d["added"] and d["removed"] and not d["changed"]


def test_implied_postconditions_for_semantic_methods():
    obs = _obs([UIElement(0, "Sub", "checkbox", (0, 0, 1, 1), checked=False), UIElement(1, "Name", "textbox", (0, 0, 1, 1))])
    assert implied_postconditions(Action("invoke", element_id=0, method="toggle"), obs)[0]["checked"] is True
    assert implied_postconditions(Action("invoke", element_id=1, method="set_value", text="Al"), obs)[0]["value"] == "Al"
    assert implied_postconditions(Action("invoke", element_id=0, method="invoke"), obs) == []


# ----------------------------------------------------------------------------- 工具通道
def test_tools_disabled_by_default_and_allowlisted(tmp_path):
    reg = ToolRegistry()
    assert reg.check(Action("shell", command=["echo", "hi"])).startswith("shell tool is disabled")
    assert reg.check(Action("file", method="read", path="a")).startswith("file tool is disabled")
    reg = ToolRegistry(ShellConfig(enabled=True, allow=[os.path.basename(sys.executable)], workdir=str(tmp_path),
                                   confirm=False), FilesConfig(root=str(tmp_path)))
    assert "not in tools.shell.allow" in reg.check(Action("shell", command=["rm", "-rf", "/"]))
    assert "bare allowlisted name" in reg.check(Action("shell", command=[sys.executable, "-c", "1"]))
    r = reg.execute(Action("shell", command=[os.path.basename(sys.executable), "-c", "print('a|b; $(x)')"]))
    assert r.ok and "a|b; $(x)" in r.output and r.route == "shell"       # 元字符只是字面量，没有 shell 解释
    bad = reg.execute(Action("shell", command=[os.path.basename(sys.executable), "-c", "import sys; sys.exit(3)"]))
    assert not bad.ok and "exit code 3" in bad.error


def test_shell_env_does_not_leak_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    exe = os.path.basename(sys.executable)
    reg = ToolRegistry(ShellConfig(enabled=True, allow=[exe], workdir=str(tmp_path)))
    r = reg.execute(Action("shell", command=[exe, "-c", "import os; print(os.environ.get('OPENAI_API_KEY'))"]))
    assert r.ok and "sk-should-not-leak" not in r.output


def test_shell_timeout(tmp_path):
    exe = os.path.basename(sys.executable)
    reg = ToolRegistry(ShellConfig(enabled=True, allow=[exe], workdir=str(tmp_path), timeout=0.5))
    r = reg.execute(Action("shell", command=[exe, "-c", "import time; time.sleep(5)"]))
    assert not r.ok and "timeout" in r.error


def test_file_tool_confined_to_root(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("top secret")
    reg = ToolRegistry(files=FilesConfig(root=str(root)))
    assert reg.execute(Action("file", method="write", path="notes/a.txt", text="hello")).ok
    assert reg.execute(Action("file", method="read", path="notes/a.txt")).output == "hello"
    assert "a.txt" in reg.execute(Action("file", method="list", path="notes")).output
    for bad in ("../secret.txt", str(tmp_path / "secret.txt")):
        r = reg.execute(Action("file", method="read", path=bad))
        assert not r.ok and r.error.startswith("blocked_by_safety")
    if hasattr(os, "symlink"):
        try:
            os.symlink(tmp_path / "secret.txt", root / "link.txt")
        except OSError:
            return
        r = reg.execute(Action("file", method="read", path="link.txt"))
        assert not r.ok and "escapes" in r.error


def test_api_tool_registry():
    reg = ToolRegistry()
    calls = []
    reg.register_api("sheet.set_cell", lambda cell, value: calls.append((cell, value)) or {"ok": True},
                     "write a cell", {"cell": "A1-style", "value": "any"})
    reg.register_api("mail.send", lambda to: "sent", "send mail", {"to": "address"}, risky=True)
    r = reg.execute(Action("api", tool="sheet.set_cell", args={"cell": "A1", "value": 5}))
    assert r.ok and calls == [("A1", 5)] and r.route == "api:sheet.set_cell"
    assert reg.is_risky(Action("api", tool="mail.send", args={"to": "x"}))
    assert any("sheet.set_cell" in d for d in reg.prompt_docs())


# ----------------------------------------------------------------------------- 安全闸门覆盖工具与语义动作
def test_safety_gate_covers_tool_actions(tmp_path):
    exe = os.path.basename(sys.executable)
    g = SafetyGuard(mode="deny")
    ok, why = g.gate(Action("shell", command=["ls"]))
    assert not ok and "not configured" in why
    g = SafetyGuard(mode="deny", tools=ToolRegistry(ShellConfig(enabled=True, allow=[exe, "rm"], confirm=False,
                                                                workdir=str(tmp_path))))
    assert g.gate(Action("shell", command=[exe, "-c", "print(1)"]))[0]
    ok, why = g.gate(Action("shell", command=["rm", "-rf", "/tmp/x"]))
    assert not ok and "destructive" in why
    ok, why = g.gate(Action("shell", command=[exe, "-c", "<secret>pw</secret>"]))
    assert not ok and "secret placeholders" in why
    confirm = SafetyGuard(mode="confirm", confirm_fn=lambda a, r: False,
                          tools=ToolRegistry(ShellConfig(enabled=True, allow=[exe], workdir=str(tmp_path))))
    ok, why = confirm.gate(Action("shell", command=[exe, "-c", "1"]))
    assert not ok and "tools.shell.confirm" in why
    # 拒绝是终止性的：同一命令不再询问
    asked = []
    confirm.confirm_fn = lambda a, r: asked.append(1) or True
    assert not confirm.gate(Action("shell", command=[exe, "-c", "1"]))[0] and not asked


def test_semantic_invoke_shares_denial_with_click_on_same_element():
    obs = _obs([UIElement(0, "Delete account", "button", (0, 0, 50, 20))])
    g = SafetyGuard(mode="confirm", confirm_fn=lambda a, r: False)
    assert not g.gate(Action("click", element_id=0, x=25, y=10), obs)[0]
    asked = []
    g.confirm_fn = lambda a, r: asked.append(1) or True
    ok, why = g.gate(Action("invoke", element_id=0, method="invoke"), obs)
    assert not ok and "previously rejected" in why and not asked       # 换模态绕不过拒绝


def test_set_value_never_targets_password_fields():
    obs = _obs([UIElement(0, "password field", "textbox", (0, 0, 50, 20), is_password=True)])
    ok, why = SafetyGuard(mode="allow").gate(Action("invoke", element_id=0, method="set_value", text="x"), obs)
    assert not ok and "non-password" in why


# ----------------------------------------------------------------------------- 混合执行器
def _hy(env, **kw):
    cfg = HybridConfig(settle=0.0, **kw)
    return HybridExecutor(env, None, cfg, observe=lambda: env.observe())


def test_background_toggle_is_verified_and_does_not_move_pointer():
    env = MockEnv(buttons=[MockButton("Subscribe", (10, 10, 120, 40), role="checkbox", checked=False)])
    env.cursor = (500, 500)
    obs = env.observe()
    hy = _hy(env)
    r, fb = hy.run_semantic(Action("invoke", element_id=0, method="toggle"), obs)
    assert r.ok and fb is None and env.buttons[0].checked is True
    assert r.signals["background"] and not r.signals["pointer_moved"] and env.cursor == (500, 500)
    assert "checked=True" in r.signals["background_effect"] and hy.stats["background_verified"] == 1


def test_background_drop_falls_back_to_foreground_for_toggle():
    env = MockEnv(buttons=[MockButton("Subscribe", (10, 10, 120, 40), role="checkbox", checked=False,
                                      background_drop=True)])
    obs = env.observe()
    r, fb = _hy(env).run_semantic(Action("invoke", element_id=0, method="toggle"), obs)
    assert not r.ok and r.error.startswith("background_no_effect") and fb is not None and fb.type == "click"


def test_nonidempotent_invoke_without_effect_is_not_replayed():
    clicks = []
    env = MockEnv(buttons=[MockButton("Send", (10, 10, 120, 40), on_click=lambda e: clicks.append(1),
                                      background_drop=True)])
    obs = env.observe()
    r, fb = _hy(env).run_semantic(Action("invoke", element_id=0, method="invoke"), obs)
    assert fb is None and r.ok          # 无法证明生效也无法证明未生效：不重放，交给步骤验证 / 恢复
    assert clicks == []


def test_modal_dialog_refuses_background_and_unsupported_falls_back():
    env = MockEnv(buttons=[MockButton("Save", (10, 10, 120, 40))])
    env.popup = "Unsaved changes"
    obs = env.observe()
    r, fb = _hy(env).run_semantic(Action("invoke", element_id=1, method="invoke"), obs)   # OK 按钮在弹窗内
    assert fb is None or fb.type == "click"
    env2 = MockEnv(buttons=[MockButton("Custom", (10, 10, 120, 40), semantic=False)])
    r2, fb2 = _hy(env2).run_semantic(Action("invoke", element_id=0, method="invoke"), env2.observe())
    assert r2.error.startswith("background_unavailable") and fb2.type == "click"


def test_gui_only_ablation_rewrites_semantic_to_foreground():
    env = MockEnv(buttons=[MockButton("Name", (10, 10, 120, 40), role="textbox")])
    obs = env.observe()
    r, fb = _hy(env, mode="gui_only").run_semantic(Action("invoke", element_id=0, method="set_value", text="Al"), obs)
    assert fb.type == "type" and fb.clear and fb.element_id == 0 and "gui_only" in r.error
    assert foreground_equivalent(Action("invoke", element_id=0, method="scroll_into_view"), obs) is None


# ----------------------------------------------------------------------------- 端到端（agent + mock）
def _agent(env, actor_fn, plan, hybrid_cfg=None, guard=None, tools=None, max_steps=10):
    budget = Budget()
    planner = ScriptedLLM(fn=lambda s, t, i: plan, budget=budget)
    actor = ScriptedLLM(fn=actor_fn, budget=budget)
    ver = ScriptedLLM(fn=lambda s, t, i: '{"verdict":"uncertain","evidence":"stub"}', budget=budget)
    hy = HybridExecutor(env, tools, hybrid_cfg or HybridConfig(settle=0.0))
    return GUIAgent(env, Planner(planner, "mock"), Actor(actor, "mock"), Grounder(None, CoordMapper("pixel")),
                    Verifier(ver), RecoveryPolicy(platform="mock"),
                    AgentConfig(max_steps=max_steps, settle_timeout=0.2, final_check=True), budget,
                    guard=guard or SafetyGuard(mode="deny"), hybrid=hy, tools=tools)


PLAN_SUB = json.dumps({"subgoals": [{"goal": "subscribe", "expected": "checkbox checked",
                                     "expect_text": "subscribed=True"}]})


def _sub_env(**kw):
    def mark(e):
        e.state["subscribed"] = True
    return fast(MockEnv(title="Prefs", buttons=[MockButton("Subscribe", (10, 10, 200, 40), role="checkbox",
                                                           checked=False, on_click=mark, **kw)]))


def test_agent_semantic_action_end_to_end_records_modality():
    env = _sub_env()

    def actor(s, t, i):
        if "subscribed=True" in t:
            return '{"action":{"type":"done"}}'
        return json.dumps({"action": {"type": "invoke", "method": "invoke", "target": "Subscribe",
                                      "expect": [{"kind": "text_appears", "text": "subscribed=True"}]}})
    res = _agent(env, actor, PLAN_SUB).run("subscribe")
    assert res.status == "done", res.message
    assert res.modality["modality"].get("semantic") == 1 and res.modality["intrusions"] == 0


def test_agent_pointer_dead_click_recovers_by_switching_to_semantic():
    env = _sub_env(pointer_dead=True)
    seen = []

    def actor(s, t, i):
        seen.append(t)
        if "subscribed=True" in t:
            return '{"action":{"type":"done"}}'
        if any("completed via another modality" in x for x in seen):
            return '{"action":{"type":"done"}}'
        return '{"action":{"type":"click","target":"Subscribe"}}'
    res = _agent(env, actor, PLAN_SUB).run("subscribe")
    assert res.status == "done", (res.message, res.recoveries)
    assert any("switch_modality" in r for r in res.recoveries)
    assert env.buttons[0].checked is True          # 只切换了一次（没有重复切换回去）


def test_agent_background_drop_invoke_switches_to_foreground_once():
    env = _sub_env(background_drop=True)

    def actor(s, t, i):
        if "subscribed=True" in t or "completed via another modality" in t:
            return '{"action":{"type":"done"}}'
        return '{"action":{"type":"invoke","method":"invoke","target":"Subscribe"}}'
    res = _agent(env, actor, PLAN_SUB).run("subscribe")
    assert res.status == "done", (res.message, res.recoveries)
    assert env.state.get("subscribed") and env.buttons[0].checked is True


def test_agent_shell_tool_runs_through_gate_and_is_logged(tmp_path):
    exe = os.path.basename(sys.executable)
    tools = ToolRegistry(ShellConfig(enabled=True, allow=[exe], confirm=False, workdir=str(tmp_path)),
                         FilesConfig(root=str(tmp_path)))
    env = fast(MockEnv(title="Term", buttons=[]))
    plan = json.dumps({"subgoals": [{"goal": "write the report file", "expected": "file exists"}]})
    steps = iter([
        {"type": "shell", "command": [exe, "-c", "open('r.txt','w').write('42')"]},
        {"type": "file", "method": "read", "path": "r.txt", "expect": [{"kind": "output_contains", "text": "42"}]},
        {"type": "done"},
    ])
    agent = _agent(env, lambda s, t, i: json.dumps({"action": next(steps)}), plan, tools=tools,
                   guard=SafetyGuard(mode="deny", tools=tools))
    agent.cfg.final_check = False
    agent.cfg.verify_goals = False
    res = agent.run("write report")
    assert res.status == "done"
    assert (tmp_path / "r.txt").read_text() == "42"
    assert res.modality["modality"] == {"shell": 1, "file": 1}

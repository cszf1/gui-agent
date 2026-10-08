"""Outcome regressions for v0.6 review: replay, privacy and takeover."""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from gua.actions import Action
from gua.env.base import ExecResult, UIElement
from gua.env.mock import MockButton, MockEnv
from gua.env.remote import RemoteEnv
from gua.env.uia_execution import semantic_control
from gua.hybrid import HybridConfig, HybridExecutor
from gua.recovery import RecoveryPolicy
from gua.safety import SafetyGuard
from gua.tools import FilesConfig, ToolRegistry
from gua.sandbox.process import run_bounded
from gua.verify import Check, Verdict
from test_v06_mcp import make_server, call
from test_v06_uia_semantic import Ctrl, Value, bound


def test_delayed_toggle_is_not_sent_again_via_foreground():
    env = MockEnv(buttons=[MockButton("Subscribe", (10, 10, 100, 40), role="checkbox", checked=False)])
    pending, executions = [], []
    def execute(action):
        executions.append(action.type)
        pending.append(lambda: setattr(env.buttons[0], "checked", not env.buttons[0].checked))
        return ExecResult(True, route="semantic:delayed")
    env.execute = execute
    before = env.observe()
    result, fallback = HybridExecutor(env, cfg=HybridConfig(settle=0), observe=env.observe).run_semantic(
        Action("invoke", method="toggle", element_id=0), before)
    if fallback is not None:
        env.execute(fallback)
    for effect in pending: effect()
    assert executions == ["invoke"] and env.buttons[0].checked is True
    assert not result.ok and "background_no_effect" in result.error


@pytest.mark.parametrize("kind,method", [("invoke", "invoke"), ("invoke", "toggle"), ("click", None)])
@pytest.mark.parametrize("fixed", [False, True])
def test_uncertain_activation_is_not_replayed_by_recovery(kind, method, fixed):
    env = MockEnv(buttons=[MockButton("Save", (10, 10, 100, 40))])
    action = Action(kind, method=method, element_id=0, x=50, y=20)
    check = Check(Verdict.NO_EFFECT, "no visible acknowledgement", "L1", {"exec_error": "background_no_effect"})
    recovery = RecoveryPolicy(fixed_retry=fixed).decide(check, action, obs=env.observe(), hybrid=True)
    assert not recovery.actions


@pytest.mark.parametrize("stable,busy", [(False, False), (True, True)])
def test_mcp_does_not_issue_done_receipt_for_unsettled_postconditions(monkeypatch, stable, busy):
    srv, env = make_server()
    call(srv, "observe", include_image=False)
    obs = env.observe()
    if busy: obs.text = "Loading..."
    monkeypatch.setattr(srv.s.agent, "_settled_for_check", lambda baseline: (obs, stable))
    result = call(srv, "verify", postconditions=[{"kind": "element_state", "name": "Save", "exists": True}])
    assert result["isError"] and result["structuredContent"]["verdict"] != "verified_done"


def test_mcp_run_task_retains_session_privacy_and_denial_state(tmp_path, monkeypatch):
    import gua.config
    srv, _ = make_server(runs=tmp_path)
    srv.s.agent.scrubber.add("fixture-secret", explicit=True)
    srv.s.agent.guard.denied["activate|send"] = "previous rejection"
    srv.s.agent.guard.uncertain["activate|save"] = "unconfirmed previous dispatch"
    created = []
    original = gua.config.build_agent
    def build(*args, **kwargs):
        agent = original(*args, **kwargs)
        created.append(agent)
        return agent
    monkeypatch.setattr(gua.config, "build_agent", build)
    call(srv, "run_task", task="save", demo={"subgoals": [{"goal": "save", "expect_text": "saved=True",
         "steps": [{"click": "Save"}]}]})
    assert created[0].scrubber is srv.s.agent.scrubber and created[0].scrubber.images_blocked
    assert "activate|send" in created[0].guard.denied
    assert "activate|save" in created[0].guard.uncertain


def test_rejected_text_cannot_be_resubmitted_as_semantic_set_value():
    env = MockEnv(buttons=[MockButton("Command", (10, 10, 100, 40), role="textbox")])
    env.focused_field = "Command"
    obs = env.observe()
    asked = []
    guard = SafetyGuard(mode="confirm", confirm_fn=lambda action, why: asked.append(action) or len(asked) > 1)
    assert not guard.gate(Action("type", text="rm -rf /", clear=True, element_id=0), obs)[0]
    assert not guard.gate(Action("invoke", method="set_value", text="rm -rf /", element_id=0), obs)[0]
    assert len(asked) == 1


def test_rejected_file_overwrite_cannot_use_a_path_alias(tmp_path):
    (tmp_path / "report.txt").write_text("original")
    asked = []
    guard = SafetyGuard(mode="confirm", tools=ToolRegistry(files=FilesConfig(root=str(tmp_path))),
                        confirm_fn=lambda action, why: asked.append(action) or len(asked) > 1)
    assert not guard.gate(Action("file", method="write", path="report.txt", text="replace"))[0]
    assert not guard.gate(Action("file", method="write", path="./report.txt", text="replace"))[0]
    assert len(asked) == 1


@pytest.mark.parametrize("when", ["before", "after"])
def test_semantic_uia_value_security_is_rechecked_without_password_reads(when):
    ctrl = Ctrl("Edit", "Name", patterns=("Value",))
    binding = bound(ctrl)
    calls, reads = [], []
    class Pattern(Value):
        @property
        def Value(self):
            reads.append(ctrl.IsPassword)
            return ctrl.value
        def SetValue(self, text, waitTime=0):
            calls.append(text)
            ctrl.value = text
            if when == "after": ctrl.IsPassword = True
            return True
    gets = []
    def getter():
        gets.append(1)
        if when == "before" and len(gets) == 2: ctrl.IsPassword = True
        return Pattern(ctrl)
    ctrl.GetValuePattern = getter
    result = semantic_control(binding, "set_value", "fixture-secret")
    assert not result.ok and True not in reads
    if when == "before": assert calls == []


def test_remote_pointer_input_rejects_an_old_observation_before_contacting_daemon():
    env = RemoteEnv.__new__(RemoteEnv)
    env._snapshot_id = "0:2"
    requests = []
    env._req = lambda *args, **kwargs: requests.append(args) or {"ok": True}
    result = env.execute(Action("click", x=40, y=30, binding={"snapshot_id": "0:1"}))
    assert not result.ok and "stale_target" in result.error and not requests


@pytest.fixture
def daemon_server(monkeypatch, tmp_path):
    from gua.sandbox import daemon
    state = SimpleNamespace(token="fixture", takeover=True, takeover_since=1, epoch=0, counter=2,
                            lock=threading.RLock(), liveview="", takeover_url="", workdir=tmp_path)
    monkeypatch.setattr(daemon, "S", state, raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), daemon.Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def post(path, data=None):
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}" + path,
            json.dumps(data or {}).encode(), headers={"X-Gua-Token": "fixture"})
        try:
            response = opener.open(req, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())
    yield daemon, state, post
    server.shutdown(); server.server_close(); worker.join(timeout=2)


@pytest.mark.parametrize("endpoint,function", [("/launch", "launch"), ("/reset", "reset"), ("/snapshot", "snapshot")])
def test_takeover_blocks_launch_reset_and_snapshot(daemon_server, monkeypatch, endpoint, function):
    daemon, state, post = daemon_server
    effects = []
    monkeypatch.setattr(daemon, function, lambda *args: effects.append(args) or {"ok": True})
    code, result = post(endpoint, {"argv": ["gua-form"], "name": "fixture"})
    assert code == 423 and not effects


def test_daemon_rejects_old_full_snapshot_for_pointer_input(daemon_server, monkeypatch):
    daemon, state, post = daemon_server
    state.takeover = False
    effects = []
    monkeypatch.setattr(daemon, "do_input", lambda *args: effects.append(args) or {"ok": True})
    code, result = post("/input", {"type": "click", "x": 40, "y": 30, "snapshot_id": "0:1"})
    assert not result["ok"] and "stale_target" in result["error"] and not effects


def test_mcp_replan_cannot_replay_unconfirmed_activation_in_another_modality(monkeypatch):
    srv, env = make_server()
    dispatches, effects = [], []
    def delayed(action):
        dispatches.append(action.type)
        effects.append(lambda: env.state.update(saved=True))
        return ExecResult(True, route="semantic:delayed")
    monkeypatch.setattr(env, "execute", delayed)
    call(srv, "observe", include_image=False)
    first = call(srv, "act", action={"type": "invoke", "target": "Save", "method": "invoke"})
    second = call(srv, "act", action={"type": "click", "target": "Save"})
    assert first["isError"] and second["isError"]
    assert not second["structuredContent"]["executed"] and dispatches == ["invoke"]
    for effect in effects: effect()
    assert len(effects) == 1 and env.state["saved"]


def test_takeover_acknowledges_only_after_inflight_action_finishes(daemon_server, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import time
    daemon, state, post = daemon_server
    state.takeover = False
    entered, release, requested = threading.Event(), threading.Event(), threading.Event()
    def slow_input(action):
        entered.set()
        assert release.wait(timeout=3)
        return {"ok": True}
    def takeover():
        requested.set()
        return post("/takeover")
    monkeypatch.setattr(daemon, "do_input", slow_input)
    with ThreadPoolExecutor(max_workers=2) as pool:
        action = pool.submit(post, "/input", {"type": "click", "x": 20, "y": 20})
        try:
            assert entered.wait(timeout=2)
            human = pool.submit(takeover)
            assert requested.wait(timeout=2)
            time.sleep(0.05)
            assert not human.done(), "takeover acknowledged while an agent action could still execute"
        finally:
            release.set()
        assert action.result()[1]["ok"] and human.result()[1]["ok"]
    assert post("/input", {"type": "click", "x": 20, "y": 20})[0] == 423


@pytest.mark.parametrize("clear", [False, True])
def test_remote_foreground_input_stops_immediately_on_focus_theft(monkeypatch, clear):
    from gua.sandbox import daemon
    calls = []
    state = SimpleNamespace(focused=True)
    constants = SimpleNamespace(STATE_FOCUSED=1, STATE_DEFUNCT=2)
    node = SimpleNamespace(getState=lambda: SimpleNamespace(contains=lambda k: state.focused if k == 1 else False),
                           getRoleName=lambda: "text")
    monkeypatch.setattr(daemon, "resolve", lambda path: (node, constants))
    monkeypatch.setattr(daemon, "active_window", lambda: ("10", "Form", "fixture"))
    def input(*args):
        calls.append(args)
        state.focused = False
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(daemon, "xdo", input)
    result = daemon.do_input({"type": "type", "text": "ab", "clear": clear, "submit": True,
        "focus_path": "field", "focus_role": "text", "focus_password": False, "focus_window": "10"})
    assert not result["ok"] and "stale_target" in result["error"]
    assert calls == [("key", "ctrl+a")] if clear else calls == [("type", "--delay", "0", "--", "a")]


def test_postcondition_does_not_select_one_of_two_identically_named_fields():
    from gua.verify.postconditions import evaluate
    env = MockEnv(buttons=[MockButton("Name", (10, 10, 100, 40), role="textbox"),
                           MockButton("Name", (10, 60, 100, 90), role="textbox")])
    obs = env.observe()
    obs.elements[0].value, obs.elements[1].value = "Alice", "Bob"
    report = evaluate([{"kind": "element_state", "name": "Name", "value": "Alice"}], obs, obs)
    assert report.verdict == "uncertain"


def test_failed_remote_reset_does_not_claim_success_or_reuse_bindings(monkeypatch):
    from gua.env.remote import RemoteError
    env = RemoteEnv("http://127.0.0.1:1", "fixture")
    env._snapshot_id = "0:1"
    monkeypatch.setattr(env, "_req", lambda *args, **kwargs: {"ok": False, "error": "no snapshot"})
    with pytest.raises(RemoteError, match="no snapshot"):
        env.reset("missing")
    assert env._snapshot_id == "0:1"


def test_process_output_is_bounded_while_reading_not_after_capture(tmp_path):
    import os
    import sys
    import tracemalloc
    tracemalloc.start()
    try:
        result = run_bounded([sys.executable, "-c", "import os; [os.write(1,b'x'*65536) for _ in range(1024)]"],
                             cwd=str(tmp_path), env=dict(os.environ), timeout=10, max_output=128)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert result.returncode == 0 and len(result.stdout) < 200 and "truncated" in result.stdout
    assert peak < 2_000_000, "64 MB of subprocess output was accumulated in parent memory"


@pytest.mark.skipif(__import__("os").name != "posix", reason="POSIX process-group cleanup")
def test_shell_timeout_kills_child_before_it_can_write_later(tmp_path):
    import os
    import subprocess
    import sys
    import time
    child = "import time; time.sleep(0.6); open('late.txt','w').write('replayed')"
    parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); time.sleep(20)"
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded([sys.executable, "-c", parent], cwd=str(tmp_path), env=dict(os.environ), timeout=0.15,
                    limits=True)
    time.sleep(0.65)
    assert not (tmp_path / "late.txt").exists()


def test_resume_keeps_denials_and_unconfirmed_dispatches_across_agents():
    from test_v06_hybrid import _agent, PLAN_SUB
    env = MockEnv(buttons=[MockButton("Subscribe", (10, 10, 100, 40), role="checkbox", checked=False)])
    agent = _agent(env, lambda *args: '{"action":{"type":"invoke","method":"toggle","target":"Subscribe"}}',
                   PLAN_SUB, max_steps=2)
    old = Action("invoke", method="toggle", element_id=0)
    source_guard = SafetyGuard()
    source_guard.remember_uncertain(old, env.observe())
    checkpoint = {"task": "subscribe", "safety_state": {"uncertain": source_guard.uncertain,
                                                         "denied": {"fixture-rejection": "rejected"}}}
    result = agent.run("subscribe", resume=checkpoint)
    assert not result.claimed_done and not env.buttons[0].checked and not env.log
    assert "fixture-rejection" in agent.guard.denied


@pytest.mark.parametrize("checkpoint,status", [
    ({"task": "another task"}, "invalid_checkpoint"),
    ({"task": "subscribe", "safety_state": {"privacy_blocked": True}}, "privacy_blocked"),
])
def test_resume_does_not_execute_another_task_or_discard_private_redaction_state(checkpoint, status):
    from test_v06_hybrid import _agent, PLAN_SUB
    env = MockEnv(buttons=[MockButton("Subscribe", (10, 10, 100, 40), role="checkbox", checked=False)])
    agent = _agent(env, lambda *args: '{"action":{"type":"done"}}', PLAN_SUB)
    result = agent.run("subscribe", resume=checkpoint)
    assert result.status == status and not result.claimed_done and not env.log

"""v0.6 沙箱电脑端到端：本机 Xvfb + AT-SPI + GTK 应用 + gua 守护进程 + RemoteEnv（真实进程，不是假对象）。

需要 Linux 与：xvfb xdotool dbus-x11 at-spi2-core openbox python3-gi gir1.2-gtk-3.0 python3-pyatspi。
缺任何一个就整体跳过（Windows / macOS CI 自动跳过）。Docker 镜像（sandbox/Dockerfile）跑的是同一个守护进程。
"""
import json
import sys
import threading
import time

import pytest

if not sys.platform.startswith("linux"):
    pytest.skip("sandbox e2e needs Linux", allow_module_level=True)

from gua.sandbox.local import LocalSandbox, SandboxPool, requirements  # noqa: E402

_req = requirements()
if not all(_req[k] for k in ("Xvfb", "xdotool", "openbox", "dbus-launch", "pyatspi", "gtk")):
    pytest.skip(f"sandbox prerequisites missing: {_req}", allow_module_level=True)

pytestmark = pytest.mark.sandbox

from gua.actions import Action  # noqa: E402
from gua.agent import AgentConfig, GUIAgent  # noqa: E402
from gua.coords import CoordMapper  # noqa: E402
from gua.grounding import Grounder  # noqa: E402
from gua.hybrid import HybridConfig, HybridExecutor  # noqa: E402
from gua.llm.base import Budget, ScriptedLLM  # noqa: E402
from gua.planner import Actor, Planner  # noqa: E402
from gua.recovery import RecoveryPolicy  # noqa: E402
from gua.safety import SafetyGuard  # noqa: E402
from gua.verify import Verifier  # noqa: E402


def wait_for(env, name, timeout=15):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        obs = env.observe()
        if any(e.name == name for e in obs.elements):
            return obs
        time.sleep(0.2)
    raise AssertionError(f"{name!r} never appeared")


@pytest.fixture(scope="module")
def box():
    sb = LocalSandbox(shell=False, liveview=True).start()
    env = sb.remote_env()
    env.launch(sb.form_app)
    wait_for(env, "Save")
    env.snapshot("clean")
    yield sb
    sb.stop()


@pytest.fixture
def env(box):
    e = box.remote_env(takeover_timeout=20, poll=0.1)
    e.reset("clean")
    wait_for(e, "Save")
    return e


def el(obs, name, role=None):
    return next(x for x in obs.elements if x.name == name and (role is None or x.role == role))


def test_semantic_actions_do_not_move_the_pointer_and_change_real_app_state(env):
    obs = env.observe()
    p0 = env.pointer_position()
    for name, method, text in (("Subscribe", "toggle", None), ("Name", "set_value", "Alice"), ("Save", "invoke", None)):
        obs = env.observe()
        r = env.execute(env.bind_action(Action("invoke", element_id=el(obs, name).id, method=method, text=text), obs))
        assert r.ok and r.route == f"atspi:{method}", r
    assert env.pointer_position() == p0
    time.sleep(0.3)
    assert json.loads(env.read_file("result.json")) == {"name": "Alice", "subscribe": True, "plan": "Free"}
    assert "Saved: Alice" in env.observe().text


def test_foreground_input_moves_pointer_and_types(env):
    obs = env.observe()
    name = el(obs, "Name")
    assert env.execute(Action("click", x=name.center[0], y=name.center[1])).ok
    assert env.execute(Action("type", text="Bob", clear=True)).ok
    obs = env.observe()
    save = el(obs, "Save")
    assert env.execute(Action("click", x=save.center[0], y=save.center[1])).ok
    assert env.pointer_position() == save.center
    time.sleep(0.3)
    assert json.loads(env.read_file("result.json"))["name"] == "Bob"


def test_stale_semantic_target_is_refused(env):
    obs = env.observe()
    a = env.bind_action(Action("invoke", element_id=el(obs, "Save").id, method="invoke"), obs)
    env.observe()                                     # newer snapshot → old binding is stale
    r = env.execute(a)
    assert not r.ok and r.error.startswith("stale_target")


def test_takeover_pauses_agent_actions_and_screenshots_then_handback_invalidates(box, env):
    obs = env.observe()
    a = env.bind_action(Action("invoke", element_id=el(obs, "Save").id, method="invoke"), obs)
    lv = env.takeover()
    assert lv["ok"]
    r = env.execute(a)
    assert not r.ok and r.error.startswith("paused_for_human")
    nowait = box.remote_env(wait_for_handback=False)
    with pytest.raises(Exception):
        nowait.observe()                               # 接管期间不截图
    with pytest.raises(Exception):
        env.handback()                                 # v0.7：agent 侧没有人工交还令牌，不能自行交还
    box.operator_env().handback()
    r = env.execute(a)                                 # 交还后旧观察失效
    assert not r.ok and r.error.startswith("stale_target")
    assert env.observe().elements


def test_agent_observe_waits_for_handback(box, env):
    human = box.operator_env()
    human.takeover()
    threading.Timer(0.8, human.handback).start()
    t0 = time.monotonic()
    obs = env.observe()
    assert time.monotonic() - t0 >= 0.5 and obs.elements
    assert [e["event"] for e in env.takeover_events][-2:] == ["agent_waiting", "agent_resumed"]


def test_snapshot_reset_restores_files_and_relaunches_app(env):
    obs = env.observe()
    env.execute(env.bind_action(Action("invoke", element_id=el(obs, "Save").id, method="invoke"), obs))
    time.sleep(0.3)
    assert env.read_file("result.json")
    env.reset("clean")
    obs = wait_for(env, "Save")
    assert env.read_file("result.json") == "" and "Status: idle" in obs.text
    assert el(obs, "Subscribe").checked is False


def test_shell_is_refused_when_sandbox_started_without_shell(env):
    r = env.run_tool(Action("shell", command=["ls"]))
    assert not r.ok and "without --shell" in r.error


PLAN = json.dumps({"subgoals": [{"goal": "fill and save the form", "expected": "status shows Saved: Bob",
                                 "expect_text": "Saved: Bob"}]})
SCRIPT = [
    {"type": "invoke", "method": "set_value", "target": "Name", "text": "Bob",
     "expect": [{"kind": "element_state", "name": "Name", "value": "Bob"}]},
    {"type": "invoke", "method": "toggle", "target": "Subscribe",
     "expect": [{"kind": "element_state", "name": "Subscribe", "checked": True}]},
    {"type": "invoke", "method": "invoke", "target": "Save", "expect": [{"kind": "text_appears", "text": "Saved: Bob"}]},
    {"type": "done"},
]


def run_agent(env, mode="hybrid", guard=None, script=SCRIPT, plan=PLAN):
    steps = iter(script)
    budget = Budget()
    agent = GUIAgent(env, Planner(ScriptedLLM(fn=lambda s, t, i: plan, budget=budget), "remote"),
                     Actor(ScriptedLLM(fn=lambda s, t, i: json.dumps({"action": next(steps)}), budget=budget), "remote"),
                     Grounder(None, CoordMapper("pixel")), Verifier(None), RecoveryPolicy(platform="remote"),
                     AgentConfig(max_steps=8, settle_timeout=2.0, platform="remote"), budget,
                     guard=guard or SafetyGuard(mode="deny"),
                     hybrid=HybridExecutor(env, None, HybridConfig(mode=mode, settle=0.15)))
    return agent.run("fill the form as Bob and save")


@pytest.mark.parametrize("mode", ["hybrid", "gui_only"])
def test_agent_end_to_end_on_sandbox_hybrid_vs_gui_only(env, mode):
    res = run_agent(env, mode)
    assert res.status == "done", (res.message, res.recoveries)
    assert json.loads(env.read_file("result.json")) == {"name": "Bob", "subscribe": True, "plan": "Free"}
    if mode == "hybrid":
        assert res.modality["modality"] == {"semantic": 3} and res.modality["intrusions"] == 0
        assert res.modality["background_verified"] >= 2
    else:
        assert res.modality["modality"] == {"gui": 3} and res.modality["fallbacks"] == 3


def test_safety_gate_blocks_semantic_delete_in_sandbox(env):
    script = [{"type": "invoke", "method": "invoke", "target": "Delete all"}] + [{"type": "fail", "text": "refused"}] * 10
    plan = json.dumps({"subgoals": [{"goal": "delete everything"}]})
    obs = env.observe()
    env.execute(env.bind_action(Action("invoke", element_id=el(obs, "Name").id, method="set_value", text="keep"), obs))
    res = run_agent(env, script=script, plan=plan)
    assert res.status != "done" and any("irreversible" in e["reason"] for e in res.safety_events)
    assert el(env.observe(), "Name").value == "keep"


def test_parallel_sandboxes_are_isolated():
    with SandboxPool(2, liveview=False) as boxes:
        envs = []
        for b in boxes:
            e = b.remote_env()
            e.launch(b.form_app)
            wait_for(e, "Save")
            envs.append(e)
        results = [None, None]

        def work(i):
            script = [dict(s) for s in SCRIPT]
            script[0]["text"] = script[0]["expect"][0]["value"] = f"User{i}"
            script[2]["expect"] = [{"kind": "text_appears", "text": f"Saved: User{i}"}]
            plan = json.dumps({"subgoals": [{"goal": "save", "expect_text": f"Saved: User{i}"}]})
            results[i] = run_agent(envs[i], script=script, plan=plan)
        ts = [threading.Thread(target=work, args=(i,)) for i in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
        assert [r.status for r in results] == ["done", "done"]
        assert [json.loads(e.read_file("result.json"))["name"] for e in envs] == ["User0", "User1"]


def test_cli_sandbox_status_and_config_builds_remote_env(box, monkeypatch):
    import os
    import subprocess
    from gua.config import build_env, load_config
    out = subprocess.run([sys.executable, "-m", "gua.cli", "sandbox", "status", "--remote-url", box.url],
                         capture_output=True, text=True, env=dict(os.environ, GUA_SANDBOX_TOKEN=box.token), timeout=30)
    assert json.loads(out.stdout)["ok"] is True
    monkeypatch.setenv("GUA_SANDBOX_TOKEN", box.token)
    cfg = load_config("configs/default.yaml", {"env": {"platform": "remote", "remote": {"url": box.url}}})
    env = build_env(cfg)
    assert env.platform == "remote" and env.health()["ok"]


def test_mcp_session_over_remote_sandbox_with_takeover(box, env, tmp_path):
    import io
    from gua.config import load_config
    from gua.mcp_server import Server, Session
    cfg = load_config("configs/default.yaml", {"env": {"platform": "remote"}, "agent": {"settle_timeout": 2.0}})
    cfg["runs_dir"] = str(tmp_path)
    srv = Server(Session(cfg, env=env), out=io.StringIO())

    def call(name, **args):
        return srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": args}})["result"]
    assert "checkbox 'Subscribe'" in call("observe", include_image=False)["content"][0]["text"]
    r = call("act", action={"type": "invoke", "method": "toggle", "target": "Subscribe"})["structuredContent"]
    assert r["verdict"] == "success" and r["route"] == "atspi:toggle" and r["pointer_moved"] is False
    lv = call("takeover")["structuredContent"]
    assert lv["ok"]
    r = call("act", action={"type": "invoke", "method": "invoke", "target": "Save"})
    assert r["isError"] and "paused_for_human" in r["structuredContent"]["error"]
    r = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",   # v0.7：模型不能通过 MCP 交还控制权
                    "params": {"name": "handback", "arguments": {}}})
    assert r["error"]["code"] == -32602
    box.operator_env().handback()            # 人工操作端用独立令牌交还
    call("observe", include_image=False)
    v = call("verify", postconditions=[{"kind": "element_state", "name": "Subscribe", "checked": True}])
    assert v["structuredContent"]["verdict"] == "verified_done"

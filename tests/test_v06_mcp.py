"""v0.6：`gua mcp` 服务器——协议握手、observe / act（带验证结论）/ verify（新鲜度）/ run_task / 安全闸门。

前半部分在进程内驱动 Server（mock 后端，任何 OS 都能跑）；最后一个测试以子进程方式启动真实的
`python -m gua.cli mcp --platform web`（Chromium），通过 stdio 交换 JSON-RPC 消息。
"""
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import fast
from gua.config import load_config
from gua.env.mock import MockButton, MockEnv
from gua.mcp_server import Server, Session

ROOT = Path(__file__).resolve().parents[1]


def make_server(allow=False, runs=None):
    def save(e):
        e.state["saved"] = True

    def nuke(e):
        e.state["deleted"] = True
    env = MockEnv(title="Editor", buttons=[MockButton("Save", (10, 10, 120, 40), on_click=save),
                                           MockButton("Delete account", (10, 60, 160, 90), on_click=nuke),
                                           MockButton("Subscribe", (10, 110, 160, 140), role="checkbox", checked=False)])
    fast(env)
    cfg = load_config(str(ROOT / "configs" / "default.yaml"), {"env": {"platform": "mock"},
                                                               "agent": {"settle_timeout": 0.2}})
    cfg["runs_dir"] = str(runs or (ROOT / "runs-test-tmp"))
    return Server(Session(cfg, env=env, allow_risky=allow), out=io.StringIO()), env


def rpc(srv, method, params=None, mid=1):
    return srv.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})


def call(srv, name, **args):
    r = rpc(srv, "tools/call", {"name": name, "arguments": args})
    return r["result"]


def test_initialize_and_tools_list():
    srv, _ = make_server()
    init = rpc(srv, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "t", "version": "0"}})
    assert init["result"]["serverInfo"]["name"] == "gua" and "tools" in init["result"]["capabilities"]
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = {t["name"] for t in rpc(srv, "tools/list")["result"]["tools"]}
    assert {"observe", "act", "verify", "run_task", "takeover"} <= names
    assert "handback" not in names          # v0.7：交还只属于人工操作端
    assert rpc(srv, "nope")["error"]["code"] == -32601


def test_observe_act_returns_verification_verdict_and_modality():
    srv, env = make_server()
    obs = call(srv, "observe")
    text = obs["content"][0]["text"]
    assert "button 'Save'" in text and "{toggle}" in text and obs["content"][1]["type"] == "image"
    r = call(srv, "act", action={"type": "invoke", "method": "toggle", "target": "Subscribe"})
    sc = r["structuredContent"]
    assert sc["verdict"] == "success" and sc["modality"] == "semantic" and sc["background"] is True
    assert sc["pointer_moved"] is False and env.buttons[2].checked is True and not r["isError"]
    bad = call(srv, "act", action={"type": "click", "target": "Nowhere"})
    assert bad["isError"]


def test_act_risky_action_is_denied_without_yes():
    srv, env = make_server()
    call(srv, "observe")
    r = call(srv, "act", action={"type": "click", "target": "Delete account"})
    assert r["isError"] and "blocked_by_safety" in r["structuredContent"]["error"]
    assert not env.state.get("deleted")
    r = call(srv, "act", action={"type": "invoke", "method": "invoke", "target": "Delete account"})
    assert r["isError"] and not env.state.get("deleted")                 # 换模态同样被拒


def test_verify_requires_fresh_evidence_and_checks_postconditions():
    srv, env = make_server()
    call(srv, "observe")
    v = call(srv, "verify", expect_text="saved=True")
    assert v["isError"] and v["structuredContent"]["verdict"] != "verified_done"
    call(srv, "act", action={"type": "click", "target": "Save"})
    v = call(srv, "verify", expect_text="saved=True", goal="document saved")
    rc = v["structuredContent"]
    assert rc["verdict"] == "verified_done" and rc["screen"]["screenshot_sha256"]
    call(srv, "verify", mark=True)
    stale = call(srv, "verify", expect_text="saved=True")                # 新基线之后没有变化 = 旧证据
    assert stale["structuredContent"]["verdict"] != "verified_done"
    pc = call(srv, "verify", postconditions=[{"kind": "element_state", "name": "Subscribe", "checked": True}])
    assert pc["structuredContent"]["verdict"] == "failed"


def test_run_task_with_scripted_demo_returns_receipts(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    srv, env = make_server(runs=tmp_path)
    demo = {"subgoals": [{"goal": "save", "expected": "saved", "expect_text": "saved=True",
                          "steps": [{"click": "Save"}]}]}
    r = call(srv, "run_task", task="save the document", demo=demo)
    sc = r["structuredContent"]
    assert sc["status"] == "done" and sc["receipts"][-1]["verdict"] == "verified_done"
    notes = [json.loads(x) for x in srv.out.getvalue().splitlines()]
    assert any(n.get("method") == "notifications/message" for n in notes)     # 流式轨迹事件
    no_model = call(srv, "run_task", task="x")
    assert no_model["isError"] and "no models" in no_model["content"][0]["text"]


@pytest.mark.web
def test_stdio_subprocess_against_real_chromium(tmp_path):
    pytest.importorskip("playwright.sync_api")
    page = tmp_path / "p.html"
    page.write_text("<title>Form</title><label><input type=checkbox id=c "
                    "onchange=\"s.textContent='on='+this.checked\">Agree</label><p id=s>on=false</p>",
                    encoding="utf-8")
    env = dict(os.environ, NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    p = subprocess.Popen([sys.executable, "-m", "gua.cli", "mcp", "--platform", "web", "--url", str(page)],
                         cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, env=env)
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "observe",
                                                                          "arguments": {"include_image": False}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "act", "arguments": {
                "action": {"type": "invoke", "method": "toggle", "target": "Agree",
                           "expect": [{"kind": "text_appears", "text": "on=true"}]}}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "verify",
                                                                          "arguments": {"expect_text": "on=true"}}}]
    try:
        out, err = p.communicate("\n".join(json.dumps(m) for m in msgs) + "\n", timeout=90)
    except subprocess.TimeoutExpired:
        p.kill()
        raise
    if "Executable doesn't exist" in err:
        pytest.skip("Chromium not installed")
    replies = {r["id"]: r for r in map(json.loads, out.splitlines()) if "id" in r}
    assert replies[1]["result"]["serverInfo"]["name"] == "gua", err[-2000:]
    assert "checkbox 'Agree'" in replies[2]["result"]["content"][0]["text"]
    act = replies[3]["result"]["structuredContent"]
    assert act["verdict"] == "success" and act["route"] == "dom_semantic:toggle", act
    assert replies[4]["result"]["structuredContent"]["verdict"] == "verified_done"

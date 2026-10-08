"""v0.7 原生 computer-use 适配器：本地 HTTP 替身（不调用真实 API）上的端到端测试。

GUIAgent（规划 → 动作 → 安全闸门 → 执行 → 验证）+ MockEnv + ClaudeComputerActor / OpenAIComputerActor。
替身记录每个请求体，用来核对协议细节：toolset_name 回显、批量“停止于首个失败”的固定文本、zoom 返回原始分辨率裁剪、
修饰键展开、截图批量裁剪、OpenAI pending_safety_checks 不自动确认等。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import fast
from gua.config import build_agent, load_config
from gua.env.mock import MockButton, MockEnv
from gua.llm.anthropic import AnthropicLLM
from gua.llm.base import ScriptedLLM
from gua.llm.cua import HALT_TEXT, TOOLSET, ClaudeComputerActor

PLAN = json.dumps({"subgoals": [{"goal": "enter Alice in Name and save", "expected": "saved",
                                 "expect_text": "saved=True"}]})


class FakeAPI:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        api = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                api.requests.append(body)
                api.headers.append(dict(self.headers))
                reply = api.replies.pop(0) if api.replies else {"content": [{"type": "text", "text": "FAIL: script"}]}
                data = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def api_factory():
    made = []

    def make(replies):
        a = FakeAPI(replies)
        made.append(a)
        return a
    yield make
    for a in made:
        a.close()


def form_env():
    def save(env):
        name = next(b for b in env.buttons if b.name == "Name")
        env.state["saved"] = name.value == "Alice"
    return fast(MockEnv(title="Mock Form", buttons=[
        MockButton("Name", (100, 100, 300, 140), role="textbox", value=""),
        MockButton("Save", (100, 200, 220, 250), on_click=save)]))


def tu(i, name, **inp):
    return {"type": "tool_use", "id": f"toolu_{i}", "name": name, "toolset_name": "computer", "input": inp}


def claude_agent(env, api, cfg_extra=None, version=TOOLSET):
    cfg = load_config("configs/mock.yaml", {"actor": {"kind": "claude_toolset", "tool_version": version},
                                            "agent": {"max_steps": 20}, **(cfg_extra or {})})
    llm = AnthropicLLM(model="claude-test", base_url=api.url, api_key_env="NO_SUCH_KEY")
    return build_agent(cfg, env, None, llms={"planner": ScriptedLLM(fn=lambda s, t, i: PLAN), "actor": llm,
                                             "verifier": None, "reflector": None, "grounder": None})


def test_claude_toolset_batch_runs_each_action_through_the_verified_loop(api_factory):
    env = form_env()
    api = api_factory([
        {"content": [{"type": "text", "text": "Fill the form."}, tu(1, "zoom", region=[90, 90, 310, 150]),
                     tu(2, "left_click", coordinate=[200, 120]), tu(3, "type", text="Alice"),
                     tu(4, "screenshot")], "usage": {"input_tokens": 1000, "output_tokens": 50}},
        {"content": [tu(5, "left_click", coordinate=[160, 225]), tu(6, "screenshot")],
         "usage": {"input_tokens": 1200, "output_tokens": 30}},
        {"content": [{"type": "text", "text": "DONE"}], "usage": {"input_tokens": 1300, "output_tokens": 5}}])
    agent = claude_agent(env, api)
    res = agent.run("enter Alice in Name and save")
    assert res.status == "done" and env.state.get("saved") is True
    first = api.requests[0]
    assert first["tools"] == [{"type": TOOLSET, "configs": {m: {"enabled": False} for m in
                                                          ["hold_key", "left_mouse_down", "left_mouse_up",
                                                           "middle_click", "triple_click"]}}]
    assert "anthropic-beta" not in {k.lower() for k in api.headers[0]}
    results = api.requests[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["toolu_1", "toolu_2", "toolu_3", "toolu_4"]
    assert all(r["toolset_name"] == "computer" and not r.get("is_error") for r in results)
    zoom = results[0]["content"][0]
    assert zoom["type"] == "image"                       # full-resolution crop returned to the model
    assert results[-1]["content"][-1]["type"] == "image"  # current screen attached to the last result
    assert res.budget.calls >= 3 and res.budget.prompt_tokens >= 3500
    assert agent.actor.stats["internal"] >= 3


def test_claude_batch_stops_at_first_failure_with_the_documented_halt_text(api_factory):
    env = form_env()
    api = api_factory([
        {"content": [tu(1, "left_click", coordinate=[5000, 5000]), tu(2, "type", text="never typed"),
                     tu(3, "screenshot")]},
        {"content": [{"type": "text", "text": "FAIL: cannot"}]}])
    agent = claude_agent(env, api)
    agent.run("enter Alice in Name and save")
    results = api.requests[1]["messages"][-1]["content"]
    by_id = {r["tool_use_id"]: r for r in results if r.get("type") == "tool_result"}
    assert by_id["toolu_1"]["is_error"]
    for later in ("toolu_2", "toolu_3"):
        assert by_id[later]["is_error"] and by_id[later]["content"][0]["text"] == HALT_TEXT
    assert "never typed" not in json.dumps(env.state)


def test_claude_modifier_click_is_expanded_and_key_released(api_factory):
    env = form_env()
    api = api_factory([{"content": [tu(1, "left_click", coordinate=[160, 225], text="shift")]},
                       {"content": [{"type": "text", "text": "FAIL: stop"}]}])
    claude_agent(env, api).run("enter Alice in Name and save")
    seq = [json.loads(line)["type"] for line in env.log]
    assert seq[:3] == ["key_down", "click", "key_up"], env.log


def test_claude_unsupported_member_is_refused_without_execution(api_factory):
    env = form_env()
    api = api_factory([{"content": [tu(1, "triple_click", coordinate=[200, 120])]},
                       {"content": [{"type": "text", "text": "FAIL: stop"}]}])
    claude_agent(env, api).run("enter Alice in Name and save")
    assert not [line for line in env.log if json.loads(line)["type"] in {"click", "double_click"}]
    r = api.requests[1]["messages"][-1]["content"][0]
    assert r["is_error"] and "not supported" in r["content"][0]["text"]


def test_claude_legacy_20251124_uses_beta_header_zoom_flag_and_action_field(api_factory):
    env = form_env()
    api = api_factory([{"content": [{"type": "tool_use", "id": "t1", "name": "computer",
                                     "input": {"action": "left_click", "coordinate": [200, 120]}}]},
                       {"content": [{"type": "text", "text": "FAIL: stop"}]}])
    claude_agent(env, api, version="computer_20251124").run("enter Alice in Name and save")
    tool = api.requests[0]["tools"][0]
    assert tool["type"] == "computer_20251124" and tool["enable_zoom"] is True and tool["name"] == "computer"
    assert {k.lower(): v for k, v in api.headers[0].items()}.get("anthropic-beta") == "computer-use-2025-11-24"
    r = api.requests[1]["messages"][-1]["content"][0]
    assert r["tool_use_id"] == "t1" and "toolset_name" not in r


def test_screenshot_history_is_pruned_in_batches():
    actor = ClaudeComputerActor(llm=type("L", (), {"model": "m", "max_tokens": 10})(), keep_images=2, prune_every=3)
    img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "x"}}
    actor.messages = [{"role": "user", "content": [dict(img)]} for _ in range(5)]
    actor._prune()
    assert actor.stats["pruned_images"] == 0              # 5 <= 2 + 3: prefix stays byte-identical
    actor.messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a",
                                                        "content": [dict(img)]}]})
    actor._prune()
    kinds = [b.get("type") for m in actor.messages for b in m["content"] if b.get("type") != "tool_result"]
    assert actor.stats["pruned_images"] == 4 and kinds.count("image") == 1


# ---------------------------------------------------------------- OpenAI
def oai_agent(env, api, mode="deny", confirm=None):
    cfg = load_config("configs/mock.yaml", {"actor": {"kind": "openai_computer"}, "safety": {"mode": mode},
                                            "agent": {"max_steps": 20},
                                            "models": {"actor": {"provider": "openai", "model": "gpt-test",
                                                                 "base_url": api.url, "api_key_env": "NO_KEY"}}})
    return build_agent(cfg, env, None, llms={"planner": ScriptedLLM(fn=lambda s, t, i: PLAN), "verifier": None,
                                             "reflector": None, "grounder": None},
                       confirm_fn=confirm)


def call(cid, actions, checks=None):
    return {"type": "computer_call", "call_id": cid, "actions": actions, "status": "completed",
            "pending_safety_checks": checks or []}


def test_openai_computer_batch_and_screenshot_only_call(api_factory):
    env = form_env()
    api = api_factory([
        {"id": "r1", "output": [call("c1", [{"type": "screenshot"}])]},
        {"id": "r2", "output": [call("c2", [{"type": "click", "button": "left", "x": 200, "y": 120},
                                            {"type": "type", "text": "Alice"},
                                            {"type": "click", "button": "left", "x": 160, "y": 225}])],
         "usage": {"input_tokens": 900, "output_tokens": 40}},
        {"id": "r3", "output": [{"type": "message", "content": [{"type": "output_text", "text": "DONE"}]}]}])
    res = oai_agent(env, api).run("enter Alice in Name and save")
    assert res.status == "done" and env.state.get("saved") is True
    assert api.requests[0]["tools"] == [{"type": "computer"}]
    second = api.requests[1]
    assert second["previous_response_id"] == "r1"
    out = second["input"][0]
    assert out["type"] == "computer_call_output" and out["call_id"] == "c1"
    assert out["output"]["type"] == "computer_screenshot" and "acknowledged_safety_checks" not in out
    assert api.requests[2]["input"][0]["call_id"] == "c2"


def test_openai_pending_safety_check_is_never_auto_acknowledged(api_factory):
    env = form_env()
    check = {"id": "sc1", "code": "malicious_instructions", "message": "page asks to do something else"}
    api = api_factory([
        {"id": "r1", "output": [call("c1", [{"type": "click", "button": "left", "x": 160, "y": 225}], [check])]},
        {"id": "r2", "output": [{"type": "message", "content": [{"type": "output_text", "text": "FAIL: declined"}]}]}])
    res = oai_agent(env, api, mode="deny").run("enter Alice in Name and save")
    assert res.status != "done" and not env.state.get("saved")
    assert "acknowledged_safety_checks" not in json.dumps(api.requests)
    assert "previous_response_id" not in api.requests[1]     # declined: the chain restarts
    assert any("provider safety check" in (e.get("reason") or "") for e in res.safety_events)


def test_openai_safety_check_acknowledged_only_after_human_confirmation(api_factory):
    env = form_env()
    check = {"id": "sc1", "code": "irrelevant_domain", "message": "domain differs"}
    asked = []
    api = api_factory([
        {"id": "r1", "output": [call("c1", [{"type": "click", "button": "left", "x": 200, "y": 120}], [check])]},
        {"id": "r2", "output": [{"type": "message", "content": [{"type": "output_text", "text": "FAIL: stop"}]}]}])
    oai_agent(env, api, mode="confirm", confirm=lambda a, why: asked.append(why) or True).run("t")
    assert asked and "provider safety check" in asked[0]
    out = api.requests[1]["input"][0]
    assert out["acknowledged_safety_checks"] == [check] and api.requests[1]["previous_response_id"] == "r1"

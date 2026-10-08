"""v0.7 审查回归：每个用例都在 f195cdb（v0.6 + 用户三个提交）上失败、在修订后通过。

覆盖：完成核验的文字证据（Unsaved/Not saved 误判）、后置条件未知时不能被文字覆盖、收尾核验重查后置条件、
修饰键点击被静默丢弃、Claude triple/middle click 被换成别的动作、沙箱守护进程的人工交还令牌 / 应用白名单 /
凭据泄漏给子进程 / 畸形请求、MCP 协议细节、本机沙箱冷启动竞态。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from PIL import Image

from gua.actions import Action, ActionParseError, parse_action
from gua.env.base import Observation, UIElement
from gua.planner import Subgoal
from gua.verify import Verdict, Verifier

ROOT = Path(__file__).resolve().parents[1]
DAEMON = ROOT / "gua" / "sandbox" / "daemon.py"


def obs(text="", elements=(), window="App"):
    return Observation(Image.new("RGB", (200, 100), "white"), time.time(), (200, 100), active_window=window,
                       elements=list(elements), text=text, platform="mock", focus_state="none")


# ---------------------------------------------------------------- 完成核验
@pytest.mark.parametrize("screen", ["You have Unsaved changes", "Not saved", "Document not saved yet",
                                    "Failed to save", "未保存"])
def test_goal_text_evidence_rejects_negated_or_partial_words(screen):
    v = Verifier(None)
    expect = "保存" if screen == "未保存" else ("save" if screen == "Failed to save" else "saved")
    c = v.check_goal(obs(screen), "save the document", "", expect, stable=True, baseline=obs("editor"))
    assert c.verdict != Verdict.SUCCESS, c.evidence


def test_goal_text_evidence_still_accepts_real_evidence():
    v = Verifier(None)
    c = v.check_goal(obs("Document saved."), "save", "", "saved", stable=True, baseline=obs("editor"))
    assert c.verdict == Verdict.SUCCESS
    c = v.check_goal(obs("保存成功"), "save", "", "保存成功", stable=True, baseline=obs("编辑器"))
    assert c.verdict == Verdict.SUCCESS


def test_final_check_rejects_unsaved_as_saved():
    v = Verifier(None)
    sg = Subgoal(1, "save", "saved", expect_text="saved")
    c = v.check_final(obs("Unsaved changes"), "save the file", [sg], stable=True, baseline=obs("editor"))
    assert c.verdict == Verdict.FAILED


def test_unknown_postcondition_is_not_overridden_by_visible_text():
    v = Verifier(None)
    two = [UIElement(1, "Agree", "checkbox", (0, 0, 10, 10), checked=True),
           UIElement(2, "Agree", "checkbox", (0, 20, 10, 30), checked=False)]
    c = v.check_goal(obs("Settings applied", two), "agree", "", "Settings applied", stable=True,
                     baseline=obs("form"),
                     postconditions=[{"kind": "element_state", "name": "Agree", "role": "checkbox", "checked": True}])
    assert c.verdict != Verdict.SUCCESS, c.evidence


def test_final_check_rechecks_postconditions_of_subgoals_with_expect_text():
    v = Verifier(None)
    sg = Subgoal(1, "tick agree", "ticked", expect_text="Preferences",
                 postconditions=[{"kind": "element_state", "name": "Agree", "checked": True}])
    now = obs("Preferences page", [UIElement(1, "Agree", "checkbox", (0, 0, 10, 10), checked=False)])
    c = v.check_final(now, "tick agree", [sg], stable=True, baseline=obs("start"))
    assert c.verdict == Verdict.FAILED


# ---------------------------------------------------------------- 动作语义
def test_modifier_keys_on_pointer_actions_are_refused_not_dropped():
    with pytest.raises(ActionParseError):
        parse_action({"type": "click", "x": 5, "y": 5, "keys": ["shift"]}).validate()
    with pytest.raises(ActionParseError):
        Action("click", x=1, y=1, keys=["ctrl"]).validate()
    Action("hotkey", keys=["ctrl", "s"]).validate()


@pytest.mark.parametrize("inp", [{"action": "triple_click", "coordinate": [10, 10]},
                                 {"action": "middle_click", "coordinate": [10, 10]},
                                 {"action": "left_click", "coordinate": [10, 10], "text": "shift"},
                                 {"action": "hold_key", "text": "shift", "duration": 2}])
def test_claude_legacy_adapter_refuses_actions_it_cannot_execute_faithfully(inp):
    from gua.llm.anthropic import tool_input_to_action
    with pytest.raises(ActionParseError):
        tool_input_to_action(inp)


# ---------------------------------------------------------------- 沙箱守护进程（不需要 X）
def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def daemon(tmp_path):
    port = _free_port()
    work = tmp_path / "work"
    work.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.upper().endswith("_PROXY")}
    env.update(GUA_SANDBOX_TOKEN="agent-token-123", GUA_SANDBOX_CONTROL_TOKEN="human-token-456")
    p = subprocess.Popen([sys.executable, str(DAEMON), "--port", str(port), "--workdir", str(work),
                          "--display", ":99", "--shell", "--apps", "gua-form", "/bin/sh"],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(100):
        try:
            with opener.open(url + "/health", timeout=1):
                break
        except OSError:
            time.sleep(0.05)

    def call(method, path, body=None, token="agent-token-123", headers=None):
        req = urllib.request.Request(url + path, method=method,
                                     data=json.dumps(body or {}).encode() if method == "POST" else None,
                                     headers={"X-Gua-Token": token, "Content-Type": "application/json",
                                              **(headers or {})})
        try:
            with opener.open(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    call.work = work
    call.url = url
    yield call
    p.terminate()
    p.wait(timeout=5)


def test_agent_token_cannot_hand_control_back_to_itself(daemon):
    assert daemon("POST", "/takeover")[1]["ok"]
    code, r = daemon("POST", "/handback")
    assert code == 403 and not r["ok"]
    assert daemon("GET", "/health")[1]["takeover"] is True
    code, r = daemon("POST", "/handback", headers={"X-Gua-Control-Token": "human-token-456"})
    assert code == 200 and r["ok"]


def test_launch_allowlist_matches_exact_names_not_basenames(daemon):
    fake = daemon.work / "gua-form"
    fake.write_text("#!/bin/sh\ntouch pwned\n")
    fake.chmod(0o755)
    for argv in (["./gua-form"], [str(fake)]):
        r = daemon("POST", "/launch", {"argv": argv})[1]
        assert not r["ok"] and "blocked_by_safety" in r["error"], argv
    time.sleep(0.3)
    assert not (daemon.work / "pwned").exists()


def test_launched_apps_do_not_inherit_sandbox_credentials(daemon):
    r = daemon("POST", "/launch", {"argv": ["/bin/sh", "-c", "env > leaked.txt"]})[1]
    assert r["ok"], r
    for _ in range(50):
        if (daemon.work / "leaked.txt").exists() and (daemon.work / "leaked.txt").stat().st_size:
            break
        time.sleep(0.05)
    leaked = (daemon.work / "leaked.txt").read_text()
    assert "agent-token-123" not in leaked and "human-token-456" not in leaked


def test_malformed_requests_get_a_json_error_instead_of_a_dropped_connection(daemon):
    code, r = daemon("POST", "/input", {"type": "click", "x": "abc", "y": 1})
    assert code == 400 and not r["ok"]
    code, r = daemon("POST", "/shell", {"argv": ["true"], "timeout": -5})
    assert not r["ok"] and "invalid_argument" in r["error"]
    (daemon.work / "adir").mkdir()
    code, r = daemon("POST", "/files", {"method": "write", "path": "adir", "text": "x"})
    assert not r["ok"] and "tool_error" in r["error"]


def test_local_sandbox_passes_credentials_by_environment_not_argv():
    src = (ROOT / "gua" / "sandbox" / "local.py").read_text(encoding="utf-8")
    assert '"--token", self.token' not in src
    from gua.sandbox.local import LocalSandbox
    box = LocalSandbox()
    assert box.control_token and box.control_token != box.token


# ---------------------------------------------------------------- 冷启动竞态
class _Proc:
    def __init__(self, alive):
        self.alive = alive

    def poll(self):
        return None if self.alive else 1


def test_window_manager_waits_for_x_and_restarts_once_if_it_exited():
    from gua.sandbox.local import start_display_and_wm
    state = {"x_ready_after": 3, "probes": 0, "spawned": []}

    def probe(argv):
        state["probes"] += 1
        if argv[1] == "getdisplaygeometry":
            return state["probes"] > state["x_ready_after"]
        return bool(state["spawned"]) and state["spawned"][-1].alive

    def spawn(argv):
        # An openbox started before X accepts clients would exit immediately.
        p = _Proc(alive=len(state["spawned"]) >= 1)
        state["spawned"].append(p)
        return p
    start_display_and_wm(spawn, probe, time.monotonic() + 5, sleep=lambda s: None)
    assert len(state["spawned"]) == 2


def test_window_manager_gives_up_after_bounded_restarts():
    from gua.sandbox.local import start_display_and_wm
    spawned = []
    with pytest.raises(RuntimeError):
        start_display_and_wm(lambda argv: spawned.append(_Proc(False)) or spawned[-1],
                             lambda argv: argv[1] == "getdisplaygeometry", time.monotonic() + 5,
                             sleep=lambda s: None)
    assert len(spawned) == 3


# ---------------------------------------------------------------- MCP 协议
def _server(tmp_path):
    import io
    from gua.config import load_config
    from gua.env.mock import MockEnv
    from gua.mcp_server import Server, Session
    cfg = load_config(str(ROOT / "configs" / "mock.yaml"))
    cfg["runs_dir"] = str(tmp_path)
    return Server(Session(cfg, env=MockEnv()), out=io.StringIO())


def test_mcp_protocol_errors_are_answered(tmp_path):
    srv = _server(tmp_path)
    r = srv.handle({"jsonrpc": "2.0", "id": 7, "params": {}})
    assert r["error"]["code"] == -32600
    r = srv.handle({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "nope"}})
    assert r["error"]["code"] == -32602
    r = srv.handle({"jsonrpc": "2.0", "id": 9, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
    assert r["result"]["protocolVersion"] == "2025-06-18"
    names = {t["name"] for t in srv.handle({"jsonrpc": "2.0", "id": 10, "method": "tools/list"})["result"]["tools"]}
    assert "handback" not in names


def test_mcp_run_task_max_steps_does_not_leak_into_the_session(tmp_path, monkeypatch):
    import gua.config as config
    srv = _server(tmp_path)
    seen = []

    class Res:
        status, claimed_done, steps, message, receipts, modality = "fail", False, 0, "", [], {}

    class Stub:
        def __init__(self, cfg):
            seen.append(cfg)
            from gua.safety import SafetyGuard
            from gua.sensitive import Scrubber
            self.guard = SafetyGuard()
            self.hybrid = type("H", (), {"tools": None})()
            self.actor = object()
            self.scrubber = Scrubber()

        def run(self, task):
            return Res()
    monkeypatch.setattr(config, "build_agent", lambda cfg, *a, **k: Stub(cfg))
    before = dict(srv.s.cfg.get("agent") or {})
    srv.s.run_task("t", demo={"subgoals": []}, max_steps=3)
    assert seen[0]["agent"]["max_steps"] == 3
    assert (srv.s.cfg.get("agent") or {}) == before


# ---------------------------------------------------------------- Windows 中文输入法
def test_windows_ascii_typing_bypasses_the_ime(monkeypatch):
    """pyautogui.write 发虚拟键，中文输入法处于中文模式时会把 abc 变成拼音候选；v0.7 改用 KEYEVENTF_UNICODE。"""
    import sys
    from fakes import fake_pyautogui
    from gua.env.desktop import KEYEVENTF_UNICODE, clipboard_type, unicode_key_events
    pg = fake_pyautogui()
    monkeypatch.setitem(sys.modules, "pyautogui", pg)
    sent = []
    clipboard_type("ab", "windows", unicode_sender=sent.append)
    assert not [c for c in pg.calls if c[0] == "write"]
    assert [e[1] for batch in sent for e in batch if not e[2] & 2] == [ord("a"), ord("b")]
    assert all(e[2] & KEYEVENTF_UNICODE for batch in sent for e in batch)
    emoji = unicode_key_events("😀")
    assert len(emoji) == 4 and {e[1] for e in emoji} == {0xD83D, 0xDE00}
    assert unicode_key_events("\n")[0][0] == 0x0D


def test_coordinate_click_on_a_text_field_is_not_an_unrepeatable_activation():
    """视觉 / computer-use actor 只给坐标：v0.6 把“点文本框聚焦但看不出变化”记成未确认激活，之后再点同一字段被闸门拒绝。"""
    from gua.hybrid import uncertain_activation
    o = obs("form", [UIElement(1, "Name", "textbox", (100, 100, 300, 140)),
                     UIElement(2, "Pay", "button", (100, 200, 220, 250))])
    assert not uncertain_activation(Action("click", x=200, y=120), o)
    assert uncertain_activation(Action("click", x=160, y=225), o)
    assert uncertain_activation(Action("click", x=5, y=5), o)

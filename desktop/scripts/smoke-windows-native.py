"""Windows-only real WinForms/UIA harness; checks app-owned state, no LLM/API.

The fixture and agent run in the same logged-in desktop session. Failure to
activate or discover it is a failure, not a passing mock test.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

FIXTURE = r"""
param([string]$Title, [string]$StateFile)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object System.Windows.Forms.Form
$form.Text = $Title
$form.Size = New-Object System.Drawing.Size(540, 360)
$form.StartPosition = 'CenterScreen'
$form.TopMost = $true
$script:clicks = 0
$name = New-Object System.Windows.Forms.TextBox
$name.AccessibleName = 'Name'
$name.Location = New-Object System.Drawing.Point(30, 30)
$name.Width = 320
$check = New-Object System.Windows.Forms.CheckBox
$check.Text = 'Subscribe'
$check.Location = New-Object System.Drawing.Point(30, 80)
$check.Width = 200
$radio = New-Object System.Windows.Forms.RadioButton
$radio.Text = 'Pro'
$radio.Location = New-Object System.Drawing.Point(30, 130)
$free = New-Object System.Windows.Forms.RadioButton
$free.Text = 'Free'
$free.Location = New-Object System.Drawing.Point(180, 130)
$button = New-Object System.Windows.Forms.Button
$button.Text = 'Continue'
$button.Location = New-Object System.Drawing.Point(30, 180)
$button.Width = 140
$status = New-Object System.Windows.Forms.Label
$status.Location = New-Object System.Drawing.Point(250, 180)
$status.Width = 180
$status.Text = 'Clicks:0'
function Record-State {
  @{name=$name.Text; checked=$check.Checked; selected=$radio.Checked; clicks=$script:clicks} |
    ConvertTo-Json -Compress | Set-Content -Path $StateFile -Encoding UTF8
}
$button.Add_Click({ $script:clicks++; $status.Text = 'Clicks:' + $script:clicks; Record-State })
$name.Add_TextChanged({ Record-State })
$check.Add_CheckedChanged({ Record-State })
$radio.Add_CheckedChanged({ Record-State })
$free.Add_CheckedChanged({ Record-State })
$form.Controls.AddRange(@($name, $check, $radio, $free, $button, $status))
$form.Add_Shown({
  $form.Activate(); $name.Focus()
  [System.Windows.Forms.Cursor]::Position = New-Object System.Drawing.Point(($form.Left + 250), ($form.Top + 250))
  Record-State
})
[void]$form.ShowDialog()
"""


def check_native():
    if sys.platform != "win32":
        raise RuntimeError("This harness requires a real Windows desktop")
    from gua.actions import Action
    from gua.config import build_agent, load_config
    from gua.env.windows import WindowsEnv
    from gua.scripted import ScriptedPolicy
    root = Path(__file__).resolve().parents[2]
    title = "GUI Agent Native Test " + uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix="gui-agent-native-") as directory:
        fixture, state = Path(directory) / "fixture.ps1", Path(directory) / "state.json"
        fixture.write_text(FIXTURE, encoding="utf-8-sig")
        fixture_log = Path(directory) / "fixture.log"
        output = fixture_log.open("wb")
        process = subprocess.Popen(["powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
                                    "-File", str(fixture), "-Title", title, "-StateFile", str(state)],
                                   stdout=output, stderr=subprocess.STDOUT)
        env = None
        def wait_for(predicate, timeout=10):
            deadline = time.monotonic() + timeout
            last = {}
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    diagnostic = fixture_log.read_text(encoding="utf-8", errors="replace")[-4000:]
                    raise RuntimeError(f"Native fixture exited before verification: {diagnostic}")
                try:
                    last = json.loads(state.read_text(encoding="utf-8-sig"))
                    if predicate(last): return last
                except (OSError, ValueError):
                    pass
                time.sleep(0.05)
            diagnostic = fixture_log.read_text(encoding="utf-8", errors="replace")[-4000:]
            raise AssertionError(f"Native application outcome did not arrive: {last}; fixture output: {diagnostic}")
        try:
            # Cold Windows/.NET GUI startup is distinct from action settling.
            # Preserve the shorter deadlines and exact outcome assertions below.
            wait_for(lambda row: "clicks" in row, timeout=45)
            env = WindowsEnv()
            assert env.focus_window(title), "Cannot activate the native test window"
            cfg = load_config(root / "configs/default.yaml", {"env": {"platform": "windows"},
                "safety": {"mode": "deny"}, "reflection": {"enabled": False}, "verification": {"llm": False}})
            agent = build_agent(cfg, env, llms=ScriptedPolicy({}).llms(), task_window=title)
            input_routes = []
            for text in ("GUI Agent", "GUI Agent 测试用户"):
                obs = agent._observe()
                assert title in obs.active_window, obs.active_window
                action, source = agent._resolve(Action("type", target="Name", text=text, clear=True), obs)
                assert source != "grounding_failed", "Native textbox is missing from UIA"
                result = agent._execute_gated(action, obs)
                assert result.ok and result.route == "uia_value", (result, text)
                wait_for(lambda row: row.get("name") == text)
                input_routes.append(result.route)
            # Exercise real keyboard replacement on the same Win32 Edit
            # (which ignores Ctrl+A), simulating a missing ValuePattern only.
            class KeyboardOnlyControl:
                def __init__(self, control): self.control = control
                def GetValuePattern(self): return None
                def __getattr__(self, name): return getattr(self.control, name)
            obs = agent._observe()
            focused = next(e for e in obs.elements if e.focused and e.name == "Name")
            key = focused.attrs["uia_key"]
            control, identity = env._controls[key]
            env._controls[key] = (KeyboardOnlyControl(control), identity)
            text = "GUI Agent Keyboard 测试用户"
            result = agent._execute_gated(Action("type", text=text, clear=True), obs)
            assert result.ok and result.route == "windows_input", result
            wait_for(lambda row: row.get("name") == text)
            input_routes.append(result.route)
            routes, timings = [], {}
            for name, route in [("Subscribe", "uia_toggle"), ("Pro", "uia_select"), ("Continue", "uia_invoke")]:
                obs = agent._observe()
                action, _ = agent._resolve(Action("click", target=name), obs)
                started = time.perf_counter()
                result = agent._execute_gated(action, obs)
                timings[route] = round(time.perf_counter() - started, 4)
                assert result.ok and result.route == route, (result, name)
                routes.append(result.route)
            actual = wait_for(lambda row: row.get("clicks") == 1 and row.get("checked") and row.get("selected"))
            assert actual["name"] == text and actual["clicks"] == 1
            # v0.6 explicit background patterns: app-owned outcomes plus exact
            # pointer/foreground invariants. No substitute mocked controls.
            background_routes = []
            for name, method, payload, predicate in [
                ("Name", "set_value", "Background 测试用户", lambda row: row.get("name") == "Background 测试用户"),
                ("Subscribe", "toggle", None, lambda row: row.get("checked") is False),
                ("Free", "select", None, lambda row: row.get("selected") is False),
                ("Pro", "select", None, lambda row: row.get("selected") is True),
                ("Continue", "invoke", None, lambda row: row.get("clicks") == 2),
            ]:
                obs = agent._observe()
                action, source = agent._resolve(Action("invoke", method=method, target=name, text=payload,
                                                       dispatch="background", expect=[{"kind": "text_appears", "text": "Clicks:2"}]
                                                       if method == "invoke" else []), obs)
                assert source != "grounding_failed", (name, method)
                pointer, foreground = env.pointer_position(), env.foreground_token()
                assert pointer is not None and foreground, "Cannot inspect native input invariants"
                result = agent._execute_gated(action, obs)
                assert result.ok and result.route == "uia_semantic:" + method, result
                wait_for(predicate)
                assert env.pointer_position() == pointer, ("Background pointer intrusion", method)
                assert env.foreground_token() == foreground, ("Background foreground intrusion", method)
                background_routes.append(result.route)
            print("Real Windows WinForms/UIA outcomes verified: native ASCII + Chinese replacement, keyboard fallback, toggle, select, invoke.")
            print(json.dumps({"input_routes": input_routes, "routes": routes, "background_routes": background_routes,
                              "native_action_seconds": timings,
                              "outcome": wait_for(lambda row: row.get("clicks") == 2)}, ensure_ascii=True))
        finally:
            if env is not None:
                env.close()
            process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
            output.close()


if __name__ == "__main__":
    from gua.app_native import configure_com_cache
    with tempfile.TemporaryDirectory(prefix="gua-uia-cache-") as cache:
        configure_com_cache(Path(cache))
        check_native()

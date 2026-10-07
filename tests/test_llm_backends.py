"""模型后端：请求构造与响应映射（离线，不发网络请求）。"""
import base64

from PIL import Image

from gua.config import load_config
from gua.llm import make_llm
from gua.llm.anthropic import (COMPUTER_BETA, AnthropicLLM, ClaudeComputerUseActor, scaling_target,
                               tool_input_to_action)
from gua.planner import Subgoal, UITarsActor
from gua.env.base import Observation
from gua.llm.base import ScriptedLLM


def test_anthropic_payload():
    llm = AnthropicLLM(model="claude-x", image_max_side=100)
    p = llm.build_payload("sys", "hello", [Image.new("RGB", (400, 200))])
    c = p["messages"][0]["content"]
    assert c[0]["type"] == "image" and c[0]["source"]["media_type"] == "image/png"
    img = Image.open(__import__("io").BytesIO(base64.b64decode(c[0]["source"]["data"])))
    assert max(img.size) == 100
    assert c[1] == {"type": "text", "text": "hello"} and p["system"] == "sys"
    h = llm.headers([COMPUTER_BETA])
    assert h["anthropic-version"] and h["anthropic-beta"] == COMPUTER_BETA


def test_scaling_target_matches_reference_impl():
    assert scaling_target(1920, 1080) == (1366, 768)
    assert scaling_target(2560, 1600) == (1280, 800)
    assert scaling_target(1024, 768) == (1024, 768)
    assert scaling_target(1000, 1000) == (1000, 1000)


def test_tool_input_mapping():
    a = tool_input_to_action({"action": "left_click", "coordinate": [100, 50]}, 2.0, 2.0)
    assert a.type == "click" and (a.x, a.y) == (200, 100)
    assert tool_input_to_action({"action": "key", "text": "ctrl+Return"}).keys == ["ctrl", "enter"]
    d = tool_input_to_action({"action": "left_click_drag", "start_coordinate": [1, 2], "coordinate": [3, 4]})
    assert (d.x, d.y, d.x2, d.y2) == (1, 2, 3, 4)
    s = tool_input_to_action({"action": "scroll", "coordinate": [5, 5], "scroll_direction": "up", "scroll_amount": 2})
    assert s.direction == "up" and s.amount == 2
    assert tool_input_to_action({"action": "screenshot"}).type == "wait"
    assert tool_input_to_action({"action": "type", "text": "hi"}).text == "hi"


def test_computer_use_actor_request_and_parse():
    actor = ClaudeComputerUseActor(AnthropicLLM(), "linux")
    o = Observation(Image.new("RGB", (1920, 1080)), 0, (1920, 1080))
    payload, scale = actor.build_request("task", Subgoal(1, "goal"), 1, o, "(none)", "(none)")
    tool = payload["tools"][0]
    assert tool["type"] == "computer_20250124" and (tool["display_width_px"], tool["display_height_px"]) == (1366, 768)
    resp = {"content": [{"type": "text", "text": "clicking"},
                        {"type": "tool_use", "name": "computer", "input": {"action": "left_click", "coordinate": [683, 384]}}]}
    a, th = actor.parse_response(resp, scale)
    assert a.type == "click" and abs(a.x - 960) <= 1 and abs(a.y - 540) <= 1 and th == "clicking"
    assert actor.parse_response({"content": [{"type": "text", "text": "DONE"}]}, scale)[0].type == "done"
    assert actor.parse_response({"content": [{"type": "text", "text": "FAIL: no app"}]}, scale)[0].type == "fail"


def test_uitars_actor_end_to_end_parse():
    llm = ScriptedLLM(["Thought: 点击\nAction: click(start_box='<|box_start|>(500,500)<|box_end|>')"])
    a, _ = UITarsActor(llm, "windows", coord_space="norm1000").next_action(
        "t", Subgoal(1, "g"), 1, Observation(Image.new("RGB", (100, 100)), 0, (100, 100)), "", "")
    assert a.coord_space == "norm1000" and a.x == 500
    assert "press_home()" in __import__("gua.planner", fromlist=["x"]).UITARS_MOBILE


def test_make_llm_openai_compat_offline():
    llm = make_llm({"provider": "openai", "model": "qwen2.5-vl-72b-instruct",
                    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"}, "planner")
    msgs = llm.build_messages("s", "t", [Image.new("RGB", (10, 10))])
    assert msgs[1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert make_llm({"provider": "anthropic", "model": "claude-x"}, "planner").model == "claude-x"


def test_all_configs_load():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "configs"
    for f in list(root.glob("*.yaml")) + list((root / "ablations").glob("*.yaml")):
        cfg = load_config(f)
        assert "models" in cfg and "agent" in cfg, f
    for f in (root / "models").glob("*.yaml"):
        cfg = load_config(root / "default.yaml", extra_files=[f])
        assert cfg["models"]["planner"]["model"], f
    raw = load_config(root / "ablations" / "raw_loop.yaml")
    assert raw["verification"]["trigger"] == "none" and raw["env"]["max_elements"] == 150
    claude = load_config(root / "default.yaml", extra_files=[root / "models" / "claude.yaml"])
    assert claude["models"]["planner"]["provider"] == "anthropic" and claude["models"]["grounder"] is None

"""安全守卫、轨迹日志 + HTML 回放、CLI。"""
import json

from PIL import Image

from gua.actions import Action
from gua.cli import main
from gua.env.base import Observation, UIElement
from gua.logger import TrajectoryLogger
from gua.safety import SafetyGuard


def o(*els):
    return Observation(Image.new("RGB", (100, 100)), 0, (100, 100), elements=list(els))


def test_safety_rules():
    g = SafetyGuard()
    assert g.assess(Action("click", target="Delete account")).verdict == "confirm"
    assert g.assess(Action("click", target="确认付款")).verdict == "confirm"
    assert g.assess(Action("click", target="Submit")).verdict == "allow"
    assert g.assess(Action("click", target="Senders list")).verdict == "allow"      # 词边界，不误伤
    assert g.assess(Action("type", text="rm -rf /")).verdict == "confirm"
    assert g.assess(Action("hotkey", keys=["alt", "f4"])).verdict == "confirm"
    pw = UIElement(0, "Password", "textbox", (0, 0, 50, 20), focused=True, attrs={"password": "true"})
    assert g.assess(Action("type", text="hunter2"), o(pw)).verdict == "confirm"
    btn = UIElement(0, "Uninstall", "button", (0, 0, 50, 20))
    assert g.assess(Action("click", x=10, y=10), o(btn)).verdict == "confirm"   # 通过坐标反查元素名
    g2 = SafetyGuard(allowed_domains=["example.com"])
    assert g2.assess(Action("navigate", url="https://evil.com/x")).verdict == "deny"
    assert g2.assess(Action("navigate", url="https://docs.example.com/")).verdict == "allow"
    assert g2.assess(Action("navigate", url="file:///tmp/a.html")).verdict == "allow"


def test_safety_gate_modes():
    a = Action("click", target="Delete")
    assert SafetyGuard(mode="deny").gate(a)[0] is False
    assert SafetyGuard(mode="allow").gate(a)[0] is True
    assert SafetyGuard(mode="confirm", confirm_fn=lambda a, r: True).gate(a)[0] is True
    assert SafetyGuard(enabled=False).gate(a) == (True, "")


def test_logger_and_report(tmp_path):
    log = TrajectoryLogger(tmp_path, "r1")
    log.meta(task_text="demo <task>", platform="mock")
    img = Image.new("RGB", (2000, 1000), (10, 20, 30))
    log.step(kind="plan", subgoals=[{"goal": "g1", "expected": "e"}])
    log.step(kind="step", step=1, subgoal=1, action={"type": "click"}, verdict="no_effect", level="L1",
             evidence="nothing", signals={"global_diff": 0}, recovery="reground_zoom",
             before=log.shot(1, "before", img), after=log.shot(1, "after", img))
    log.meta(result={"status": "done", "claimed_done": True, "steps": 1})
    out = log.close()
    html = out.read_text(encoding="utf-8")
    assert "demo &lt;task&gt;" in html and "no_effect" in html and "shots/0001_before.png" in html
    assert max(Image.open(tmp_path / "r1" / "shots" / "0001_before.png").size) == 1280
    lines = (tmp_path / "r1" / "steps.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[1])["recovery"] == "reground_zoom"


def test_cli_doctor_and_replay(tmp_path, capsys):
    main(["doctor"])
    out = capsys.readouterr().out
    assert "auto-detected platform" in out and "web" in out and "android" in out
    log = TrajectoryLogger(tmp_path, "r2")
    log.close(report=False)
    main(["replay", str(tmp_path / "r2")])
    assert (tmp_path / "r2" / "report.html").exists()

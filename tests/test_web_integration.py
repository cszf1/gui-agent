"""Web 集成测试：真实 Chromium（Playwright）+ 本地静态页面 + 脚本策略（无需 API key）。

覆盖：表单填写、加载中等待（时间失配）、意外弹窗遮挡、目标在视口外（越界→滚动）、
新标签页抢焦点（动作前状态核对→恢复焦点）、安全闸门拦截危险按钮、HTML 回放报告。
若沙箱/CI 里没有 playwright 或 chromium，整文件自动跳过。
"""
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.web

ROOT = Path(__file__).resolve().parent.parent


def _chromium_ok() -> bool:
    try:
        with pw.sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


if not _chromium_ok():
    pytest.skip("chromium not installed (run: playwright install chromium)", allow_module_level=True)

from gua.config import build_agent, load_config  # noqa: E402
from gua.env.web import WebEnv  # noqa: E402
from gua.eval.runner import load_tasks, run_suite  # noqa: E402
from gua.scripted import ScriptedPolicy  # noqa: E402


@pytest.fixture(scope="module")
def cfg():
    return load_config(ROOT / "configs" / "web_local.yaml")


def test_web_env_observe_and_execute():
    env = WebEnv(start_url=str(ROOT / "tasks" / "web_assets" / "form.html"))
    try:
        o = env.observe()
        assert o.platform == "web" and o.active_window == "Contact form" and o.screen_size == (1280, 800)
        names = {e.name: e for e in o.elements}
        assert names["Name"].role == "textbox" and names["Pro"].role == "radio" and names["Submit"].role == "button"
        from gua.actions import Action
        x, y = names["Name"].center
        assert env.execute(Action("click", x=x, y=y)).ok
        assert env.execute(Action("type", text="Zed")).ok
        o2 = env.observe()
        assert next(e for e in o2.elements if e.name == "Name").value == "Zed"
        assert not env.execute(Action("click", x=5000, y=10)).ok        # 越界
        assert not env.execute(Action("home")).ok                        # 平台不支持
    finally:
        env.close()


def test_web_suite_scripted_all_pass(cfg, tmp_path):
    tasks = load_tasks(ROOT / "tasks" / "web")
    assert len(tasks) >= 6
    s = run_suite(cfg, tasks, str(tmp_path), policy="scripted", quiet=True)
    rows = {r["task"]: r for r in s["_rows"]}
    failed = {k: (r["status"], r["check_notes"], r["error"]) for k, r in rows.items() if not r["passed"]}
    assert not failed, failed
    assert s["success_rate"] == 1.0 and s["false_done_rate"] == 0.0
    # 方向 A 的各条恢复路径都真实触发过
    assert "in_progress->wait" in rows["web_delayed_report"]["recoveries"]
    assert "blocked->dismiss" in rows["web_modal_export"]["recoveries"]
    assert "failed->scroll" in rows["web_long_page_save"]["recoveries"]
    assert "failed->refocus" in rows["web_form_focus_steal"]["recoveries"]
    assert rows["web_form_focus_steal"]["disturbed"] and s["recovery_rate"] == 1.0
    for r in rows.values():
        assert (Path(r["run_dir"]) / "report.html").exists()
        assert (Path(r["run_dir"]) / "steps.jsonl").stat().st_size > 0


def test_raw_loop_baseline_claims_done_on_stale_page(tmp_path):
    """无验证基线：点了“加载”就宣告完成，判分时报告还没刷新 → 错误宣告完成（false done）。"""
    raw = load_config(ROOT / "configs" / "ablations" / "raw_loop.yaml",
                      {"env": {"platform": "web"}, "safety": {"mode": "deny"},
                       "agent": {"settle_timeout": 0.3, "settle_interval": 0.1}})
    tasks = load_tasks(ROOT / "tasks" / "web" / "delayed_report.json")
    s = run_suite(raw, tasks, str(tmp_path), policy="scripted", quiet=True)
    row = s["_rows"][0]
    assert row["claimed_done"] and not row["passed"] and s["false_done_rate"] == 1.0


def test_safety_gate_blocks_delete_on_real_page(cfg, tmp_path):
    env = WebEnv(start_url=str(ROOT / "tasks" / "web_assets" / "danger.html"))
    try:
        demo = {"subgoals": [{"goal": "delete the account", "expect_text": "Account deleted",
                              "steps": [{"click": "Delete account"}]}]}
        c = dict(cfg)
        c["agent"] = dict(cfg["agent"], max_steps=4, max_steps_per_subgoal=2, max_replans=0)
        agent = build_agent(c, env, llms=ScriptedPolicy(demo).llms(), task_window="Account settings")
        res = agent.run("Delete my account")
        assert env.eval_js("window.__deleted === true") is False
        assert res.status == "fail" and any(e["decision"] == "confirm" and not e["approved"] for e in res.safety_events)
    finally:
        env.close()

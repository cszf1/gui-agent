"""完成核验的补充回归：旧证据不能被无关 UI 变化“洗白”，持久 Loading 不能被丢弃。

对应本轮审查（gui-agent v0.3.1 复审）两条缺陷：

  A. 旧证据洗白：baseline 里就有 expect_text，只要屏幕有任何变化（新 banner、结构化元素改名、
     像素差、文本被重新拼接），旧实现就用“屏幕变了”判定为新鲜证据 → 误报 success。
     或者把 all_text 整体对比，导致不同元素被拼在一起时误判同一证据发生了变化。
  B. 持续 Loading：忙碌判定在“基线里也有同样的 Loading / 命中数量不增加”时豁免，于是一直在转圈的
     界面被当成已完成。

设计契约（本文件固化）：
  - 新鲜度只看承载 expect_text 的片段本身（元素 name/value，或 obs.text 里的独立文本行），
    不看全局像素、也不看整体 all_text。
  - 有 baseline 时：只有“期望证据自身”的出现或被替换（例如 (yesterday)→(today)）才算新鲜；
    旧证据只要还（完整）保留、只是旁边多了 banner、或多了一行重复，就仍算旧证据。
  - 当前屏幕上只要还有忙碌指示（未满 progressbar / aria-busy / 整行状态文字）就一律不算完成，
    不再因基线也有同样的忙碌文字而豁免。

先写反例再修：本文件在修复前应当失败。
"""
import pytest
from PIL import Image

from gua.actions import Action
from gua.env.base import ExecResult, Observation, UIElement
from gua.llm.base import ScriptedLLM
from gua.planner import Subgoal
from gua.verify import Verdict, Verifier


def _obs(elements=(), text="", window="App", size=(400, 300), color=(255, 255, 255), **kw):
    o = Observation(Image.new("RGB", size, color), 0.0, size, 1.0, window, "p", [window],
                    list(elements), "mock", "", text)
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def _report_ready_sg(sg_id=1):
    return [Subgoal(sg_id, "generate the report", expect_text="Report ready")]


# =============================================================== 反例 A：旧证据被无关 UI 变化洗白
def test_goal_old_evidence_not_whitewashed_by_banner_and_pixels():
    """旧行“Report ready”仍在，旁边多了新 banner，像素也变了：不能算新鲜完成。"""
    base = _obs(text="Report ready", elements=[UIElement(0, "Report ready", "text", (0, 0, 80, 10))])
    now = _obs(text="Report ready\nNew version available",
               elements=[UIElement(0, "Report ready", "text", (0, 0, 80, 10)),
                         UIElement(1, "New version available", "button", (0, 20, 120, 40))],
               color=(235, 235, 235))
    c = Verifier(None).check_goal(now, "generate report", "", "Report ready", baseline=base)
    assert c.verdict != Verdict.SUCCESS


def test_final_old_evidence_not_whitewashed_by_banner_and_pixels():
    base = _obs(text="Report ready", elements=[UIElement(0, "Report ready", "text", (0, 0, 80, 10))])
    now = _obs(text="Report ready\nNew version available",
               elements=[UIElement(0, "Report ready", "text", (0, 0, 80, 10)),
                         UIElement(1, "New version available", "button", (0, 20, 120, 40))],
               color=(235, 235, 235))
    c = Verifier(None).check_final(now, "generate the report", _report_ready_sg(), baseline=base)
    assert c.verdict != Verdict.SUCCESS


def test_goal_repeated_old_line_is_not_new_evidence():
    base = _obs(text="Report ready")
    now = _obs(text="Report ready\nReport ready")
    c = Verifier(None).check_goal(now, "generate report", "", "Report ready", baseline=base)
    assert c.verdict != Verdict.SUCCESS


def test_final_repeated_old_line_is_not_new_evidence():
    base = _obs(text="Report ready")
    now = _obs(text="Report ready\nReport ready")
    c = Verifier(None).check_final(now, "generate the report", _report_ready_sg(), baseline=base)
    assert c.verdict != Verdict.SUCCESS


def test_goal_unrelated_text_appended_to_same_line_is_still_old():
    """旧证据还完整保留，只是在同一行前后附加了无关文字：不应算刷新。"""
    base = _obs(text="Report ready")
    now = _obs(text="Report ready — generated at 10:00")
    c = Verifier(None).check_goal(now, "generate report", "", "Report ready", baseline=base)
    assert c.verdict != Verdict.SUCCESS


def test_final_unrelated_text_appended_to_same_line_is_still_old():
    base = _obs(text="Report ready")
    now = _obs(text="Report ready — generated at 10:00")
    c = Verifier(None).check_final(now, "generate the report", _report_ready_sg(), baseline=base)
    assert c.verdict != Verdict.SUCCESS


def test_goal_structured_element_irrelevant_change_is_not_fresh():
    """承载 expect_text 的片段没变，只是别的结构化元素改名/新增：不能算新鲜。"""
    base = _obs(text="Report ready",
                elements=[UIElement(0, "Report ready", "text", (0, 0, 80, 10)),
                          UIElement(1, "Settings", "button", (0, 40, 80, 60))])
    now = _obs(text="Report ready",
               elements=[UIElement(0, "Report ready", "text", (0, 0, 80, 10)),
                         UIElement(1, "Preferences", "button", (0, 40, 80, 60)),
                         UIElement(2, "Beta", "tab", (0, 60, 80, 80))])
    c = Verifier(None).check_goal(now, "generate report", "", "Report ready", baseline=base)
    assert c.verdict != Verdict.SUCCESS


def test_final_structured_element_irrelevant_change_is_not_fresh():
    base = _obs(text="Report ready",
                elements=[UIElement(0, "Report ready", "text", (0, 0, 80, 10)),
                          UIElement(1, "Settings", "button", (0, 40, 80, 60))])
    now = _obs(text="Report ready",
               elements=[UIElement(0, "Report ready", "text", (0, 0, 80, 10)),
                         UIElement(1, "Preferences", "button", (0, 40, 80, 60)),
                         UIElement(2, "Beta", "tab", (0, 60, 80, 80))])
    c = Verifier(None).check_final(now, "generate the report", _report_ready_sg(), baseline=base)
    assert c.verdict != Verdict.SUCCESS


# =============================================================== 反例 B：持续 Loading 被丢弃
def test_goal_persistent_loading_not_ignored_because_baseline_also_loading():
    base = _obs(text="Report ready\nLoading...")
    now = _obs(text="Report ready\nLoading...\nNew version available", color=(235, 235, 235))
    c = Verifier(None).check_goal(now, "generate report", "", "Report ready", baseline=base)
    assert c.verdict != Verdict.SUCCESS


def test_final_persistent_loading_not_ignored_because_baseline_also_loading():
    base = _obs(text="Report ready\nLoading...")
    now = _obs(text="Report ready\nLoading...\nNew version available", color=(235, 235, 235))
    c = Verifier(None).check_final(now, "generate the report", _report_ready_sg(), baseline=base)
    assert c.verdict != Verdict.SUCCESS


def test_busy_ignores_baseline_count_exemption():
    """busy() 不再接受 baseline 计数豁免：当前屏幕只要还有整行状态文字就算忙。"""
    v = Verifier(None)
    obs = _obs(text="Loading...")
    assert v.busy(obs)


# =============================================================== 不能误伤的“正常”路径
def test_goal_status_transition_yesterday_to_today_is_fresh():
    base = _obs(text="Report ready (yesterday)")
    now = _obs(text="Report ready (today)")
    c = Verifier(None).check_goal(now, "generate report", "", "Report ready", baseline=base)
    assert c.verdict == Verdict.SUCCESS


def test_final_status_transition_yesterday_to_today_is_fresh():
    base = _obs(text="Report ready (yesterday)")
    now = _obs(text="Report ready (today)")
    c = Verifier(None).check_final(now, "generate the report", _report_ready_sg(), baseline=base)
    assert c.verdict == Verdict.SUCCESS


def test_goal_genuinely_new_evidence_is_success():
    base = _obs(text="Working")
    now = _obs(text="Saved")
    c = Verifier(None).check_goal(now, "save the file", "", "Saved", baseline=base)
    assert c.verdict == Verdict.SUCCESS


def test_final_genuinely_new_evidence_is_success():
    base = _obs(text="Working")
    now = _obs(text="Saved")
    c = Verifier(None).check_final(now, "save the file", [Subgoal(1, "save the file", expect_text="Saved")],
                                   baseline=base)
    assert c.verdict == Verdict.SUCCESS


def test_goal_busy_cleared_then_completes():
    base = _obs(text="Report ready (old)\nLoading...")
    now = _obs(text="Report ready (done)")
    c = Verifier(None).check_goal(now, "generate report", "", "Report ready", baseline=base)
    assert c.verdict == Verdict.SUCCESS


# =============================================================== L2 必须拿到旧证据上下文
def test_goal_l2_prompt_carries_old_evidence_context():
    sllm = ScriptedLLM(fn=lambda s, t, i: '{"verdict":"uncertain","evidence":"x"}')
    v = Verifier(sllm)
    base = _obs(text="Report ready")
    now = _obs(text="Report ready")
    v.check_goal(now, "generate report", "report is ready", "Report ready", baseline=base)
    assert sllm.prompts, "L2 must be consulted when only stale evidence exists"
    prompt = sllm.prompts[-1]
    assert "already visible" in prompt and "stale" in prompt.lower()


# =============================================================== 步骤级：同类忙碌 / 旧证据
def test_step_new_evidence_while_still_busy_is_in_progress():
    """步骤级：动作后虽然出现了新证据，但屏幕上还在 Loading：不能判 success（fail-closed）。"""
    v = Verifier(None)
    before = _obs(text="Starting\nLoading...")
    after = _obs(text="Report ready\nLoading...", color=(230, 230, 230))
    c = v.rule_check(before, after, Action("click", x=10, y=10), ExecResult(True), True, expect_text="Report ready")
    assert c.verdict == Verdict.IN_PROGRESS


def test_step_status_transition_is_fresh_evidence():
    """步骤级：承载 expect_text 的行被替换（(yesterday)→(today)）算新鲜；只是屏幕变了不算。"""
    v = Verifier(None)
    stale = v.rule_check(_obs(text="Report ready (yesterday)"),
                         _obs(text="Report ready (yesterday)", color=(230, 230, 230)),
                         Action("click", x=10, y=10), ExecResult(True), True, expect_text="Report ready")
    assert stale.verdict != Verdict.SUCCESS
    fresh = v.rule_check(_obs(text="Report ready (yesterday)"), _obs(text="Report ready (today)"),
                         Action("click", x=10, y=10), ExecResult(True), True, expect_text="Report ready")
    assert fresh.verdict == Verdict.SUCCESS


# =============================================================== final 的 L2 也要拿到旧证据上下文
def test_final_l2_prompt_carries_old_evidence_context():
    sllm = ScriptedLLM(fn=lambda s, t, i: '{"verdict":"uncertain","evidence":"x"}')
    v = Verifier(sllm)
    base = _obs(text="Report ready")
    now = _obs(text="Report ready")
    v.check_final(now, "generate the report", _report_ready_sg(), baseline=base)
    assert sllm.prompts, "L2 must be consulted when only stale evidence exists"
    prompt = sllm.prompts[-1]
    assert "already visible" in prompt and "stale" in prompt.lower()


def test_busy_with_progress_percentage_or_counter_blocks_success():
    v = Verifier(None)
    for busy_line in ["Loading 45%", "Processing 3/10", "正在加载 45%", "Loading, please wait", "Loading results..."]:
        assert v.busy(_obs(text=busy_line)) != "", f"{busy_line} should be recognized as busy"
        obs = _obs(text="Report ready" + chr(10) + busy_line)
        c = v.check_goal(obs, "generate report", "", "Report ready")
        assert c.verdict != Verdict.SUCCESS, f"{busy_line} must block goal success"
        c_final = v.check_final(obs, "generate report", _report_ready_sg())
        assert c_final.verdict != Verdict.SUCCESS, f"{busy_line} must block final success"


def test_active_window_only_stale_evidence_is_not_success():
    v = Verifier(None)
    base = _obs(text="canvas body", active_window="Report ready")
    now = _obs(text="canvas body", active_window="Report ready")
    c = v.check_goal(now, "generate report", "", "Report ready", baseline=base)
    assert c.verdict != Verdict.SUCCESS, "unchanged active_window title must be treated as stale"
    c_final = v.check_final(now, "generate report", _report_ready_sg(), baseline=base)
    assert c_final.verdict != Verdict.SUCCESS, "unchanged active_window title must block final success"

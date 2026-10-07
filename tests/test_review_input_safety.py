"""第三轮输入 / 安全审查回归测试（复合按键、日志 / 确认脱敏、拒绝意图、焦点过期）。

覆盖条目：
1 复合按键：Action.validate / 安全闸门 / env 直接执行入口都严格限制 hotkey 序列（tab+enter、
  多字符 secret+enter 非法，必须拆独立动作）；非法组合 deny 而不是 confirm 后执行。
2 日志 / 确认脱敏：按键含 Enter/Tab/Backspace 也照样脱敏；unreported 与 password 同等脱敏；
  拒绝签名一律哈希；安全副本处理所有字符串字段。
3 表单拒绝去重：点击优先真实 name / attrs.dom_id；提交优先 form_submit / form_submit_id，
  不拼文本框名；换 target 描述绕不过已拒绝目标；桌面不猜表单提交目标。
4 焦点过期：复用 _activation 语义（type 的 \n / \r / 空格 / 字符激活、clear、所有移焦按键）。
5 共享 Scrubber：SafetyGuard.scrubber；日志 / 确认 / reason 统一清洗；敏感输入独立登记完整 payload。
6 Scrubber 接口：mark_sensitive / images_blocked / add / extend(explicit)、scrub_obj 清洗 dict key、
  替换确定不递归失真。

本文件不依赖 Playwright（Web 浏览器版本由 Web 组负责），可在 Windows / macOS / Linux 运行。
"""
import json
import sys

import pytest
from PIL import Image

from fakes import fake_pyautogui, import_platform_module
from gua.actions import Action, ActionParseError, parse_action
from gua.env.android import AndroidEnv
from gua.env.base import Observation, UIElement
from gua.safety import SafetyGuard
from gua.sensitive import (REDACTED, Scrubber, carries_text, is_sensitive_type, redacted_action,
                           safe_short, safe_view)

SECRET = "S3cr3t-Pa55!"
TOKEN = "tok-12345"


def _obs(elements=(), text="", window="App", size=(400, 300), **kw):
    o = Observation(Image.new("RGB", size, (255, 255, 255)), 0.0, size, 1.0, window, "p",
                    [window], list(elements), "mock", "", text)
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def _pw_focused(name="Password"):
    return _obs([UIElement(0, name, "textbox", (10, 10, 200, 40), focused=True, is_password=True)],
                focus_state="known")


def _normal_focused(name="Search"):
    return _obs([UIElement(0, name, "textbox", (10, 10, 200, 40), focused=True)], focus_state="known")


def _unreported():
    # 有元素但没有任何 focused 元素、focus_state 也未报告 → unreported
    return _obs([UIElement(0, "Notes", "textbox", (10, 10, 200, 40))], focus_state="")


# ===============================================================  1 复合按键严格校验
def test_hotkey_sequence_invalid_in_parse_action():
    for keys in (["tab", "enter"], ["hunter2", "enter"], ["hunter2"], ["a", "b"], ["ctrl", "shift"]):
        with pytest.raises(ActionParseError) as ei:
            parse_action({"type": "hotkey", "keys": keys})
        assert ei.value.code == "bad_value" and ei.value.field == "keys", keys


def test_key_down_sequence_at_most_one_non_modifier():
    assert parse_action({"type": "key_down", "keys": ["shift"]})
    assert parse_action({"type": "key_down", "keys": ["alt", "f4"]})
    for keys in (["tab", "enter"], ["hunter2", "enter"]):
        with pytest.raises(ActionParseError):
            parse_action({"type": "key_down", "keys": keys})


def test_legal_combos_and_alias_names_still_pass():
    a = parse_action({"type": "hotkey", "keys": "ctrl+shift+delete"})
    assert a.keys == ["ctrl", "shift", "delete"]
    assert parse_action({"type": "hotkey", "keys": ["Shift", "Del"]})
    assert parse_action({"type": "hotkey", "keys": ["super", "q"]})
    assert parse_action({"type": "hotkey", "keys": ["return"]})
    # 规范键别名仍然可用
    from gua.keys import canonical_key
    assert canonical_key("Del") == "delete" and canonical_key("Escape") == "esc"


@pytest.mark.parametrize("keys", [[""], ["  "], ["ctrl", ""]])
def test_blank_keys_stay_invalid(keys):
    with pytest.raises(ActionParseError):
        parse_action({"type": "hotkey", "keys": keys})


def test_validate_raises_for_sequence():
    with pytest.raises(ActionParseError):
        Action("hotkey", keys=["tab", "enter"]).validate()
    Action("hotkey", keys=["ctrl", "shift", "delete"]).validate()      # 合法组合不抛


def test_gate_denies_invalid_sequence_even_in_allow_mode():
    """非法组合必须 deny，而不是“确认后执行”（allow 模式会自动确认 → 就会执行）。"""
    g = SafetyGuard(mode="allow")
    ok, why = g.gate(Action("hotkey", keys=["tab", "enter"]))
    assert not ok and "invalid key sequence" in why
    assert g.log and g.log[-1]["decision"] == "deny" and not g.log[-1]["approved"]


def test_invalid_sequence_recorded_and_repeat_denied():
    g = SafetyGuard(mode="deny")
    a = Action("key_down", keys=["tab", "enter"])
    assert not g.gate(a)[0]
    assert any(s.startswith("keys|") for s in g.denied)
    assert not g.gate(Action("key_down", keys=["tab", "enter"]))[0]


def test_android_execute_rejects_sequence():
    calls = []

    def runner(args, binary=False):
        calls.append(args)
        if args[:2] == ["shell", "wm size"]:
            return "Physical size: 1080x2400"
        return ""

    env = AndroidEnv(runner=runner)
    r = env.execute(Action("hotkey", keys=["tab", "enter"]))
    assert not r.ok and "invalid_argument" in r.error
    assert all(c[0] != "shell" or c[1] == "wm size" for c in calls), calls
    # 合法单键仍然执行
    calls.clear()
    assert env.execute(Action("hotkey", keys=["enter"])).ok
    assert any(c[:2] == ["shell", "input keyevent 66"] for c in calls)


def test_desktop_input_entry_rejects_sequence(monkeypatch):
    pg = fake_pyautogui()
    monkeypatch.setitem(sys.modules, "pyautogui", pg)
    from gua.env.desktop import PyAutoGUIInput
    inp = PyAutoGUIInput("windows", 1.0)
    with pytest.raises(ActionParseError):
        inp.run(Action("hotkey", keys=["tab", "enter"]))
    assert not any(c[0] == "hotkey" for c in pg.calls)
    inp.run(Action("hotkey", keys=["ctrl", "shift", "delete"]))
    assert any(c[0] == "hotkey" for c in pg.calls)


def test_windows_env_execute_rejects_sequence(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyautogui", fake_pyautogui())
    win = import_platform_module(monkeypatch, "windows", "win32")
    env = win.WindowsEnv.__new__(win.WindowsEnv)
    env._mon = {"left": 0, "top": 0, "width": 1920, "height": 1080}
    from gua.env.desktop import PyAutoGUIInput
    env.input = PyAutoGUIInput("windows", 1.0)
    r = env.execute(Action("hotkey", keys=["enter", "tab"]))
    assert not r.ok and "bad_value" in r.error


# ===============================================================  2 脱敏与拒绝签名
def test_carries_text_counts_enter_tab_backspace_and_single_chars():
    assert carries_text(Action("hotkey", keys=["s", "enter"]))
    assert carries_text(Action("hotkey", keys=["tab"]))
    assert carries_text(Action("key_down", keys=["backspace"]))
    assert carries_text(Action("hotkey", keys=["shift", "a"]))
    assert not carries_text(Action("hotkey", keys=["f4"]))
    assert not carries_text(Action("hotkey", keys=["ctrl", "s"]))
    assert not carries_text(Action("hotkey", keys=["esc"]))


def test_key_action_mixing_enter_is_still_redacted():
    a = Action("hotkey", keys=["s", "enter"])
    assert is_sensitive_type(a, _pw_focused())
    out = safe_short(a, _pw_focused())
    assert '"s"' not in out and REDACTED in out
    # 不因混入 Enter 而取消：未知焦点同样脱敏
    assert '"s"' not in safe_short(a, _obs([], focus_state="unknown"))


def test_unreported_is_redacted_like_password_but_execution_policy_unchanged():
    obs = _unreported()
    a = Action("type", text="cats")
    assert is_sensitive_type(a, obs)
    assert "cats" not in safe_short(a, obs)                 # 日志不能有字符
    # 可执行策略保持 v0.3.1：unreported 的普通输入不由闸门强制确认
    g = SafetyGuard(mode="deny")
    assert g.gate(Action("type", text="cats"), obs)[0]
    # 按键动作同理：日志脱敏，但 unreported 不因此确认
    g2 = SafetyGuard(mode="deny")
    assert g2.gate(Action("hotkey", keys=["s"]), obs)[0]
    assert '"s"' not in json.dumps(g2.log, ensure_ascii=False)


def test_denial_signatures_hash_key_names():
    g = SafetyGuard(mode="deny")
    g.gate(Action("key_down", keys=["x"]), _pw_focused())
    key_sigs = [s for s in g.denied if s.startswith("keys|")]
    assert key_sigs, g.denied
    for s in key_sigs:
        digest = s.split("|", 1)[1]
        assert "x" not in digest and len(digest) == 16 and all(c in "0123456789abcdef" for c in digest)
    assert "x" not in json.dumps(g.denied)


def test_redacted_action_scrubs_every_string_field():
    scrub = Scrubber()
    scrub.add(SECRET)
    a = Action("type", text=SECRET, target="field " + SECRET, target2=SECRET,
               reason="because " + SECRET, url=SECRET, app=SECRET)
    safe = redacted_action(a, _pw_focused(), scrubber=scrub)
    assert safe.text == REDACTED
    for v in (safe.target, safe.target2, safe.reason, safe.url, safe.app):
        assert SECRET not in (v or ""), v


def test_confirm_callback_and_log_never_see_secret_copied_into_metadata():
    scrub = Scrubber()
    scrub.add(SECRET)
    seen = []

    def confirm(a, why):
        seen.append((a.text, a.target, a.reason, why))
        return False

    g = SafetyGuard(mode="confirm", scrubber=scrub, confirm_fn=confirm)
    g.gate(Action("type", text=SECRET, target="copy " + SECRET, reason="why " + SECRET), _pw_focused())
    assert seen
    blob = json.dumps(seen, ensure_ascii=False)
    assert SECRET not in blob
    assert SECRET not in json.dumps(g.log, ensure_ascii=False)


# ===============================================================  3 表单 / 点击目标命名
def test_click_target_uses_element_name_and_dom_id():
    el = UIElement(0, "", "button", (10, 10, 120, 40), attrs={"dom_id": "delete-account-btn"})
    g = SafetyGuard(mode="deny")
    ok, why = g.gate(Action("click", x=50, y=20), _obs([el]))
    assert not ok and "delete" in why.lower()
    sigs = g.signatures(Action("click", x=50, y=20), _obs([el]))
    assert "activate|delete-account-btn" in sigs


def test_submit_uses_form_submit_not_textbox_name():
    # 文本框名字含危险词，但表单提交目标是良性的 Search → 新的语义应放行
    obs = _obs([UIElement(0, "Delete account note", "textbox", (10, 10, 200, 40), focused=True,
                          attrs={"form_submit": "Search"})], focus_state="known")
    assert SafetyGuard(mode="deny").gate(Action("type", text="hi", submit=True), obs)[0]


def test_submit_uses_form_submit_id_when_name_absent():
    obs = _obs([UIElement(0, "Search", "textbox", (10, 10, 200, 40), focused=True,
                          attrs={"form_submit_id": "btn-delete-account"})], focus_state="known")
    ok, why = SafetyGuard(mode="deny").gate(Action("type", text="hi", submit=True), obs)
    assert not ok and "delete" in why.lower()


def test_target_description_cannot_bypass_denied_target():
    # 点不到任何元素（没有观察）：只能靠模型给的 target 描述判定风险。
    # 旧实现把 target 文本写进签名，换个描述就绕过了；新实现按坐标去重。
    g = SafetyGuard(mode="confirm", confirm_fn=lambda a, why: False)
    obs = _obs([])
    assert not g.gate(Action("click", x=500, y=500, target="Delete account"), obs)[0]
    assert "click|(500, 500)" in g.denied
    ok, why = g.gate(Action("click", x=500, y=500, target="the green OK button"), obs)
    assert not ok and "previously rejected" in why


def test_resolvable_element_name_shares_keyboard_and_pointer_denial():
    g = SafetyGuard(mode="confirm", confirm_fn=lambda a, why: False)
    obs = _obs([UIElement(0, "Delete account", "button", (10, 10, 120, 40))])
    assert not g.gate(Action("click", x=50, y=20), obs)[0]
    ok, why = g.gate(Action("click", x=50, y=20, target="the green OK button"), obs)
    assert not ok and "previously rejected" in why
    assert "activate|delete account" in g.denied


def test_desktop_does_not_guess_form_submit_target():
    # 焦点是普通文本框、没有 form_submit；同窗口有危险按钮也不能拿来当提交目标
    obs = _obs([UIElement(0, "Confirm your name", "textbox", (10, 10, 200, 40), focused=True),
                UIElement(1, "Delete everything", "button", (300, 300, 400, 340))], focus_state="known")
    assert SafetyGuard(mode="deny").gate(Action("hotkey", keys=["enter"]), obs)[0]


# ===============================================================  4 焦点过期
def test_plain_typing_keeps_focus_valid():
    g = SafetyGuard(mode="deny")
    obs = _normal_focused()
    assert g.gate(Action("type", text="cats"), obs)[0]
    assert g.gate(Action("hotkey", keys=["enter"]), obs)[0]      # 焦点仍在文本框，可继续判断


def test_type_clear_expires_focus():
    g = SafetyGuard(mode="deny")
    obs = _normal_focused()
    assert g.gate(Action("type", text="cats", clear=True), obs)[0]
    assert not g.gate(Action("hotkey", keys=["enter"]), obs)[0]


def test_type_with_carriage_return_expires_focus():
    g = SafetyGuard(mode="deny")
    obs = _normal_focused()
    assert g.gate(Action("type", text="a\rb"), obs)[0]
    assert not g.gate(Action("hotkey", keys=["enter"]), obs)[0]


def test_typing_into_button_activates_and_expires_focus():
    g = SafetyGuard(mode="deny")
    obs = _obs([UIElement(0, "OK", "button", (10, 10, 120, 40), focused=True)], focus_state="known")
    assert g.gate(Action("type", text=" "), obs)[0]              # 往按钮里打字 = 字符激活（目标良性）
    assert not g.gate(Action("hotkey", keys=["space"]), obs)[0]  # 同一观察焦点已可能移动


def test_tab_expires_focus():
    g = SafetyGuard(mode="deny")
    obs = _normal_focused()
    assert g.gate(Action("hotkey", keys=["tab"]), obs)[0]
    assert not g.gate(Action("hotkey", keys=["enter"]), obs)[0]


# ===============================================================  5 共享 Scrubber
def test_guard_has_own_scrubber_by_default():
    g = SafetyGuard()
    assert isinstance(g.scrubber, Scrubber)
    assert SafetyGuard().scrubber is not g.scrubber       # 每个实例独立，不影响独立使用


def test_guard_uses_shared_scrubber_for_ordinary_token_field():
    scrub = Scrubber()
    scrub.add(TOKEN)
    g = SafetyGuard(mode="allow", scrubber=scrub)
    assert g.scrubber is scrub
    ok, _ = g.gate(Action("type", text=TOKEN), _normal_focused())   # 普通输入框文本本身不脱敏
    assert ok
    assert TOKEN not in json.dumps(g.log, ensure_ascii=False)       # 但已配置的秘密不能露出


def test_guard_registers_standalone_sensitive_payload():
    g = SafetyGuard(mode="deny")
    short = "pw"
    assert not g.gate(Action("type", text=short), _pw_focused())[0]
    assert g.scrubber.images_blocked
    assert g.scrubber.scrub("x" + short + "y") == "x***y"           # 完整 payload 已登记


def test_gate_reason_is_scrubbed():
    scrub = Scrubber()
    scrub.add(SECRET)
    g = SafetyGuard(mode="deny", scrubber=scrub)
    _, why = g.gate(Action("type", text=SECRET), _pw_focused())
    assert SECRET not in why
    assert SECRET not in json.dumps(g.log, ensure_ascii=False)


# ===============================================================  6 Scrubber 接口
def test_mark_sensitive_is_monotonic_and_read_only():
    s = Scrubber()
    assert s.images_blocked is False
    s.add("")
    assert s.images_blocked is False                    # 空字符串不算秘密
    s.add("ab")
    assert s.images_blocked is True
    s.add(None)
    assert s.images_blocked is True
    with pytest.raises(AttributeError):
        s.images_blocked = False                        # 只读属性


def test_add_min_len_and_explicit():
    s = Scrubber(min_len=4)
    s.add("ab")
    assert s.images_blocked and s.scrub("ab") == "ab"   # 默认不把短串当搜索词
    s2 = Scrubber(min_len=4)
    s2.add("ab", explicit=True)
    assert s2.scrub("zabz") == "z***z"
    s3 = Scrubber()
    s3.extend(["abcd", "efgh"])
    assert s3.scrub("xabcdy efgh") == "x***y ***"


def test_scrub_is_deterministic_and_not_recursive():
    s = Scrubber()
    s.add(TOKEN)
    y = s.scrub("a" + TOKEN + "b " + TOKEN)
    assert y == "a***b ***"
    assert s.scrub(y) == y                              # *** 不会再被替换 / 失真
    s2 = Scrubber()
    s2.mark_sensitive()
    assert s2.scrub("***") == "***"                     # 无 form 时不做任何替换


def test_scrub_obj_cleans_dict_keys_and_values():
    s = Scrubber()
    s.add(TOKEN)
    assert s.scrub_obj({TOKEN: TOKEN, "keep": ["x" + TOKEN]}) == {"***": "***", "keep": ["x***"]}


def test_safe_view_marks_redacted_flag():
    d = safe_view(Action("type", text=SECRET), _pw_focused())
    assert d.get("redacted") is True and d.get("text") == REDACTED

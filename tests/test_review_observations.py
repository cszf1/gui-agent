"""v0.3.1 审查观察项回归：平台敏感名称 / 描述 / 值 / 公共序列化残留（条 1-4）。

覆盖：
- 条 1  web autocomplete token 列表拆分、raw secure 最高优先级、不扩大信用卡范围
- 条 2  Android content-desc / 密码子树、AXTitle/Description/Help、UIA/AT-SPI name 不外泄秘密
- 条 3  finalize / UIElement.brief / Observation.all_text 公共兜底（含直接构造 UIElement）
- 条 4  web dom_id / form_submit_id 在 attrs 透传，且不进入模型 brief
"""
import json

from PIL import Image

from gua.env.a11y import (android_xml_to_elements, atspi_tree_to_elements, ax_tree_to_elements, finalize,
                          uia_raw, web_raws)
from gua.env.base import SAFE_PASSWORD_NAME, Observation, UIElement

SECRET = "s3cr3t-Pa55w0rd-zz"


def _obs(els, text=""):
    return Observation(Image.new("RGB", (400, 300), (255, 255, 255)), 0.0, (400, 300), 1.0, "App", "p",
                       ["App"], list(els), "mock", "", text)


def _no_leak(els, text=""):
    blob = json.dumps([e.name for e in els] + [e.value for e in els], ensure_ascii=False) + "\n" + \
        "\n".join(e.brief() for e in els) + "\n" + _obs(els, text).all_text() + "\n" + text
    assert SECRET not in blob, blob


# ---------------------------------------------------------------- 条 1 autocomplete token 列表
def test_web_autocomplete_token_list_and_credit_cards_untouched():
    raws = web_raws([
        {"tag": "input", "type": "text", "autocomplete": "username current-password",
         "value": SECRET, "rect": [0, 0, 10, 10]},
        {"tag": "input", "type": "text", "autocomplete": "new-password webauthn",
         "value": SECRET, "rect": [0, 20, 10, 30]},
        {"tag": "input", "type": "text", "autocomplete": "one-time-code",
         "value": SECRET, "rect": [0, 40, 10, 50]},
    ])
    assert all(r["is_password"] and r["value"] is None for r in raws)
    # 不擅自扩大信用卡范围：cc-number / cc-csc 不是密码框
    cc = web_raws([{"tag": "input", "type": "text", "autocomplete": "cc-number",
                    "value": "4111111111111111", "rect": [0, 60, 10, 70]},
                   {"tag": "input", "type": "text", "autocomplete": "cc-csc",
                    "value": "123", "rect": [0, 80, 10, 90]}])
    assert not any(r["is_password"] for r in cc)
    assert [r["value"] for r in cc] == ["4111111111111111", "123"]


def test_web_secure_raw_has_top_priority_and_masked_value_cleared():
    r = web_raws([{"tag": "input", "type": "text", "secure": True, "name": "pin", "value": SECRET,
                   "autocomplete": "off", "rect": [0, 0, 10, 10]}])[0]
    assert r["is_password"] and r["value"] is None


# ---------------------------------------------------------------- 条 2 平台敏感字段
def test_android_password_content_desc_and_parent_label_do_not_leak():
    xml = f'''<?xml version='1.0' ?><hierarchy>
<node class="android.widget.LinearLayout" clickable="true" bounds="[0,0][900,300]" text="" content-desc="">
  <node class="android.widget.EditText" password="true" text="{SECRET}" content-desc="{SECRET}"
        resource-id="com.app:id/pwd" bounds="[10,10][890,120]" clickable="true"/>
</node>
<node class="android.widget.EditText" password="true" text="{SECRET}" content-desc="{SECRET}"
      bounds="[10,300][900,400]" clickable="true"/>
</hierarchy>'''
    els, text = android_xml_to_elements(xml, (1080, 2000))
    _no_leak(els, text)
    pws = [e for e in els if e.is_password]
    assert pws and all(e.name == SAFE_PASSWORD_NAME for e in pws)
    assert all(SECRET not in e.name for e in els)          # 父容器名字也不能带密码子树文字


def test_android_normal_parent_label_still_names_container():
    xml = '''<?xml version='1.0' ?><hierarchy>
<node class="android.widget.LinearLayout" clickable="true" bounds="[0,0][1080,300]" text="" content-desc="">
  <node class="android.widget.TextView" text="Network &amp; internet" bounds="[10,10][600,60]"/>
</node></hierarchy>'''
    els, _ = android_xml_to_elements(xml, (1080, 2000))
    assert any(e.name == "Network & internet" for e in els)  # 普通容器命名不回归


def test_ax_password_title_description_help_do_not_leak():
    for field in ("AXTitle", "AXDescription", "AXHelp"):
        node = {"AXRole": "AXSecureTextField", field: SECRET, "AXValue": SECRET,
                "AXPosition": [0, 0], "AXSize": [100, 20]}
        els, text = ax_tree_to_elements(node, (800, 600))
        assert els[0].is_password
        _no_leak(els, text)
        assert els[0].name == SAFE_PASSWORD_NAME


def test_atspi_and_uia_password_name_do_not_leak():
    at = {"role": "password text", "name": SECRET, "extents": [0, 0, 100, 20], "states": ["showing"]}
    els, text = atspi_tree_to_elements(at, (800, 600))
    assert els[0].is_password
    _no_leak(els, text)
    assert els[0].name == SAFE_PASSWORD_NAME

    ctrl = type("C", (), dict(
        ControlTypeName="EditControl", Name=SECRET, AutomationId="pw1", IsOffscreen=False, IsEnabled=True,
        HasKeyboardFocus=False, IsPassword=True,
        BoundingRectangle=type("R", (), dict(left=0, top=0, right=100, bottom=20,
                                             width=lambda s: 100, height=lambda s: 20))(),
        GetValuePattern=lambda s: None))()
    els, text = finalize([uia_raw(ctrl)], (800, 600))
    assert els[0].is_password
    _no_leak(els, text)
    assert els[0].name == SAFE_PASSWORD_NAME


# ---------------------------------------------------------------- 条 3 公共序列化兜底
def test_common_layer_direct_construction_never_leaks():
    e = UIElement(0, SECRET, "textbox", (0, 0, 20, 20), value=SECRET, is_password=True,
                  attrs={"value": SECRET, "text": SECRET, "password": "true"})
    assert SECRET not in e.brief()
    assert SECRET not in _obs([e]).all_text()

    els, text = finalize([{"name": SECRET, "role": "textbox", "rect": (0, 0, 20, 20), "value": SECRET,
                           "is_password": True, "attrs": {"text": SECRET, "password": "true"}}], (400, 300))
    assert els[0].value is None
    assert els[0].name == SAFE_PASSWORD_NAME
    assert els[0].attrs.get("text") is None        # 敏感 attrs 副本被剔除
    assert els[0].attrs.get("password") == "true"  # 身份属性保留
    _no_leak(els, text)


def test_normal_elements_name_value_preserved():
    raws = web_raws([{"tag": "input", "type": "text", "name": "Email", "value": "a@b.com",
                      "rect": [0, 0, 100, 20]},
                     {"tag": "button", "name": "Save", "rect": [0, 30, 100, 60]}])
    els, _ = finalize(raws, (400, 300))
    assert [e.name for e in els] == ["Email", "Save"]
    assert els[0].value == "a@b.com"
    assert "Email" in _obs(els).all_text() and "Save" in _obs(els).all_text()


# ---------------------------------------------------------------- 条 4 安全身份 id 透传
def test_web_security_ids_passed_through_without_model_exposure():
    item = {"tag": "input", "type": "text", "name": "user", "rect": [0, 0, 100, 20], "gid": 3,
            "dom_id": "gua-3", "form_submit_id": "gua-9", "form_submit": "Delete account"}
    r = web_raws([item])[0]
    assert r["attrs"]["dom_id"] == "gua-3"
    assert r["attrs"]["form_submit_id"] == "gua-9"
    assert r["attrs"]["form_submit"] == "Delete account"          # 旧字段保留
    # 焦点探测的 raw 同样透传
    rf = web_raws([{"tag": "input", "type": "text", "name": "user", "rect": [0, 0, 100, 20],
                    "dom_id": "gua-3", "form_submit_id": "gua-9", "secure": False}])[0]
    assert rf["attrs"]["dom_id"] == "gua-3" and rf["attrs"]["form_submit_id"] == "gua-9"
    # 内部 id 不进入模型 brief
    els, _ = finalize([r], (400, 300))
    assert "gua-3" not in els[0].brief() and "gua-9" not in els[0].brief()

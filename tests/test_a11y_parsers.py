"""各平台无障碍树 → 统一元素模型（fixture 测试，无需真实设备）。"""
import json

from conftest import FIX
from gua.env.a11y import (android_xml_to_elements, atspi_tree_to_elements, ax_tree_to_elements, finalize,
                          parse_android_bounds, web_raws, web_role)
from gua.env.base import INTERACTIVE_ROLES, SAFE_PASSWORD_NAME


def by_name(els, name):
    return next(e for e in els if e.name == name)


def test_android_uiautomator_xml():
    els, text = android_xml_to_elements((FIX / "android_settings.xml").read_text(encoding="utf-8"), (1080, 2400))
    names = [e.name for e in els]
    # 可点击容器用子孙文字命名；resource-id 只是兜底
    assert "Network & internet | Mobile, Wi‑Fi, hotspot" in names
    assert by_name(els, "Search settings").role in {"button", "text"}
    wifi = by_name(els, "Wi‑Fi")
    assert wifi.role == "switch" and wifi.checked is True and wifi.center == (950, 650)
    edit = by_name(els, "Device name")
    assert edit.role == "textbox" and edit.focused
    assert by_name(els, "Navigate up").role == "button"
    battery = by_name(els, "Battery")
    assert not battery.enabled and battery.rect[3] == 2400          # 裁剪到屏幕内
    assert all(e.name != "Zero" for e in els)                         # 零尺寸节点丢弃
    assert "Network & internet" in text
    assert [e.id for e in els] == list(range(len(els)))
    assert parse_android_bounds("[1,2][3,4]") == (1, 2, 3, 4)


def test_atspi_tree():
    tree = json.loads((FIX / "atspi_gedit.json").read_text(encoding="utf-8"))
    els, text = atspi_tree_to_elements(tree, (1280, 800))
    assert by_name(els, "Save").rect == (1100, 8, 1170, 42)
    assert by_name(els, "Main Menu").role == "switch" and by_name(els, "Main Menu").checked
    tb = next(e for e in els if e.role == "textbox")
    assert tb.value == "hello gui" and tb.focused
    assert by_name(els, "Highlight").role == "checkbox" and by_name(els, "Highlight").checked is False
    assert by_name(els, "Save changes?").role == "dialog"
    assert all(e.name != "Hidden" for e in els)                       # 不在 showing 状态的节点忽略
    assert "Ln 1, Col 10" in text
    # HiDPI 缩放
    els2, _ = atspi_tree_to_elements(tree, (2560, 1600), scale=2.0)
    assert by_name(els2, "Save").rect == (2200, 16, 2340, 84)


def test_ax_tree_retina_scale():
    tree = json.loads((FIX / "ax_textedit.json").read_text(encoding="utf-8"))
    els, text = ax_tree_to_elements(tree, (1600, 1200), scale=2.0)
    assert by_name(els, "Bold").role == "checkbox" and by_name(els, "Bold").checked is True
    assert by_name(els, "Format").role == "tab"                        # AXSubrole 优先
    assert by_name(els, "Helvetica").role == "combobox"
    assert by_name(els, "Save dialog").role == "dialog"
    assert not by_name(els, "Cancel").enabled
    pw = next(e for e in els if e.is_password)
    assert pw.name == SAFE_PASSWORD_NAME and pw.value is None
    assert pw.attrs.get("password") == "true" and pw.rect == (440, 400, 840, 444)
    assert by_name(els, "document body").value == "hello"
    assert "Words: 1" in text
    assert by_name(els, "close button").center == (30, 78)


def test_web_roles_and_offscreen():
    assert web_role("input", "", "checkbox") == "checkbox"
    assert web_role("input", "", "email") == "textbox"
    assert web_role("div", "dialog") == "dialog"
    assert web_role("a") == "link"
    raws = web_raws([{"tag": "button", "name": "Save", "rect": [10, 2000, 90, 2030]},
                     {"tag": "input", "type": "text", "name": "Name", "value": "A", "rect": [0, 0, 100, 20]}])
    els, _ = finalize(raws, (1280, 800))
    assert els[0].offscreen and els[0].center == (50, 2015)
    assert els[1].value == "A" and els[1].role in INTERACTIVE_ROLES
    els, _ = finalize(raws, (1280, 800), include_offscreen=False)
    assert [e.name for e in els] == ["Name"]

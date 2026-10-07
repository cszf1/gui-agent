"""各平台无障碍树 → 统一 UIElement 列表。

纯 Python、无平台依赖，所以能在任何机器上用 fixture 做单元测试：
- Android：`uiautomator dump` 产生的 XML（bounds="[l,t][r,b]"）
- Linux：AT-SPI（pyatspi）节点先被 env/linux.py 序列化成 dict 树，再在这里转换
- macOS：AX API（AXUIElement）节点先被 env/macos.py 序列化成 dict 树，再在这里转换
- Windows：UIA ControlTypeName 映射
- Web：DOM 快照（env/web.py 注入 JS 得到的扁平列表）

统一流程：raw dict 列表 → finalize()：裁剪到屏幕、标记 offscreen、去重、过滤、编号。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Iterable, Optional

from .base import INTERACTIVE_ROLES, UIElement

# ---------------------------------------------------------------- 角色映射
ANDROID_ROLE = [
    ("EditText", "textbox"), ("AutoCompleteTextView", "textbox"), ("CheckBox", "checkbox"),
    ("CheckedTextView", "checkbox"), ("Switch", "switch"), ("ToggleButton", "switch"),
    ("RadioButton", "radio"), ("ImageButton", "button"), ("Button", "button"), ("Spinner", "combobox"),
    ("SeekBar", "slider"), ("TabWidget", "tab"), ("ImageView", "image"), ("TextView", "text"),
    ("WebView", "group"), ("RecyclerView", "list"), ("ListView", "list"),
]
ATSPI_ROLE = {
    "push button": "button", "toggle button": "switch", "button": "button", "text": "textbox",
    "entry": "textbox", "password text": "textbox", "spin button": "textbox", "check box": "checkbox",
    "check menu item": "menuitem", "radio button": "radio", "radio menu item": "menuitem",
    "combo box": "combobox", "menu item": "menuitem", "menu": "menu", "page tab": "tab",
    "list item": "listitem", "link": "link", "dialog": "dialog", "alert": "dialog", "file chooser": "dialog",
    "label": "text", "static": "text", "heading": "text", "paragraph": "text", "table cell": "cell",
    "slider": "slider", "tree item": "treeitem", "icon": "image", "image": "image", "frame": "window",
    "scroll bar": "scrollbar", "panel": "group", "filler": "group",
}
AX_ROLE = {
    "AXButton": "button", "AXMenuButton": "button", "AXTextField": "textbox", "AXTextArea": "textbox",
    "AXSecureTextField": "textbox", "AXComboBox": "combobox", "AXPopUpButton": "combobox",
    "AXCheckBox": "checkbox", "AXRadioButton": "radio", "AXMenuItem": "menuitem", "AXMenuBarItem": "menuitem",
    "AXLink": "link", "AXStaticText": "text", "AXImage": "image", "AXSlider": "slider", "AXCell": "cell",
    "AXRow": "listitem", "AXSheet": "dialog", "AXDisclosureTriangle": "button", "AXIncrementor": "slider",
    "AXWindow": "window", "AXGroup": "group", "AXToolbar": "group", "AXTabGroup": "group",
}
AX_SUBROLE = {"AXTabButton": "tab", "AXDialog": "dialog", "AXSystemDialog": "dialog",
              "AXSearchField": "textbox", "AXSwitch": "switch", "AXToggle": "switch"}
UIA_ROLE = {
    "Button": "button", "SplitButton": "button", "Edit": "textbox", "Document": "textbox",
    "CheckBox": "checkbox", "RadioButton": "radio", "ComboBox": "combobox", "MenuItem": "menuitem",
    "TabItem": "tab", "ListItem": "listitem", "TreeItem": "treeitem", "Hyperlink": "link", "DataItem": "cell",
    "Slider": "slider", "Text": "text", "Image": "image", "Window": "window", "MenuBar": "menu", "Menu": "menu",
    "List": "list", "Group": "group", "Pane": "group", "ToolBar": "group", "ScrollBar": "scrollbar",
}
ARIA_ROLE = {
    "a": "link", "button": "button", "input": "textbox", "textarea": "textbox", "select": "combobox",
    "searchbox": "textbox", "option": "listitem", "menuitemcheckbox": "menuitem", "menuitemradio": "menuitem",
    "alertdialog": "dialog", "img": "image", "gridcell": "cell", "summary": "button", "label": "text",
}


def android_role(cls: str, clickable: bool) -> str:
    short = cls.rsplit(".", 1)[-1]
    for suffix, role in ANDROID_ROLE:
        if short.endswith(suffix):
            if role in {"text", "image", "group", "list"} and clickable:
                return "button"
            return role
    return "button" if clickable else "other"


def ax_role(role: str, subrole: str = "") -> str:
    return AX_SUBROLE.get(subrole) or AX_ROLE.get(role, "other")


def web_role(tag: str, aria_role: str = "", input_type: str = "") -> str:
    r = (aria_role or "").lower()
    if r:
        return ARIA_ROLE.get(r, r if r in INTERACTIVE_ROLES | {"dialog", "text", "image"} else "other")
    tag = tag.lower()
    if tag == "input":
        t = (input_type or "text").lower()
        return {"checkbox": "checkbox", "radio": "radio", "submit": "button", "button": "button",
                "reset": "button", "range": "slider", "image": "button"}.get(t, "textbox")
    if tag == "dialog":
        return "dialog"
    return ARIA_ROLE.get(tag, "other")


# ---------------------------------------------------------------- 统一收尾
def finalize(raws: Iterable[dict[str, Any]], screen: tuple[int, int], max_elements: int = 200,
             include_text: bool = True, include_offscreen: bool = True,
             min_size: int = 2) -> tuple[list[UIElement], str]:
    """raw dict → UIElement；返回 (元素列表, 可见文本)。

    规则：
    - 宽或高 < min_size 的丢弃；与屏幕无交集的标记 offscreen（Web 长页面需要它来触发“滚动到可见”恢复）
    - 只保留可交互角色 + 对话框；静态文字只进入可见文本（include_text=True 时也作为元素保留少量，便于 L1 核验）
    - (role, name, rect) 完全相同的去重
    """
    sw, sh = screen
    out: list[UIElement] = []
    texts: list[str] = []
    seen = set()
    for r in raws:
        l, t, rr, b = (int(round(v)) for v in r["rect"])
        if rr - l < min_size or b - t < min_size:
            continue
        name = re.sub(r"\s+", " ", str(r.get("name") or "")).strip()[:100]
        value = r.get("value")
        value = None if value in (None, "") else str(value)[:100]
        role = r.get("role", "other")
        off = rr <= 0 or b <= 0 or l >= sw or t >= sh
        if name and not off and role in {"text", "image", "other", "group"}:
            texts.append(name)
        keep = role in INTERACTIVE_ROLES or role == "dialog" or \
            (include_text and role == "text" and name and not off)
        if not keep or (off and not include_offscreen):
            continue
        key = (role, name, l, t, rr, b)
        if key in seen:
            continue
        seen.add(key)
        if not off:  # 裁到屏幕内，避免中心点落在屏幕外
            l, t, rr, b = max(0, l), max(0, t), min(sw, rr), min(sh, b)
        out.append(UIElement(
            id=len(out), name=name, role=role, rect=(l, t, rr, b), enabled=bool(r.get("enabled", True)),
            focused=bool(r.get("focused", False)), value=value, checked=r.get("checked"), offscreen=off,
            native_role=str(r.get("native_role", "")), attrs=dict(r.get("attrs") or {}),
        ))
        if len(out) >= max_elements:
            break
    return out, "\n".join(dict.fromkeys(texts))


# ---------------------------------------------------------------- Android
_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def parse_android_bounds(s: str) -> Optional[tuple[int, int, int, int]]:
    m = _BOUNDS.match(s or "")
    return tuple(int(v) for v in m.groups()) if m else None  # type: ignore[return-value]


def android_raws(xml: str) -> list[dict[str, Any]]:
    # uiautomator dump 输出前有时带 "UI hierchary dumped to:" 之类噪声
    start = xml.find("<?xml") if "<?xml" in xml else xml.find("<hierarchy")
    root = ET.fromstring(xml[start:] if start >= 0 else xml)
    raws = []

    def desc_text(node) -> str:
        """可点击容器（如设置列表项）本身没有文字时，用子孙节点的文字命名（前两段）。"""
        parts = []
        for c in node.iter("node"):
            if c is node:
                continue
            t = c.attrib.get("text") or c.attrib.get("content-desc")
            if t:
                parts.append(t)
            if len(parts) >= 2:
                break
        return " | ".join(parts)

    for n in root.iter("node"):
        a = n.attrib
        rect = parse_android_bounds(a.get("bounds", ""))
        if rect is None:
            continue
        clickable = a.get("clickable") == "true" or a.get("long-clickable") == "true"
        cls = a.get("class", "")
        role = android_role(cls, clickable)
        if a.get("checkable") == "true" and role == "button":
            role = "checkbox"
        text, desc = a.get("text", ""), a.get("content-desc", "")
        rid = a.get("resource-id", "")
        is_edit = role == "textbox"
        name = (desc if is_edit and desc else "") or text or desc or (desc_text(n) if clickable else "") or \
            (rid.split("/")[-1] if rid else "")
        raws.append({
            "name": name, "role": role, "native_role": cls, "rect": rect,
            "enabled": a.get("enabled", "true") == "true", "focused": a.get("focused") == "true",
            "value": text if is_edit else None,
            "checked": (a.get("checked") == "true") if a.get("checkable") == "true" else None,
            "attrs": {k: v for k, v in (("resource-id", rid), ("package", a.get("package", "")),
                                        ("scrollable", a.get("scrollable", "")),
                                        ("password", a.get("password", ""))) if v and v != "false"},
        })
    return raws


def android_xml_to_elements(xml: str, screen: tuple[int, int], max_elements: int = 200):
    return finalize(android_raws(xml), screen, max_elements)


# ---------------------------------------------------------------- AT-SPI (Linux)
def atspi_raws(node: dict[str, Any], scale: float = 1.0, depth: int = 0, max_depth: int = 40) -> list[dict]:
    """node: {"role": "push button", "name": "OK", "extents": [x, y, w, h], "states": [...],
              "text": "...", "value": ..., "children": [...]}（extents 为屏幕坐标）"""
    out = []
    if depth > max_depth:
        return out
    states = {s.lower() for s in node.get("states", [])}
    ext = node.get("extents")
    if ext and len(ext) == 4 and ("showing" in states or not states):   # AT-SPI：showing 才是真正在屏幕上
        x, y, w, h = ext
        native = str(node.get("role", "")).lower()
        role = ATSPI_ROLE.get(native, "other")
        name = node.get("name") or (node.get("text") if role == "text" else "") or \
            ("text field" if role == "textbox" else "")
        interactive = role in INTERACTIVE_ROLES
        out.append({
            "name": name, "role": role, "native_role": native,
            "rect": (x * scale, y * scale, (x + w) * scale, (y + h) * scale),
            "enabled": ("enabled" in states or "sensitive" in states) if (states and interactive) else True,
            "focused": "focused" in states,
            "value": node.get("text") if role == "textbox" else node.get("value"),
            "checked": ("checked" in states) if role in {"checkbox", "radio", "switch"} else None,
            "attrs": {"password": "true"} if native == "password text" else {},
        })
    for c in node.get("children", []) or []:
        out.extend(atspi_raws(c, scale, depth + 1, max_depth))
    return out


def atspi_tree_to_elements(tree: dict, screen: tuple[int, int], scale: float = 1.0, max_elements: int = 200):
    return finalize(atspi_raws(tree, scale), screen, max_elements)


# ---------------------------------------------------------------- AX (macOS)
def ax_raws(node: dict[str, Any], scale: float = 1.0, depth: int = 0, max_depth: int = 40) -> list[dict]:
    """node: {"AXRole": "AXButton", "AXSubrole": "", "AXTitle": "OK", "AXDescription": "",
              "AXValue": ..., "AXPosition": [x, y], "AXSize": [w, h], "AXEnabled": true,
              "AXFocused": false, "children": [...]}（坐标单位为 point，scale=Retina 倍率）"""
    out = []
    if depth > max_depth:
        return out
    pos, size = node.get("AXPosition"), node.get("AXSize")
    if pos and size:
        native = str(node.get("AXRole", ""))
        role = ax_role(native, str(node.get("AXSubrole", "") or ""))
        val = node.get("AXValue")
        name = node.get("AXTitle") or node.get("AXDescription") or node.get("AXHelp") or \
            (val if role == "text" and isinstance(val, str) else "") or ""
        checked = None
        if role in {"checkbox", "radio", "switch"} and val is not None:
            checked = bool(int(val)) if str(val).isdigit() else bool(val)
        out.append({
            "name": name, "role": role, "native_role": native,
            "rect": (pos[0] * scale, pos[1] * scale, (pos[0] + size[0]) * scale, (pos[1] + size[1]) * scale),
            "enabled": node.get("AXEnabled", True) is not False, "focused": bool(node.get("AXFocused")),
            "value": val if role in {"textbox", "combobox", "slider"} else None, "checked": checked,
            "attrs": {"password": "true"} if native == "AXSecureTextField" else {},
        })
    for c in node.get("children", []) or []:
        out.extend(ax_raws(c, scale, depth + 1, max_depth))
    return out


def ax_tree_to_elements(tree: dict, screen: tuple[int, int], scale: float = 1.0, max_elements: int = 200):
    return finalize(ax_raws(tree, scale), screen, max_elements)


# ---------------------------------------------------------------- Web DOM 快照
def web_raws(items: list[dict[str, Any]]) -> list[dict]:
    """items 来自 env/web.py 的 JS：{tag, role, type, name, value, rect:[l,t,r,b], disabled, focused, checked, gid}"""
    out = []
    for it in items:
        role = web_role(it.get("tag", ""), it.get("role", ""), it.get("type", ""))
        out.append({
            "name": it.get("name", ""), "role": role, "native_role": it.get("role") or it.get("tag", ""),
            "rect": it["rect"], "enabled": not it.get("disabled"), "focused": bool(it.get("focused")),
            "value": it.get("value"), "checked": it.get("checked"),
            "attrs": {k: v for k, v in (("gid", it.get("gid")), ("type", it.get("type")),
                                        ("href", it.get("href"))) if v},
        })
    return out

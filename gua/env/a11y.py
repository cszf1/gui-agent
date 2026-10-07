"""各平台无障碍树 → 统一 UIElement 列表。

纯 Python、无平台依赖，所以能在任何机器上用 fixture 做单元测试：
- Android：`uiautomator dump` 产生的 XML（bounds="[l,t][r,b]"）
- Linux：AT-SPI（pyatspi）节点先被 env/linux.py 序列化成 dict 树，再在这里转换
- macOS：AX API（AXUIElement）节点先被 env/macos.py 序列化成 dict 树，再在这里转换
- Windows：UIA ControlTypeName 映射
- Web：DOM 快照（env/web.py 注入 JS 得到的扁平列表）

统一流程：raw dict 列表 → finalize()：裁剪到屏幕、标记 offscreen、去重、过滤、编号。

v0.3：raw dict 新增 is_password（审查条目 11），finalize 带入 UIElement.is_password；
Windows 的 UIA 控件 → raw 的转换抽成纯函数 uia_raw()，可用假控件离线测试。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Iterable, Optional

from .base import INTERACTIVE_ROLES, SAFE_PASSWORD_NAME, UIElement

# 密码元素 attrs 中可能夹带内容副本的键：公共序列化层对密码元素一律剔除这些键（不剔 password / type /
# resource-id 等身份属性，安全闸门仍需要它们）。v0.3.1 审查条目 2 / 3。
SENSITIVE_ATTR_KEYS = frozenset({"value", "text", "content", "content-desc", "content_desc", "axvalue",
                                 "ax_title", "ax_description", "ax_help", "label", "secret"})

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


# 忙碌 / 进度指示器的平台原始角色（v0.3.1：finalize 保留它们，供收尾核验判断“仍在加载”）
PROGRESS_ROLES = {"progressbar", "ProgressBar", "AXProgressIndicator", "AXBusyIndicator", "progress bar",
                  "android.widget.ProgressBar"}


def is_busy_raw(r: dict) -> bool:
    nr = str(r.get("native_role", ""))
    return nr in PROGRESS_ROLES or nr.endswith(".ProgressBar") or (r.get("attrs") or {}).get("aria-busy") == "true"


# ---------------------------------------------------------------- Windows UIA
UIA_SKIP_ROLES = {"group", "other", "window", "list", "menu", "scrollbar"}


def uia_raw(ctrl: Any, offset: tuple[int, int] = (0, 0)) -> Optional[dict[str, Any]]:
    """uiautomation.Control → raw dict（None = 跳过）。IsPassword 对应 UIA IsPasswordProperty。"""
    native = str(ctrl.ControlTypeName).replace("Control", "")
    role = UIA_ROLE.get(native, "other")
    if role in UIA_SKIP_ROLES and native != "ProgressBar":
        return None
    r = ctrl.BoundingRectangle
    if r.width() <= 0 or r.height() <= 0 or ctrl.IsOffscreen:
        return None
    ox, oy = offset
    is_pw = False
    try:
        is_pw = bool(getattr(ctrl, "IsPassword", False))
    except Exception:
        pass
    val = None
    if not is_pw:                     # 密码框的值不读取、不进入日志/提示词
        try:
            vp = ctrl.GetValuePattern()
            val = vp.Value[:80] if vp else None
        except Exception:
            pass
    checked = None
    if role in {"checkbox", "radio"}:
        try:
            checked = ctrl.GetTogglePattern().ToggleState == 1
        except Exception:
            pass
    aid = getattr(ctrl, "AutomationId", "") or ""
    # v0.3.1（条目 2）：密码控件的 UIA Name 可能夹带明文，改用固定安全名称（AutomationId 作为非内容属性保留）
    nm = SAFE_PASSWORD_NAME if is_pw else (ctrl.Name or "")
    return {"name": nm, "role": role, "native_role": native,
            "rect": (r.left - ox, r.top - oy, r.right - ox, r.bottom - oy),
            "enabled": bool(ctrl.IsEnabled), "focused": bool(ctrl.HasKeyboardFocus),
            "value": val, "checked": checked, "is_password": is_pw,
            "attrs": {"automation_id": aid} if aid else {}}


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
    raws = list(raws)
    focus_kept = False
    for idx, r in enumerate(raws):
        l, t, rr, b = (int(round(v)) for v in r["rect"])
        if rr - l < min_size or b - t < min_size:
            continue
        is_pw = bool(r.get("is_password")) or (r.get("attrs") or {}).get("password") == "true"
        # v0.3.1（条目 2 / 3）：密码元素不保留不可信 name（Android content-desc、AXTitle、UIA / AT-SPI name……），
        # 统一换成固定安全名称；非密码元素名字照旧。
        name = SAFE_PASSWORD_NAME if is_pw else \
            re.sub(r"\s+", " ", str(r.get("name") or "")).strip()[:100]
        value = r.get("value")
        value = None if value in (None, "") or is_pw else str(value)[:100]   # v0.3.1：密码值在公共层清空
        role = r.get("role", "other")
        off = rr <= 0 or b <= 0 or l >= sw or t >= sh
        if name and not off and role in {"text", "image", "other", "group"} and not is_pw:
            texts.append(name)
        focused = bool(r.get("focused", False))
        # v0.3.1：焦点元素无论角色都保留（安全闸门要知道键盘输入 / 激活键落在哪里）
        keep = role in INTERACTIVE_ROLES or role == "dialog" or focused or (is_busy_raw(r) and not off) or \
            (include_text and role == "text" and name and not off)
        if not keep or (off and not include_offscreen):
            continue
        key = (role, name, l, t, rr, b)
        if key in seen:
            continue
        seen.add(key)
        if not off:  # 裁到屏幕内，避免中心点落在屏幕外
            l, t, rr, b = max(0, l), max(0, t), min(sw, rr), min(sh, b)
        out.append(_mk(len(out), r, name, role, (l, t, rr, b), focused, value, off, is_pw))
        focus_kept = focus_kept or focused
        if len(out) >= max_elements:
            # v0.3.1：候选数量上限不能挡住焦点探测——继续找焦点元素并追加在末尾
            if not focus_kept:
                for r2 in raws[idx + 1:]:
                    if r2.get("focused"):
                        l2, t2, r2r, b2 = (int(round(v)) for v in r2["rect"])
                        pw2 = bool(r2.get("is_password")) or (r2.get("attrs") or {}).get("password") == "true"
                        nm2 = SAFE_PASSWORD_NAME if pw2 else \
                            re.sub(r"\s+", " ", str(r2.get("name") or "")).strip()[:100]
                        v2 = r2.get("value")
                        v2 = None if v2 in (None, "") or pw2 else str(v2)[:100]
                        off2 = r2r <= 0 or b2 <= 0 or l2 >= sw or t2 >= sh
                        out.append(_mk(len(out), r2, nm2, r2.get("role", "other"), (l2, t2, r2r, b2), True, v2,
                                       off2, pw2))
                        break
            break
    return out, "\n".join(dict.fromkeys(texts))


def _safe_attrs(is_pw: bool, attrs: Any) -> dict:
    """密码元素的 attrs 去掉可能夹带内容副本的键；身份属性（password / type / resource-id……）保留。"""
    a = dict(attrs or {})
    if not is_pw:
        return a
    return {k: v for k, v in a.items() if str(k).lower() not in SENSITIVE_ATTR_KEYS}


def _mk(i, r, name, role, rect, focused, value, off, is_pw) -> UIElement:
    return UIElement(id=i, name=name, role=role, rect=rect, enabled=bool(r.get("enabled", True)),
                     focused=focused, value=value, checked=r.get("checked"), offscreen=off,
                     native_role=str(r.get("native_role", "")), attrs=_safe_attrs(is_pw, r.get("attrs")),
                     is_password=is_pw)


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
        """可点击容器（如设置列表项）本身没有文字时，用子孙节点的文字命名（前两段）。
        v0.3.1（条目 2）：密码子树整体跳过（text / content-desc 都可能夹带明文），不只是密码节点自己的 text。"""
        parts: list[str] = []

        def walk(n) -> None:
            for c in n:
                if len(parts) >= 2:
                    return
                if c.tag != "node" or c.attrib.get("password") == "true":
                    continue
                t = c.attrib.get("text") or c.attrib.get("content-desc")
                if t:
                    parts.append(t)
                    if len(parts) >= 2:
                        return
                walk(c)

        walk(node)
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
        is_pw = a.get("password") == "true"
        if is_pw:
            # v0.3.1（条目 2）：密码节点的 text / content-desc 都可能夹带明文，名字绝不用它们（也不回退 resource-id）
            text = desc = ""
        name = SAFE_PASSWORD_NAME if is_pw else \
            ((desc if is_edit and desc else "") or text or desc or
             (desc_text(n) if clickable else "") or (rid.split("/")[-1] if rid else ""))
        raws.append({
            "name": name, "role": role, "native_role": cls, "rect": rect,
            "enabled": a.get("enabled", "true") == "true", "focused": a.get("focused") == "true",
            "value": text if is_edit else None,
            "checked": (a.get("checked") == "true") if a.get("checkable") == "true" else None,
            "is_password": is_pw,
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
        is_pw = native == "password text" or "password" in states   # v0.3.1：AT-SPI 也可能用状态标记
        if is_pw:          # 条目 2：AT-SPI name / text 都可能夹带明文，改用固定安全名称
            name = SAFE_PASSWORD_NAME
        else:
            name = node.get("name") or (node.get("text") if role == "text" else "") or \
                ("text field" if role == "textbox" else "")
        interactive = role in INTERACTIVE_ROLES
        out.append({
            "name": name, "role": role, "native_role": native,
            "rect": (x * scale, y * scale, (x + w) * scale, (y + h) * scale),
            "enabled": ("enabled" in states or "sensitive" in states) if (states and interactive) else True,
            "focused": "focused" in states,
            "value": None if is_pw else (node.get("text") if role == "textbox" else node.get("value")),
            "checked": ("checked" in states) if role in {"checkbox", "radio", "switch"} else None,
            "is_password": is_pw,
            "attrs": {"password": "true"} if is_pw else {},
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
        subrole = str(node.get("AXSubrole", "") or "")
        role = ax_role(native, subrole)
        secure = native == "AXSecureTextField" or subrole == "AXSecureTextField"
        val = node.get("AXValue")
        if secure:          # v0.3.1（条目 2 / 6）：安全输入框的 AXValue / AXTitle / AXDescription / AXHelp
            val = None      # 都可能夹带明文，一律不采用，改用固定安全名称
            name = SAFE_PASSWORD_NAME
        else:
            name = node.get("AXTitle") or node.get("AXDescription") or node.get("AXHelp") or \
                (val if role == "text" and isinstance(val, str) else "")
        checked = None
        if role in {"checkbox", "radio", "switch"} and val is not None:
            checked = bool(int(val)) if str(val).isdigit() else bool(val)
        out.append({
            "name": name, "role": role, "native_role": native,
            "rect": (pos[0] * scale, pos[1] * scale, (pos[0] + size[0]) * scale, (pos[1] + size[1]) * scale),
            "enabled": node.get("AXEnabled", True) is not False, "focused": bool(node.get("AXFocused")),
            "value": val if role in {"textbox", "combobox", "slider"} else None, "checked": checked,
            "is_password": secure,
            "attrs": {"password": "true"} if secure else {},
        })
    for c in node.get("children", []) or []:
        out.extend(ax_raws(c, scale, depth + 1, max_depth))
    return out


def ax_tree_to_elements(tree: dict, screen: tuple[int, int], scale: float = 1.0, max_elements: int = 200):
    return finalize(ax_raws(tree, scale), screen, max_elements)


# ---------------------------------------------------------------- Web DOM 快照
PASSWORD_AUTOCOMPLETE = {"current-password", "new-password", "one-time-code"}
# HTML autocomplete 是「按 ASCII 空白分隔的 token 列表」：整串命中不够，要逐 token 判断（v0.3.1 条目 1）
_ASCII_WS = re.compile(r"[ \t\n\r\f\v]+")


def autocomplete_has_password_token(value: Any) -> bool:
    """autocomplete 是否含 current-password / new-password / one-time-code 任一 token（不扩大信用卡范围）。"""
    return bool({t for t in _ASCII_WS.split(str(value or "").strip().lower()) if t} & PASSWORD_AUTOCOMPLETE)


def web_raws(items: list[dict[str, Any]]) -> list[dict]:
    """items 来自 env/web.py 的 JS：{tag, role, type, name, value, rect:[l,t,r,b], disabled, focused, checked, gid}"""
    out = []
    for it in items:
        role = web_role(it.get("tag", ""), it.get("role", ""), it.get("type", ""))
        # v0.3.1（条目 1）：原始 secure（CSS -webkit-text-security 掩码等）优先级最高；否则看
        # type=password，或 autocomplete token 列表命中 current-password / new-password / one-time-code
        if it.get("secure"):
            is_pw = True
        else:
            is_pw = str(it.get("type") or "").lower() == "password" or \
                autocomplete_has_password_token(it.get("autocomplete"))
        # dom_id / form_submit_id 只作安全身份（Web / Safety 约定），与旧 form_submit 一起透传，不进模型 brief
        attrs = {}
        for k, v in (("gid", it.get("gid")), ("dom_id", it.get("dom_id")),
                     ("form_submit_id", it.get("form_submit_id")), ("type", it.get("type")),
                     ("href", it.get("href")), ("frame", it.get("frame")),
                     ("form_submit", it.get("form_submit")), ("aria-busy", it.get("busy")),
                     ("aria-valuenow", it.get("valuenow")), ("aria-valuemax", it.get("valuemax"))):
            if v is None or v == "" or v is False:
                continue
            attrs[k] = v
        out.append({
            "name": SAFE_PASSWORD_NAME if is_pw else it.get("name", ""), "role": role,
            "native_role": it.get("role") or it.get("tag", ""),
            "rect": it["rect"], "enabled": not it.get("disabled"), "focused": bool(it.get("focused")),
            "value": None if is_pw else it.get("value"),
            "checked": it.get("checked"),
            "is_password": is_pw,
            "attrs": attrs,
        })
    return out

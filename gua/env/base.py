"""跨平台环境接口：截图 + 统一无障碍元素模型 + 执行动作。

所有后端（windows / macos / linux / android / web / mock）都实现 Env：
- observe()  → Observation（截图、前台窗口/页面、统一 UIElement 列表、可见文本）
- execute()  → ExecResult（执行层错误：越界、窗口不存在、不支持的动作……）
- focus_window() / reset() / close()

统一元素模型参考 UFO² 的控件候选、browser-use 的带编号 DOM 元素、AndroidWorld 的 UI element
以及 OSWorld 的 a11y tree：一个元素 = 角色 + 名字 + 截图像素矩形 + 状态，编号 id 供模型直接引用。
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image

from ..actions import UNSUPPORTED, Action

# 跨平台统一角色名（各平台原始角色映射到这里，见 env/a11y.py）
ROLES = {"button", "link", "textbox", "checkbox", "radio", "combobox", "listitem", "menuitem", "tab",
         "treeitem", "slider", "switch", "image", "text", "dialog", "window", "menu", "list", "group",
         "scrollbar", "cell", "other"}
INTERACTIVE_ROLES = {"button", "link", "textbox", "checkbox", "radio", "combobox", "listitem", "menuitem",
                     "tab", "treeitem", "slider", "switch", "cell"}

# 密码 / 安全输入框的公共安全名称（v0.3.1 审查条目 2 / 3）：名称可能来自不可信字段
# （Android content-desc、macOS AXTitle / AXDescription / AXHelp、UIA / AT-SPI name……），转换器与公共序列化层一律用它替换。
SAFE_PASSWORD_NAME = "password field"


@dataclass
class UIElement:
    """统一元素。rect 为截图像素坐标 (left, top, right, bottom)。"""
    id: int
    name: str
    role: str
    rect: tuple[int, int, int, int]
    enabled: bool = True
    focused: bool = False
    value: Optional[str] = None
    checked: Optional[bool] = None
    offscreen: bool = False          # 在当前视口外（Web 长页面 / 列表），需要先滚动
    native_role: str = ""            # 平台原始角色（UIA ControlType / AXRole / Android class / ARIA role）
    attrs: dict = field(default_factory=dict)   # 其他平台特有信息（resource-id、package、selector……）
    # 密码 / 安全输入框（v0.3，审查条目 11）：来自 UIA IsPasswordProperty、macOS AXSecureTextField(子)角色、
    # Android password 属性、Web input[type=password]、AT-SPI "password text" 角色。安全闸门据此判断。
    is_password: bool = False

    @property
    def control_type(self) -> str:   # 兼容 v0.1
        return self.native_role or self.role

    @property
    def center(self) -> tuple[int, int]:
        l, t, r, b = self.rect
        return (l + r) // 2, (t + b) // 2

    @property
    def area(self) -> int:
        l, t, r, b = self.rect
        return max(0, r - l) * max(0, b - t)

    def contains(self, x: float, y: float) -> bool:
        l, t, r, b = self.rect
        return l <= x <= r and t <= y <= b

    def brief(self) -> str:
        # v0.3.1（条目 3）：公共序列化层兜底——密码元素不输出 name（可能来自 Android content-desc /
        # AXTitle / UIA、AT-SPI name 等不可信字段）与 value，改用固定安全名称；普通元素原样保留。
        name = SAFE_PASSWORD_NAME if self.is_password else self.name
        v = f" value={self.value!r}" if self.value and not self.is_password else ""
        flags = ("" if self.enabled else " disabled") + (" focused" if self.focused else "") + \
                (" offscreen" if self.offscreen else "") + (" password" if self.is_password else "") + \
                ("" if self.checked is None else (" checked" if self.checked else " unchecked"))
        return f"[{self.id}] {self.role} {name!r}{v}{flags}"


@dataclass
class Observation:
    screenshot: Image.Image
    timestamp: float
    screen_size: tuple[int, int]            # 截图像素
    dpi_scale: float = 1.0                  # 截图像素 / 输入坐标单位（macOS Retina = 2）
    active_window: str = ""                 # 前台窗口标题 / 当前页面标题 / Android 前台 Activity
    active_process: str = ""                # 进程名 / 包名 / 域名
    windows: list[str] = field(default_factory=list)
    elements: list[UIElement] = field(default_factory=list)
    platform: str = ""
    url: str = ""                           # web 专用
    text: str = ""                          # 可见文本（Web innerText / 无障碍树里的文字），供 L1 规则核验
    cursor: Optional[tuple[int, int]] = None  # 当前鼠标位置（截图像素），未知为 None（Claude 拖拽起点用）
    # v0.3.1：键盘焦点探测结果。"known"（elements 里有 focused 元素）| "none"（明确没有可输入焦点）|
    # "unknown"（无法确定，例如跨源 iframe 探测失败）| ""（平台未报告）。安全闸门与脱敏据此保守处理，见 gua.sensitive。
    focus_state: str = ""
    snapshot_id: str = ""                   # Executor-owned generation, not an element selector

    def element(self, eid: int) -> Optional[UIElement]:
        for e in self.elements:
            if e.id == eid:
                return e
        return None

    def element_at(self, x: float, y: float) -> Optional[UIElement]:
        hits = [e for e in self.elements if not e.offscreen and e.contains(x, y)]
        return min(hits, key=lambda e: e.area) if hits else None

    def all_text(self) -> str:
        # v0.3.1（条目 3）：密码元素的 name 可能来自不可信字段（Android content-desc / AXTitle /
        # UIA、AT-SPI name），公共兜底不再追加，也不追加其 value；普通元素不受影响。
        parts = [self.text, self.active_window]
        parts += [e.name for e in self.elements if not e.is_password]
        parts += [e.value or "" for e in self.elements if not e.is_password]
        return "\n".join(p for p in parts if p)

    def dialogs(self) -> list[UIElement]:
        return [e for e in self.elements if e.role == "dialog" and not e.offscreen]


@dataclass
class ExecResult:
    ok: bool
    error: str = ""          # 执行层错误码前缀：out_of_bounds / window_not_found / unsupported / blocked_by_safety ...
    started: float = 0.0
    finished: float = 0.0
    output: str = ""         # 例如 ask_user 的回答
    route: str = ""          # Actual execution path for measurement, not a success claim


class Env(ABC):
    platform: str = "base"
    scroll_unit_px: int = 100          # 一格滚动大约多少截图像素（恢复策略按距离计算滚动格数）
    targeted_input: bool = False
    input_epoch: int = 0

    @abstractmethod
    def observe(self, with_elements: bool = True) -> Observation: ...

    @abstractmethod
    def execute(self, action: Action) -> ExecResult: ...

    def bind_action(self, action: Action, obs: Observation) -> Action:
        """Bind to the observation used for consent/decision; supported by native/web backends."""
        return action

    def element_identity(self, element: UIElement):
        """Stable identity within an app/document, for focus-and-type verification."""
        return None

    def focus_window(self, title_substring: str) -> bool:
        return False

    def reset(self) -> None:
        pass

    def close(self) -> None:
        pass

    # ------------------------------------------------------------------
    def supports(self, action_type: str) -> bool:
        return action_type not in UNSUPPORTED.get(self.platform, set())

    @staticmethod
    def _now() -> float:
        return time.time()

    def _bounds_error(self, a: Action, w: int, h: int) -> Optional[str]:
        """统一越界检查（QQ 实测里的“点击越界”）：指针动作坐标必须落在截图范围内。"""
        pts = []
        if a.is_pointer or (a.type == "scroll" and a.x is not None):
            pts.append((a.x, a.y))
        if a.type == "drag":
            pts.append((a.x2, a.y2))
        for x, y in pts:
            if x is None or y is None:
                return "out_of_bounds (missing coordinate)"
            if not (0 <= x < w and 0 <= y < h):
                return f"out_of_bounds ({int(x)},{int(y)}) screen={w}x{h}"
        return None

    def wait_until_stable(self, timeout: float = 5.0, interval: float = 0.4,
                          threshold: float = 0.002, stable_frames: int = 2) -> tuple[Observation, bool]:
        """等待界面稳定：连续 stable_frames 帧像素差低于阈值。

        返回 (最后一帧观察, 是否稳定)。方向 A 的第一道关口：用加载中的截图做判断是时间失配的主要来源。
        """
        from ..verify.diff import frame_diff
        deadline = time.monotonic() + timeout
        prev = self.observe(with_elements=False)
        calm = 0
        while time.monotonic() < deadline:
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
            cur = self.observe(with_elements=False)
            if frame_diff(prev.screenshot, cur.screenshot) < threshold:
                calm += 1
                if calm >= stable_frames:
                    return self.observe(with_elements=True), True
            else:
                calm = 0
            prev = cur
        return self.observe(with_elements=True), False

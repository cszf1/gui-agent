"""离线 Mock 环境：可编程的“假桌面”，用于单元测试和调试 agent 控制流（三大 OS 的 CI 都跑它）。

可注入方向 A 关心的干扰：渲染延迟（点击后 N 帧才生效）、失焦、弹窗遮挡、动作静默失败、
目标在屏幕外（需要滚动）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from PIL import Image, ImageDraw

from ..actions import Action
from .base import Env, ExecResult, Observation, UIElement


@dataclass
class MockButton:
    name: str
    rect: tuple[int, int, int, int]
    on_click: Optional[Callable[["MockEnv"], None]] = None
    delay_frames: int = 0        # 点击后过多少次 observe 才生效（模拟加载）
    flaky_first: bool = False    # 第一次点击静默失败
    role: str = "button"
    danger: bool = False
    # v0.6：控件状态与“后台 / 前台”通道的可注入差异
    checked: Optional[bool] = None       # checkbox / radio
    value: Optional[str] = None          # textbox
    semantic: bool = True                # 是否暴露语义模式（Invoke / Toggle / Value）
    background_drop: bool = False        # 语义调用“成功返回”但实际没生效（Chromium 丢弃 PostMessage 一类）
    pointer_dead: bool = False           # 真实点击没有效果（被透明层吃掉），但语义调用有效


@dataclass
class MockEnv(Env):
    size: tuple[int, int] = (1280, 800)
    title: str = "Mock App"
    buttons: list[MockButton] = field(default_factory=list)
    state: dict = field(default_factory=dict)
    popup: Optional[str] = None          # 模态弹窗文字；存在时只能点 OK
    focused: bool = True
    scroll_y: int = 0                    # 视口滚动偏移（按钮 rect 为“文档坐标”）
    log: list[str] = field(default_factory=list)
    platform: str = "mock"
    _pending: list = field(default_factory=list)
    _clicked: set = field(default_factory=set)
    cursor: tuple = (0, 0)               # 真实指针位置（后台动作不应移动它）
    focused_field: Optional[str] = None
    semantic_actions = True

    def _vis_rect(self, b: MockButton) -> tuple[int, int, int, int]:
        l, t, r, bt = b.rect
        return l, t - self.scroll_y, r, bt - self.scroll_y

    def _render(self) -> Image.Image:
        img = Image.new("RGB", self.size, (240, 240, 240))
        d = ImageDraw.Draw(img)
        d.text((10, 10), f"{self.title} state={self.state}", fill=(0, 0, 0))
        for b in self.buttons:
            r = self._vis_rect(b)
            d.rectangle(r, outline=(0, 0, 0), fill=(200, 220, 255))
            label = b.name
            if b.checked is not None:
                label = ("[x] " if b.checked else "[ ] ") + label
            if b.value:
                label += f": {b.value}"
            d.text((r[0] + 4, r[1] + 4), label, fill=(0, 0, 0))
        if self.popup:
            d.rectangle((400, 300, 880, 500), fill=(255, 255, 200), outline=(0, 0, 0))
            d.text((420, 320), self.popup, fill=(0, 0, 0))
            d.rectangle((600, 440, 680, 480), fill=(180, 255, 180), outline=(0, 0, 0))
            d.text((615, 450), "OK", fill=(0, 0, 0))
        return img

    def _tick(self) -> None:
        still = []
        for n, fn in self._pending:
            if n <= 0:
                fn(self)
            else:
                still.append((n - 1, fn))
        self._pending = still

    def observe(self, with_elements: bool = True) -> Observation:
        self._tick()
        elems = []
        if with_elements:
            if self.popup:
                elems = [UIElement(0, self.popup, "dialog", (400, 300, 880, 500)),
                         UIElement(1, "OK", "button", (600, 440, 680, 480))]
            else:
                w, h = self.size
                for i, b in enumerate(self.buttons):
                    r = self._vis_rect(b)
                    off = r[3] <= 0 or r[1] >= h
                    elems.append(UIElement(i, b.name, b.role, r, offscreen=off, native_role="MockButton",
                                           checked=b.checked, value=b.value,
                                           focused=(b.role == "textbox" and self.focused_field == b.name)))
        txt = " ".join(f"{k}={v}" for k, v in self.state.items())
        return Observation(self._render(), time.time(), self.size, 1.0,
                           self.title if self.focused else "Other Window", "mock.exe",
                           [self.title, "Other Window"], elems, platform="mock", text=txt,
                           focus_state="none")   # 假桌面没有可聚焦控件：键盘输入落在“文档”上

    def pointer_position(self):
        return tuple(self.cursor)

    def foreground_token(self) -> str:
        return self.title if self.focused else "Other Window"

    def _activate(self, b: MockButton) -> None:
        if b.role in {"checkbox", "switch"} and b.checked is not None:
            b.checked = not b.checked
        elif b.role == "radio":
            b.checked = True
        elif b.role == "textbox":
            self.focused_field = b.name
        if b.on_click:
            if b.delay_frames:
                self._pending.append((b.delay_frames, b.on_click))
            else:
                b.on_click(self)

    def _semantic(self, a: Action, t0: float) -> ExecResult:
        if self.popup:
            return ExecResult(False, "background_unavailable: modal popup in front", t0, time.time())
        if a.element_id is None or not 0 <= a.element_id < len(self.buttons):
            return ExecResult(False, "stale_target: element not found", t0, time.time())
        b = self.buttons[a.element_id]
        route = f"semantic:mock:{a.method}"
        if not b.semantic:
            return ExecResult(False, "background_unavailable: control exposes no accessibility pattern", t0,
                              time.time(), route=route)
        if b.background_drop:
            return ExecResult(True, "", t0, time.time(), route=route)     # 返回成功但没有生效
        m = a.method
        if m == "invoke":
            if b.on_click:
                (self._pending.append((b.delay_frames, b.on_click)) if b.delay_frames else b.on_click(self))
        elif m == "toggle":
            if b.checked is None:
                return ExecResult(False, "unsupported: control has no toggle pattern", t0, time.time(), route=route)
            b.checked = not b.checked
            if b.on_click:
                b.on_click(self)
        elif m == "select":
            b.checked = True
            if b.on_click:
                b.on_click(self)
        elif m == "set_value":
            if b.role != "textbox":
                return ExecResult(False, "unsupported: control has no value pattern", t0, time.time(), route=route)
            b.value = a.text or ""
        elif m == "focus":
            self.focused_field = b.name if b.role == "textbox" else self.focused_field
        elif m == "scroll_into_view":
            l, t, r, bt = b.rect
            if t - self.scroll_y < 0 or bt - self.scroll_y > self.size[1]:
                self.scroll_y = max(0, t - 100)
        else:
            return ExecResult(False, f"unsupported: {m}", t0, time.time(), route=route)
        return ExecResult(True, "", t0, time.time(), route=route)

    def execute(self, a: Action) -> ExecResult:
        t0 = time.time()
        self.log.append(a.short())
        if a.type == "invoke":
            return self._semantic(a, t0)
        err = self._bounds_error(a, *self.size)
        if err:
            return ExecResult(False, err, t0, time.time())
        if a.is_pointer and a.x is not None:
            self.cursor = (int(a.x), int(a.y))
        if a.type in {"click", "double_click", "long_press"}:
            if not self.focused:
                self.focused = True  # 第一次点击只是把窗口拉到前台
                return ExecResult(True, "", t0, time.time())
            if self.popup:
                if 600 <= a.x <= 680 and 440 <= a.y <= 480:
                    self.popup = None
                return ExecResult(True, "", t0, time.time())
            for b in self.buttons:
                l, t, r, bt = self._vis_rect(b)
                if l <= a.x <= r and t <= a.y <= bt:
                    if b.flaky_first and b.name not in self._clicked:
                        self._clicked.add(b.name)
                        break
                    self._clicked.add(b.name)
                    if not b.pointer_dead:
                        self._activate(b)
                    break
        elif a.type == "type":
            field_ = next((b for b in self.buttons if b.role == "textbox" and b.name == self.focused_field), None)
            if field_ is not None:
                field_.value = ("" if a.clear else (field_.value or "")) + (a.text or "")
            self.state["typed"] = self.state.get("typed", "") + (a.text or "")
        elif a.type == "hotkey":
            if [k.lower() for k in a.keys] in (["esc"], ["escape"]):
                self.popup = None
            self.state.setdefault("hotkeys", []).append("+".join(a.keys))
        elif a.type == "scroll":
            step = a.amount * self.scroll_unit_px
            self.scroll_y = max(0, self.scroll_y + (step if a.direction == "down" else -step))
        elif a.type == "focus_window":
            if not self.focus_window(a.text or self.title):
                return ExecResult(False, f"window_not_found {a.text!r}", t0, time.time())
        elif a.type == "back":
            self.popup = None
        return ExecResult(True, "", t0, time.time())

    def focus_window(self, title_substring: str) -> bool:
        if title_substring.lower() in self.title.lower():
            self.focused = True
            return True
        return False

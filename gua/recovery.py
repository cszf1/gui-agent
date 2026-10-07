"""失败分类 + 有限恢复（方向 A 的第二半），跨平台。

原则（来自 SRTP 调研报告第 4 章）：
- 先判断“该等待还是该恢复”：IN_PROGRESS 只等待，不重复点击。
- 恢复按失败类型选择最小动作，每个子目标有恢复预算，超出则重规划或放弃。
- UNCERTAIN 时不盲目重复同一动作；撤销只在能确认上一步是误操作时执行。

平台差异只体现在“最小恢复动作”的具体形式上（见 _platform_actions）：
  REFOCUS  desktop: focus_window(任务窗口)   android: open_app(任务包名)   web: focus_window(任务页面标题)
  DISMISS  desktop/web: Esc                  android: back
  SCROLL   按目标点与屏幕中心的距离 / env.scroll_unit_px 计算滚动方向与格数（目标在屏幕外时）
  UNDO     desktop/web: ctrl+z (macOS: cmd+z)  android: 不支持
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .actions import Action
from .verify import Check, Verdict


class Strategy(str, Enum):
    WAIT = "wait"                    # 再等一会，界面在加载
    REFOCUS = "refocus"              # 恢复任务窗口焦点 / 还原最小化 / 切回任务 App 或页面
    DISMISS = "dismiss"              # 关闭意外弹窗/菜单（Esc / back），或交给 actor 读弹窗
    REGROUND_ZOOM = "reground_zoom"  # 局部放大重新定位（RegionFocus 思路）
    SCROLL_INTO_VIEW = "scroll"      # 目标在屏幕外
    UNDO = "undo"                    # 确认误操作后撤销
    REPLAN = "replan"                # 交回规划器换路径
    GIVE_UP = "give_up"


@dataclass
class RecoveryPlan:
    strategy: Strategy
    actions: list[Action] = field(default_factory=list)
    note: str = ""


@dataclass
class RecoveryPolicy:
    max_waits: int = 3
    max_recoveries_per_subgoal: int = 4
    allow_undo: bool = False
    enabled: bool = True                 # 关掉即为“无恢复”基线
    fixed_retry: bool = False            # True 即为“固定次数重试”基线：任何失败都原样重试
    platform: str = "desktop"
    scroll_unit_px: int = 100
    _waits: int = 0
    _used: int = 0
    history: list[str] = field(default_factory=list)

    def reset_subgoal(self) -> None:
        self._waits = 0
        self._used = 0

    @property
    def exhausted(self) -> bool:
        return self._used >= self.max_recoveries_per_subgoal

    # ---------------------------------------------------------------- 平台相关的最小动作
    def _refocus(self, task_window: str) -> list[Action]:
        if self.platform == "android":
            return [Action("open_app", app=task_window)] if task_window else [Action("back")]
        return [Action("focus_window", text=task_window)] if task_window else []

    def _dismiss(self) -> list[Action]:
        return [Action("back")] if self.platform == "android" else [Action("hotkey", keys=["esc"])]

    def _undo(self) -> list[Action]:
        if self.platform == "android":
            return []
        return [Action("hotkey", keys=["cmd" if self.platform == "macos" else "ctrl", "z"])]

    def _scroll_toward(self, sig: dict) -> list[Action]:
        pt, screen = sig.get("point"), sig.get("screen")
        if not pt or not screen:
            return [Action("scroll", direction="down", amount=3)]
        (x, y), (w, h) = pt, screen
        if y >= h or y < 0:
            dy = y - h / 2
            n = max(1, math.ceil(abs(dy) / self.scroll_unit_px))
            return [Action("scroll", direction="down" if dy > 0 else "up", amount=n, x=w // 2, y=h // 2)]
        dx = x - w / 2
        n = max(1, math.ceil(abs(dx) / self.scroll_unit_px))
        return [Action("scroll", direction="right" if dx > 0 else "left", amount=n, x=w // 2, y=h // 2)]

    # ---------------------------------------------------------------- 决策
    def decide(self, check: Check, action: Action, task_window: str = "",
               last_failure: Optional[str] = None) -> RecoveryPlan:
        if not self.enabled:
            return RecoveryPlan(Strategy.REPLAN, note="recovery disabled")
        if check.verdict == Verdict.IN_PROGRESS:
            if self._waits < self.max_waits:
                self._waits += 1
                plan = RecoveryPlan(Strategy.WAIT, [Action("wait", seconds=1.5 * self._waits)], "screen still changing")
                self.history.append(f"{check.verdict.value}->{plan.strategy.value}")
                return plan
            # 等太久仍不稳定，当作不确定
        if self.exhausted:
            return RecoveryPlan(Strategy.GIVE_UP if last_failure == "repeat" else Strategy.REPLAN,
                                note="recovery budget exhausted")
        self._used += 1

        if self.fixed_retry:
            plan = RecoveryPlan(Strategy.REGROUND_ZOOM, [action], "fixed retry baseline")
            self.history.append(f"{check.verdict.value}->retry")
            return plan

        sig = check.signals or {}
        err = sig.get("exec_error", "") or ""
        if "out_of_bounds" in err:
            plan = RecoveryPlan(Strategy.SCROLL_INTO_VIEW, self._scroll_toward(sig), "target off screen; scroll toward it")
        elif "blocked_by_safety" in err:
            plan = RecoveryPlan(Strategy.REPLAN, note="action refused by safety policy; choose another path")
        elif "unsupported" in err:
            plan = RecoveryPlan(Strategy.REPLAN, note=f"action not supported on {self.platform}")
        elif "window_not_found" in err or "app_not_found" in err or sig.get("focus_lost"):
            if check.verdict == Verdict.BLOCKED:
                plan = RecoveryPlan(Strategy.DISMISS, [], "unexpected window in front; actor must read and handle it")
            else:
                plan = RecoveryPlan(Strategy.REFOCUS, self._refocus(task_window), "focus lost; bring task window back")
        elif check.verdict == Verdict.BLOCKED:
            plan = RecoveryPlan(Strategy.DISMISS, [], "dialog covering target; actor must read and handle it")
        elif check.verdict == Verdict.NO_EFFECT:
            if action.point is not None and last_failure != "no_effect":
                plan = RecoveryPlan(Strategy.REGROUND_ZOOM, [], "click had no effect; re-ground with zoom")
            else:
                plan = RecoveryPlan(Strategy.REPLAN, note="repeated no-effect; try another path")
        elif check.verdict == Verdict.FAILED:
            if self.allow_undo and action.type in {"type", "hotkey", "drag"} and self._undo():
                plan = RecoveryPlan(Strategy.UNDO, self._undo(), "undo wrong edit")
            elif action.type in {"click", "right_click", "double_click", "long_press"}:
                plan = RecoveryPlan(Strategy.DISMISS, self._dismiss(), "close wrongly opened menu/page")
            else:
                plan = RecoveryPlan(Strategy.REPLAN, note="unexpected change")
        else:  # UNCERTAIN
            plan = RecoveryPlan(Strategy.REPLAN, note="uncertain state: re-observe, do not blindly repeat")
        self.history.append(f"{check.verdict.value}->{plan.strategy.value}")
        return plan

"""混合执行器（v0.6）：code/API 优先、语义（后台）动作次之、像素 GUI 兜底——且每一种都要被验证。

与 Cua Driver（后台投递，失败时返回 background_unavailable 由调用方决定是否前台）、UFO² Puppeteer
（API 优先、GUI 兜底）思路一致；本项目的差异点在于 **后台动作必须证明自己真的生效了**：

1. 安全闸门在调用这里之前已经针对“意图”（动作 + 目标元素）放行；换模态（后台 → 前台）时生成的前台动作
   会被 agent **重新送进闸门**（同一激活签名，拒绝记忆共享，换模态绕不过拒绝）。
2. 后台尝试前：目标被模态对话框挡住时不走后台（避免 DOM/UIA 调用“穿透”用户可见的弹窗）。
3. 后台尝试后：
   - 侵入检测：指针是否移动、前台窗口是否变化（`signals.pointer_moved` / `signals.focus_stolen`）；
   - 生效检测：重新观察，按语义方法自带的后置条件（toggle→勾选翻转、set_value→值相等、select→选中、
     focus→获得焦点）或“无障碍树差分 / 像素差非空”判断；
   - 没有生效证据时：**幂等**方法（set_value / select / focus / expand / collapse / scroll_into_view）可以自动
     改走前台；toggle 在短暂等待复查仍未翻转时才改走前台；**非幂等的 invoke 绝不自动重放**（可能延迟生效，
     重放会执行两次），返回 `background_no_effect` 交给恢复策略显式地换模态并重新验证。
4. `mode=gui_only`（消融基线）时，语义动作直接改写成前台等价动作执行；路由与模态都写进日志。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

from .actions import TOOL_ACTIONS, Action, modality_of
from .env.base import Env, ExecResult, Observation
from .verify.postconditions import a11y_diff, diff_is_empty, evaluate, implied_postconditions

IDEMPOTENT = {"set_value", "select", "focus", "expand", "collapse", "scroll_into_view"}


@dataclass
class HybridConfig:
    mode: str = "hybrid"               # hybrid | gui_only
    dispatch: str = "auto"             # auto | background | foreground（invoke 未指定时的默认）
    verify_background: bool = True
    fallback_to_foreground: bool = True
    modality_recovery: bool = True     # 恢复策略可在 GUI 点击无效时改用语义动作，反之亦然
    settle: float = 0.25               # 后台动作后复查前的等待（秒）

    @classmethod
    def from_config(cls, cfg: dict) -> "HybridConfig":
        h = (cfg or {}).get("hybrid") or {}
        return cls(mode=h.get("mode", "hybrid"), dispatch=h.get("dispatch", "auto"),
                   verify_background=bool(h.get("verify_background", True)),
                   fallback_to_foreground=bool(h.get("fallback_to_foreground", True)),
                   modality_recovery=bool(h.get("modality_recovery", True)),
                   settle=float(h.get("settle", 0.25)))


def foreground_equivalent(a: Action, obs: Optional[Observation]) -> Optional[Action]:
    """语义动作的前台（真实输入）等价动作；没有等价动作返回 None。"""
    el = obs.element(a.element_id) if obs is not None and a.element_id is not None else None
    m = a.method
    if m in {"invoke", "toggle", "select", "expand", "collapse", "focus"}:
        if el is None and not a.target:
            return None
        x, y = (el.center if el is not None else (None, None))
        return Action("click", x=x, y=y, element_id=a.element_id, target=a.target or (el.name if el else None),
                      reason=a.reason, expect=list(a.expect))
    if m == "set_value":
        if el is None or el.role not in {"textbox", "combobox"}:
            return None
        return Action("type", element_id=el.id, target=a.target, text=a.text, clear=True, reason=a.reason,
                      expect=list(a.expect))
    return None


@dataclass
class HybridExecutor:
    env: Env
    tools: Optional[object] = None                     # gua.tools.ToolRegistry
    cfg: HybridConfig = field(default_factory=HybridConfig)
    observe: Optional[Callable[[], Observation]] = None
    stats: dict = field(default_factory=lambda: {"modality": {}, "fallbacks": 0, "intrusions": 0,
                                                 "background_verified": 0, "background_no_effect": 0})

    def _count(self, a: Action, r: ExecResult) -> None:
        m = modality_of(a, r.route)
        r.signals.setdefault("modality", m)
        self.stats["modality"][m] = self.stats["modality"].get(m, 0) + 1

    # ------------------------------------------------------------------ 工具通道
    def run_tool(self, a: Action) -> ExecResult:
        r = self.env.run_tool(a) if a.type in {"shell", "file"} else None
        if r is None:
            if self.tools is None:
                t = time.time()
                r = ExecResult(False, f"blocked_by_safety: {a.type} tools are not configured", t, t,
                               route=f"{a.type}:denied")
            else:
                r = self.tools.execute(a)
        self._count(a, r)
        return r

    # ------------------------------------------------------------------ 语义 / 后台
    def run_semantic(self, a: Action, obs: Optional[Observation]) -> tuple[ExecResult, Optional[Action]]:
        """返回 (结果, 需要改走前台时的前台动作)。前台动作由 agent 重新过闸后执行。"""
        t0 = time.time()
        dispatch = a.dispatch or self.cfg.dispatch
        fg = foreground_equivalent(a, obs)
        if self.cfg.mode == "gui_only" or dispatch == "foreground" or not getattr(self.env, "semantic_actions",
                                                                                 False):
            why = ("gui_only ablation" if self.cfg.mode == "gui_only" else
                   "foreground requested" if dispatch == "foreground" else "backend has no semantic actions")
            if fg is None:
                return ExecResult(False, f"unsupported: no foreground equivalent for invoke {a.method} ({why})",
                                  t0, time.time(), route="semantic:none"), None
            return ExecResult(False, f"background_skipped: {why}", t0, time.time(), route="semantic:skipped",
                              signals={"fallback_reason": why}), fg
        el = obs.element(a.element_id) if obs is not None and a.element_id is not None else None
        if obs is not None and el is not None:
            dialogs = [d for d in obs.dialogs() if d.id != el.id]
            inside = any(d.contains(*el.center) for d in dialogs)
            if dialogs and not inside:
                why = f"modal dialog {dialogs[0].name!r} is in front of the target"
                r = ExecResult(False, f"background_unavailable: {why}", t0, time.time(), route="semantic:refused",
                               signals={"fallback_reason": why})
                return r, (fg if dispatch == "auto" and self.cfg.fallback_to_foreground else None)
            if el.attrs.get("covered") == "true":
                why = "target covered by another surface"
                r = ExecResult(False, f"background_unavailable: {why}", t0, time.time(), route="semantic:refused",
                               signals={"fallback_reason": why})
                return r, (fg if dispatch == "auto" and self.cfg.fallback_to_foreground else None)
        ptr0, fg0 = self.env.pointer_position(), self.env.foreground_token()
        r = self.env.execute(a)
        if not r.ok:
            err = r.error or ""
            if err.startswith(("background_unavailable", "unsupported")) and dispatch == "auto" \
                    and self.cfg.fallback_to_foreground and fg is not None:
                r.signals.setdefault("fallback_reason", err[:120])
                return r, fg
            self._count(a, r)
            return r, None
        ptr1, fg1 = self.env.pointer_position(), self.env.foreground_token()
        moved = ptr0 is not None and ptr1 is not None and tuple(ptr0) != tuple(ptr1)
        stolen = bool(fg0) and bool(fg1) and fg0 != fg1
        r.signals.update({"background": True, "pointer_moved": moved, "focus_stolen": stolen})
        if moved or stolen:
            self.stats["intrusions"] += 1
        if self.cfg.verify_background and self.observe is not None and obs is not None:
            ok, evidence = self._effect(a, obs)
            if ok is None and a.method == "toggle":
                time.sleep(self.cfg.settle * 2)      # toggle 非幂等：复查一次再决定
                ok, evidence = self._effect(a, obs)
            r.signals["background_effect"] = evidence
            if ok is False or (ok is None and a.method in IDEMPOTENT):
                self.stats["background_no_effect"] += 1
                if (a.method in IDEMPOTENT or a.method == "toggle") and dispatch == "auto" \
                        and self.cfg.fallback_to_foreground and fg is not None:
                    r2 = ExecResult(False, f"background_no_effect: {evidence}", r.started, time.time(),
                                    route=r.route, signals=dict(r.signals, fallback_reason="no observable effect"))
                    return r2, fg
                r = ExecResult(False, f"background_no_effect: {evidence}; not replayed "
                                      f"({'non-idempotent' if a.method not in IDEMPOTENT else 'fallback disabled'})",
                               r.started, time.time(), route=r.route, signals=r.signals)
            elif ok:
                self.stats["background_verified"] += 1
        self._count(a, r)
        return r, None

    def _effect(self, a: Action, before: Observation) -> tuple[Optional[bool], str]:
        """(True=有生效证据 | False=明确未生效 | None=无法判断, 证据)。"""
        time.sleep(self.cfg.settle)
        try:
            after = self.observe()
        except Exception as e:  # noqa: BLE001
            return None, f"re-observe failed ({type(e).__name__})"
        preds = implied_postconditions(a, before)
        if preds:
            rep = evaluate(preds, before, after)
            if rep.verdict == "success":
                return True, rep.evidence()
            if rep.verdict == "failed":
                return False, rep.evidence()
        d = a11y_diff(before, after)
        if not diff_is_empty(d):
            return True, "accessibility tree changed: " + "; ".join((d["added"] + d["changed"] + d["removed"])[:3])
        from .verify.diff import frame_diff
        try:
            px = frame_diff(before.screenshot, after.screenshot)
        except Exception:  # noqa: BLE001
            px = 0.0
        if px > 0.002:
            return True, f"pixels changed ({px:.4f})"
        if a.method in {"scroll_into_view", "focus"}:
            return None, "no observable change (may already have been in that state)"
        return None, "no observable change in accessibility tree or pixels"


def wrap_fallback_result(first: ExecResult, second: ExecResult) -> ExecResult:
    """把“后台尝试 + 前台回退”合并成一条结果，保留两段路由与回退原因（消融统计用）。"""
    sig = dict(first.signals)
    sig.update(second.signals)
    sig["fallback_from"] = first.route
    sig["fallback_error"] = (first.error or "")[:160]
    sig["modality"] = "gui"
    return replace(second, route=f"fallback:foreground:{second.route or 'input'}", signals=sig,
                   started=first.started)


def is_tool(a: Action) -> bool:
    return a.type in TOOL_ACTIONS

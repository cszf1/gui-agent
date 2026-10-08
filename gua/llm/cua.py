"""v0.7：原生 computer-use 模型适配器（多轮会话 + 批量动作 + 逐个验证）。

两家厂商的“电脑工具”都已经从“一轮一个动作”变成“一轮一批动作”：
- Anthropic `computer_toolset_20260801`（GA，无 beta 头）：每个动作是一个成员工具 `tool_use`（name=成员名、
  toolset_name="computer"），一轮可以有多个；按顺序执行、**第一个失败后停止**，其余块回
  `Not executed: an earlier computer action in this turn failed.`；每个结果都要回 toolset_name。
  旧版 `computer_20251124`（beta 头 computer-use-2025-11-24，enable_zoom）与 `computer_20250124` 也支持。
- OpenAI Responses API `computer` 工具（GA）：`computer_call.actions[]` 是一批动作，执行后回一个
  `computer_call_output`（截图），用 previous_response_id 续接；`pending_safety_checks` 需要开发者确认。

本项目的做法（与参考实现不同的地方）：模型给出的**每一个**可执行动作都单独交给 GUIAgent 主循环——
定位 → 安全闸门 → 执行 → 验证 → 恢复；批内第一个“执行失败 / 验证明确失败 / 无效果 / 被拦截”的动作之后，
同批剩余动作不再执行（停止于首个失败），失败原因写回模型。screenshot / zoom / cursor_position 这类只读成员在
适配器内部用当前观察回答，不消耗主循环步数；zoom 从**原始分辨率**截图裁剪（真正的放大再定位）。
修饰键点击展开为 key_down → 点击 → key_up，key_up 在失败时也会执行（不会留下按住的键）。
OpenAI 的 pending_safety_checks 从不自动确认：挂在动作上交给安全闸门，必须人工确认（deny 模式直接拒绝）。

上下文管理：会话按子目标划分（换子目标即新会话，任务与历史以文字带入）；只保留最近 keep_images 张截图，
超过 keep_images + prune_every 时**成批**删除旧截图（参考 Anthropic 文档建议，保持提示缓存前缀稳定）。

诚实说明：这里的请求/响应格式按官方文档实现，并用本地 HTTP 替身做了离线端到端测试（tests/test_v07_cua.py）；
没有调用真实 API 跑任务。
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import time
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from PIL import Image

from ..actions import Action, ActionParseError
from ..coords import ImageTransform
from .base import Budget, price_cost

HALT_TEXT = "Not executed: an earlier computer action in this turn failed."
TOOLSET = "computer_toolset_20260801"
LEGACY_BETAS = {"computer_20251124": "computer-use-2025-11-24", "computer_20250124": "computer-use-2025-01-24"}
# Members this executor cannot perform faithfully; disabled in the toolset definition and refused if called.
UNSUPPORTED_MEMBERS = {"left_mouse_down", "left_mouse_up", "hold_key", "triple_click", "middle_click"}
MAX_KEY_REPEAT = 20
_XDO = {"return": "enter", "kp_enter": "enter", "escape": "esc", "super": "win", "super_l": "win", "meta": "win",
        "cmd": "win", "command": "win", "control": "ctrl", "control_l": "ctrl", "ctrl_l": "ctrl", "alt_l": "alt",
        "shift_l": "shift", "page_down": "pagedown", "page_up": "pageup", "next": "pagedown", "prior": "pageup",
        "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right", "del": "delete"}


def norm_keys(spec) -> list[str]:
    parts = spec if isinstance(spec, list) else str(spec or "").replace(" ", "+").split("+")
    return [_XDO.get(str(k).strip().lower(), str(k).strip().lower()) for k in parts if str(k).strip()]


def fit_size(w: int, h: int, max_side: int = 1568, max_pixels: int = 1_150_000) -> tuple[int, int]:
    """缩放到模型图像上限内（长边 / 总像素），保持宽高比；不放大。"""
    s = min(1.0, max_side / max(w, h), math.sqrt(max_pixels / float(w * h)))
    return max(1, int(w * s)), max(1, int(h * s))


def png_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


@dataclass
class _Item:
    action: Action
    ref: str                   # provider block / call id
    cleanup: bool = False      # key_up after a modifier click: runs even when the batch failed


@dataclass
class _Batch:
    refs: list = field(default_factory=list)            # block ids in order
    queue: list = field(default_factory=list)           # pending _Item for the current block
    results: dict = field(default_factory=dict)         # ref -> (ok, text | content list)
    failed: str = ""
    current: str = ""


class _BatchActor:
    """把一批模型动作逐个交给主循环；记录每个动作的验证结论，首个失败后停止。"""

    halt_on = {"failed", "blocked", "no_effect"}

    def __init__(self):
        self.batch: Optional[_Batch] = None
        self.inflight: Optional[_Item] = None
        self.sg_id: Any = None
        self.obs = None
        self.cursor: Optional[tuple[float, float]] = None
        self.stats = {"model_turns": 0, "actions": 0, "internal": 0, "halted": 0, "pruned_images": 0}

    # ---- called by GUIAgent after each step's verification
    def record_outcome(self, action: Action, res, check) -> None:
        item, self.inflight = self.inflight, None
        if item is None or self.batch is None:
            return
        verdict = getattr(getattr(check, "verdict", None), "value", str(getattr(check, "verdict", "")))
        halt = set(self.halt_on)
        if verdict == "no_effect":
            # Focusing a text field often has no visible effect; only an activation (button,
            # link, submit, toggle ...) without an effect stops the rest of the batch.
            from ..hybrid import uncertain_activation
            if not uncertain_activation(action, self.obs):
                halt.discard("no_effect")
        ok = bool(getattr(res, "ok", False)) and verdict not in halt
        if ok and verdict == "no_effect":
            self.batch.results.setdefault(item.ref + ":note", (True, "verifier: no visible effect"))
        if item.cleanup:
            return
        if not ok and not self.batch.failed:
            why = (getattr(res, "error", "") or "") if not getattr(res, "ok", False) else \
                f"{verdict}: {getattr(check, 'evidence', '')}"
            self.batch.failed = item.ref
            self.batch.results[item.ref] = (False, f"Error: {why}"[:600])
            self.batch.queue = [i for i in self.batch.queue if i.cleanup]
        elif ok and verdict == "uncertain":
            self.batch.results.setdefault(item.ref + ":note", (True, f"verifier: uncertain ({getattr(check, 'evidence', '')})"[:300]))

    def _unrecorded(self, feedback: str) -> None:
        """An action we returned never reached execution (invalid / grounding failed): treat as failure."""
        if self.inflight is not None and self.batch is not None and not self.inflight.cleanup:
            ref = self.inflight.ref
            if not self.batch.failed:
                self.batch.failed = ref
                self.batch.results[ref] = (False, f"Error: not executed ({feedback or 'rejected by the executor'})"[:600])
                self.batch.queue = [i for i in self.batch.queue if i.cleanup]
        self.inflight = None

    def _pop(self) -> Optional[Action]:
        b = self.batch
        if b and b.queue:
            item = b.queue.pop(0)
            self.inflight = item
            self.stats["actions"] += 1
            if item.action.x is not None and item.action.type in {"click", "double_click", "right_click", "move",
                                                                    "scroll", "drag"}:
                self.cursor = (item.action.x2, item.action.y2) if item.action.type == "drag" else \
                    (item.action.x, item.action.y)
            return item.action
        return None

    @staticmethod
    def _with_modifiers(action: Action, mods: list[str], ref: str) -> list[_Item]:
        if not mods:
            return [_Item(action, ref)]
        return [_Item(Action("key_down", keys=list(mods)), ref), _Item(action, ref),
                _Item(Action("key_up", keys=list(mods)), ref, cleanup=True)]


# ============================================================================ Anthropic
CLAUDE_SYSTEM = """You are operating a {platform} computer to complete ONE sub-goal of a larger task.
Every action you take is executed and then independently verified; if an action fails, the rest of that batch is
not executed and you are told why. Prefer keyboard shortcuts for dropdowns and scrolling lists. Use zoom to read small
text before clicking dense UI. End each group of actions with a screenshot so you can check the result.
When the sub-goal is complete reply with text starting with DONE (no tool call). If it is impossible reply
FAIL: <reason>. If you need information only the user has, reply ASK: <question>."""


class ClaudeComputerActor(_BatchActor):
    """Claude computer-use：toolset_20260801（默认）/ computer_20251124 / computer_20250124。"""

    def __init__(self, llm, platform: str = "linux", tool_version: str = TOOLSET, enable_zoom: bool = True,
                 keep_images: int = 3, prune_every: int = 5, max_internal_turns: int = 4,
                 max_side: int = 1568, max_pixels: int = 1_150_000):
        super().__init__()
        if tool_version not in {TOOLSET, *LEGACY_BETAS}:
            raise ValueError(f"unknown computer tool version {tool_version!r}")
        self.llm = llm
        self.platform = platform
        self.version = tool_version
        self.enable_zoom = enable_zoom and tool_version != "computer_20250124"
        self.keep_images, self.prune_every = keep_images, prune_every
        self.max_internal_turns = max_internal_turns
        self.max_side, self.max_pixels = max_side, max_pixels
        self.messages: list[dict] = []
        self.tf: Optional[ImageTransform] = None
        self._awaiting_text_reply = False

    # ---- request pieces
    def tool_def(self) -> dict:
        if self.version == TOOLSET:
            configs = {m: {"enabled": False} for m in sorted(UNSUPPORTED_MEMBERS)}
            if not self.enable_zoom:
                configs["zoom"] = {"enabled": False}
            return {"type": TOOLSET, "configs": configs}
        w, h = self.tf.sent_size if self.tf is not None else (1280, 800)
        d = {"type": self.version, "name": "computer", "display_width_px": w, "display_height_px": h}
        if self.version == "computer_20251124" and self.enable_zoom:
            d["enable_zoom"] = True
        return d

    @property
    def betas(self) -> list[str]:
        return [LEGACY_BETAS[self.version]] if self.version in LEGACY_BETAS else []

    def _shot(self, obs) -> dict:
        w, h = obs.screenshot.size
        tw, th = fit_size(w, h, self.max_side, self.max_pixels)
        self.tf = ImageTransform((w, h), (tw, th), "pixel", dpi_scale=getattr(obs, "dpi_scale", 1.0))
        img = obs.screenshot if (tw, th) == (w, h) else obs.screenshot.resize((tw, th), Image.LANCZOS)
        return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png_b64(img)}}

    def _xy(self, c) -> tuple[float, float]:
        if not isinstance(c, (list, tuple)) or len(c) != 2 or not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in c):
            raise ActionParseError("bad_type", f"coordinate must be [x, y], got {c!r}", "coordinate")
        return self.tf.model_to_screenshot(c[0], c[1]) if self.tf is not None else (float(c[0]), float(c[1]))

    def _payload(self) -> dict:
        p = {"model": self.llm.model, "max_tokens": self.llm.max_tokens,
             "system": CLAUDE_SYSTEM.format(platform=self.platform), "messages": self.messages,
             "tools": [self.tool_def()]}
        return p

    def _prune(self) -> None:
        """成批删除旧截图：总数超过 keep + prune_every 时只保留最近 keep 张。"""
        spots = []
        for m in self.messages:
            if m["role"] != "user" or not isinstance(m["content"], list):
                continue
            for blk in m["content"]:
                if blk.get("type") == "image":
                    spots.append((m["content"], blk))
                elif blk.get("type") == "tool_result" and isinstance(blk.get("content"), list):
                    spots += [(blk["content"], b) for b in blk["content"] if b.get("type") == "image"]
        if len(spots) <= self.keep_images + self.prune_every:
            return
        for holder, blk in spots[:len(spots) - self.keep_images]:
            holder[holder.index(blk)] = {"type": "text", "text": "[earlier screenshot removed to save context]"}
            self.stats["pruned_images"] += 1

    # ---- conversation
    def _start(self, task, sg, total, obs, history, milestones, feedback) -> None:
        self.sg_id, self.batch, self.inflight = sg.id, None, None
        text = (f"Overall task: {task}\nCurrent sub-goal ({sg.id}/{total}): {sg.goal}\n"
                f"Expected after this sub-goal: {sg.expected or '-'}\nDone milestones:\n{milestones}\n"
                f"Recent steps:\n{history}\n" + (f"Verifier feedback: {feedback}\n" if feedback else "") +
                "Current screen:")
        self.messages = [{"role": "user", "content": [{"type": "text", "text": text}, self._shot(obs)]}]

    def _close_turn(self, obs, feedback: str) -> None:
        b = self.batch
        content: list[dict] = []
        if b is not None and b.refs:
            for ref in b.refs:
                ok, body = b.results.get(ref, (False, HALT_TEXT))
                note = b.results.get(ref + ":note")
                if isinstance(body, str):
                    body = [{"type": "text", "text": body + (f" ({note[1]})" if note else "")}]
                r = {"type": "tool_result", "tool_use_id": ref, "content": body}
                if not ok:
                    r["is_error"] = True
                if self.version == TOOLSET:
                    r["toolset_name"] = "computer"
                content.append(r)
            # Attach the current screen to the last result so the model always sees the outcome.
            last = content[-1]
            last["content"] = list(last["content"]) + [self._shot(obs)]
            if feedback:
                content.append({"type": "text", "text": f"Verifier feedback: {feedback}"})
        else:
            content = [{"type": "text", "text": (f"Verifier feedback: {feedback}\n" if feedback else "") +
                        "Current screen:"}, self._shot(obs)]
        self.messages.append({"role": "user", "content": content})
        self.batch = None
        self._prune()

    def _member(self, blk: dict) -> tuple[str, dict]:
        inp = dict(blk.get("input") or {})
        if self.version == TOOLSET:
            return str(blk.get("name")), inp
        return str(inp.pop("action", "")), inp

    def _is_computer(self, blk: dict) -> bool:
        if blk.get("type") != "tool_use":
            return False
        return blk.get("toolset_name") == "computer" if self.version == TOOLSET else blk.get("name") == "computer"

    def _map(self, name: str, inp: dict, ref: str):
        """→ ("internal", content) 或 ("actions", [_Item])；不支持的成员抛 ActionParseError。"""
        obs = self.obs
        if name in UNSUPPORTED_MEMBERS:
            raise ActionParseError("unsupported", f"{name} is not supported by this executor")
        if name == "screenshot":
            return "internal", [self._shot(obs)]
        if name == "zoom":
            if not self.enable_zoom:
                raise ActionParseError("unsupported", "zoom is disabled")
            region = inp.get("region")
            if not isinstance(region, (list, tuple)) or len(region) != 4:
                raise ActionParseError("bad_type", "zoom needs region [x0, y0, x1, y1]", "region")
            x0, y0 = self._xy(region[:2])
            x1, y1 = self._xy(region[2:])
            W, H = obs.screenshot.size
            box = (max(0, int(min(x0, x1))), max(0, int(min(y0, y1))), min(W, int(math.ceil(max(x0, x1)))),
                   min(H, int(math.ceil(max(y0, y1)))))
            if box[2] - box[0] < 2 or box[3] - box[1] < 2:
                raise ActionParseError("bad_value", "zoom region is empty or outside the screen", "region")
            crop = obs.screenshot.crop(box)           # full-resolution pixels, not the downscaled screenshot
            lim = self.tf.sent_size if self.tf is not None else crop.size
            s = min(lim[0] / crop.width, lim[1] / crop.height)
            if s != 1:
                crop = crop.resize((max(1, int(crop.width * s)), max(1, int(crop.height * s))), Image.LANCZOS)
            return "internal", [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                             "data": png_b64(crop)}}]
        if name == "cursor_position":
            cur = getattr(obs, "cursor", None) or self.cursor
            if cur is None:
                raise ActionParseError("unknown", "cursor position is unknown on this platform")
            mx, my = self.tf.screenshot_to_model(*cur) if self.tf is not None else cur
            return "internal", f"X={int(mx)}, Y={int(my)}"
        mods = norm_keys(inp.get("text")) if name in {"left_click", "right_click", "double_click", "scroll",
                                                       "left_click_drag"} and inp.get("text") else []
        if name in {"left_click", "right_click", "double_click", "mouse_move"}:
            if inp.get("coordinate") is not None:
                x, y = self._xy(inp["coordinate"])
            elif name != "mouse_move" and (getattr(obs, "cursor", None) or self.cursor):
                x, y = getattr(obs, "cursor", None) or self.cursor
            else:
                raise ActionParseError("missing_field", f"{name} needs coordinate (cursor unknown)", "coordinate")
            t = {"left_click": "click", "right_click": "right_click", "double_click": "double_click",
                 "mouse_move": "move"}[name]
            return "actions", self._with_modifiers(Action(t, x=x, y=y), mods, ref)
        if name == "left_click_drag":
            if inp.get("start_coordinate") is None or inp.get("coordinate") is None:
                start = getattr(obs, "cursor", None) or self.cursor
                if inp.get("coordinate") is None or start is None:
                    raise ActionParseError("missing_field", "left_click_drag needs start_coordinate and coordinate")
                sx, sy = start
            else:
                sx, sy = self._xy(inp["start_coordinate"])
            ex, ey = self._xy(inp["coordinate"])
            return "actions", self._with_modifiers(Action("drag", x=sx, y=sy, x2=ex, y2=ey), mods, ref)
        if name == "scroll":
            kw = {}
            if inp.get("coordinate") is not None:
                kw["x"], kw["y"] = self._xy(inp["coordinate"])
            a = Action("scroll", direction=str(inp.get("scroll_direction") or "down"),
                       amount=int(inp.get("scroll_amount") or 3), **kw)
            return "actions", self._with_modifiers(a, mods, ref)
        if name == "type":
            return "actions", [_Item(Action("type", text=str(inp.get("text") or "")), ref)]
        if name == "key":
            keys = norm_keys(inp.get("text"))
            n = int(inp.get("repeat") or 1)
            if not 1 <= n <= MAX_KEY_REPEAT:
                raise ActionParseError("out_of_range", f"repeat must be 1..{MAX_KEY_REPEAT} in this executor")
            return "actions", [_Item(Action("hotkey", keys=list(keys)), ref) for _ in range(n)]
        if name == "wait":
            return "actions", [_Item(Action("wait", seconds=min(float(inp.get("duration") or 1.0), 30.0)), ref)]
        raise ActionParseError("unknown_action", f"unsupported computer action {name!r}")

    def _next_from_batch(self) -> Optional[Action]:
        b = self.batch
        if b is None:
            return None
        while True:
            a = self._pop()
            if a is not None:
                return a
            if b.current and b.current not in b.results:
                b.results[b.current] = (True, "OK")
            b.current = ""
            pending = [r for r in b.refs if r not in b.results]
            if not pending:
                return None
            ref = pending[0]
            if b.failed:
                for r in pending:
                    b.results[r] = (False, HALT_TEXT)
                    self.stats["halted"] += 1
                return None
            blk = self._blocks[ref]
            name, inp = self._member(blk)
            try:
                kind, payload = self._map(name, inp, ref)
                if kind == "actions":
                    for it in payload:
                        it.action.validate()
            except (ActionParseError, ValueError, TypeError) as e:
                b.results[ref] = (False, f"Error: {e}"[:400])
                b.failed = ref
                continue
            if kind == "internal":
                self.stats["internal"] += 1
                b.results[ref] = (True, payload)
                continue
            b.current, b.queue = ref, list(payload)

    def _final(self, text: str) -> Action:
        up = text.strip().upper()
        self._awaiting_text_reply = True
        if up.startswith("FAIL"):
            return Action("fail", text=text.strip()[5:].strip(), reason=text)
        if up.startswith("ASK"):
            return Action("ask_user", text=text.strip()[4:].strip() or "?", reason=text)
        return Action("done", text=text.strip(), reason=text)

    def next_action(self, task, sg, total, obs, history, milestones, feedback="", notes="(none)"):
        self.obs = obs
        if getattr(obs, "cursor", None) is not None:
            self.cursor = tuple(obs.cursor)
        if sg.id != self.sg_id or not self.messages:
            self._start(task, sg, total, obs, history, milestones, feedback)
            need_model = True
        else:
            if self.inflight is not None:
                self._unrecorded(feedback)
            a = self._next_from_batch()
            if a is not None:
                return a, "continuing the model's action batch"
            self._close_turn(obs, feedback)
            need_model = True
        thought = ""
        for _ in range(self.max_internal_turns + 1):
            if not need_model:
                a = self._next_from_batch()
                if a is not None:
                    return a, thought
                self._close_turn(obs, "")
            resp = self.llm.post(self._payload(), betas=self.betas)
            self.stats["model_turns"] += 1
            content = resp.get("content") or []
            self.messages.append({"role": "assistant", "content": content})
            thought = "".join(c.get("text", "") for c in content if c.get("type") == "text").strip()
            blocks = [c for c in content if self._is_computer(c)]
            if not blocks:
                return self._final(thought), thought
            self._blocks = {c["id"]: c for c in blocks}
            self.batch = _Batch(refs=[c["id"] for c in blocks])
            need_model = False
        # The model only inspected the screen (screenshot / zoom) for several turns.
        if self.batch is not None:
            self._close_turn(obs, "")
        return Action("wait", seconds=0.0, reason="model inspected the screen only; re-observe"), thought


# ============================================================================ OpenAI
OPENAI_INSTRUCTIONS = """You operate a {platform} computer for ONE sub-goal of a larger task. Each action is executed
and verified independently; if one fails the rest of that computer_call is skipped and you are told why. When the
sub-goal is complete answer with text starting with DONE. If impossible answer FAIL: <reason>; to ask the user,
ASK: <question>."""
_OAI_KEYS = {"enter": "enter", "return": "enter", "ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift",
             "cmd": "win", "meta": "win", "super": "win", "esc": "esc", "escape": "esc", "space": "space",
             "backspace": "backspace", "delete": "delete", "tab": "tab", "arrowup": "up", "arrowdown": "down",
             "arrowleft": "left", "arrowright": "right", "pageup": "pageup", "pagedown": "pagedown",
             "home": "home", "end": "end"}


class OpenAIResponsesClient:
    """最小 Responses API 客户端（标准库 urllib；离线测试时 base_url 指向本地替身）。"""

    def __init__(self, model: str, base_url: Optional[str] = None, api_key_env: str = "OPENAI_API_KEY",
                 budget: Optional[Budget] = None, role: str = "actor", timeout: float = 120.0,
                 price: Optional[tuple[float, float]] = None, extra: Optional[dict] = None):
        self.model = model
        self.url = (base_url or "https://api.openai.com/v1").rstrip("/") + "/responses"
        self.api_key = os.environ.get(api_key_env, "")
        self.budget = budget or Budget()
        self.role, self.timeout, self.price = role, timeout, price
        self.extra = dict(extra or {})

    def create(self, body: dict) -> dict:
        self.budget.before_call(self.role)
        body = dict(self.extra, **body, model=self.model)
        req = urllib.request.Request(self.url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.api_key}"})
        t0 = time.time()
        opener = urllib.request.build_opener()
        with opener.open(req, timeout=self.timeout) as r:
            resp = json.loads(r.read().decode("utf-8"))
        u = resp.get("usage") or {}
        pt, ct = int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
        self.budget.add(self.role, pt, ct, time.time() - t0, price_cost(pt, ct, self.price))
        return resp


class OpenAIComputerActor(_BatchActor):
    """OpenAI Responses `computer` 工具（GA；legacy=True 时用 computer_use_preview）。"""

    def __init__(self, client: OpenAIResponsesClient, platform: str = "linux", legacy: bool = False,
                 max_side: int = 1920, max_pixels: int = 2_400_000, max_internal_turns: int = 3,
                 environment: Optional[str] = None):
        super().__init__()
        self.client = client
        self.platform = platform
        self.legacy = legacy
        self.max_side, self.max_pixels = max_side, max_pixels
        self.max_internal_turns = max_internal_turns
        self.environment = environment or {"windows": "windows", "macos": "mac", "web": "browser"}.get(platform, "linux")
        self.prev_id: Optional[str] = None
        self.call_ids: list[str] = []
        self.checks: dict[str, list] = {}
        self.confirmed: set[str] = set()
        self.tf: Optional[ImageTransform] = None

    def _image(self, obs) -> str:
        w, h = obs.screenshot.size
        tw, th = fit_size(w, h, self.max_side, self.max_pixels)
        self.tf = ImageTransform((w, h), (tw, th), "pixel", dpi_scale=getattr(obs, "dpi_scale", 1.0))
        img = obs.screenshot if (tw, th) == (w, h) else obs.screenshot.resize((tw, th), Image.LANCZOS)
        return "data:image/png;base64," + png_b64(img)

    def tools(self) -> list[dict]:
        if not self.legacy:
            return [{"type": "computer"}]
        w, h = self.tf.sent_size if self.tf is not None else (1280, 800)
        return [{"type": "computer_use_preview", "display_width": w, "display_height": h,
                 "environment": self.environment}]

    def record_outcome(self, action: Action, res, check) -> None:
        item = self.inflight
        if item is not None and action.provider_checks and getattr(res, "ok", False):
            self.confirmed.add(item.ref)            # executed => a human approved it at the gate
        super().record_outcome(action, res, check)

    def _xy(self, x, y) -> tuple[float, float]:
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (x, y)):
            raise ActionParseError("bad_type", f"x/y must be numbers, got {x!r}, {y!r}")
        return self.tf.model_to_screenshot(x, y) if self.tf is not None else (float(x), float(y))

    def _map(self, act: dict, ref: str) -> list[_Item]:
        t = act.get("type")
        mods = norm_keys([_OAI_KEYS.get(str(k).lower(), str(k).lower()) for k in act.get("keys") or []]) \
            if t in {"click", "double_click", "scroll", "drag"} else []
        if t == "click":
            btn = act.get("button", "left")
            if btn not in {"left", "right"}:
                raise ActionParseError("unsupported", f"mouse button {btn!r} is not supported by this executor")
            x, y = self._xy(act.get("x"), act.get("y"))
            return self._with_modifiers(Action("right_click" if btn == "right" else "click", x=x, y=y), mods, ref)
        if t == "double_click":
            x, y = self._xy(act.get("x"), act.get("y"))
            return self._with_modifiers(Action("double_click", x=x, y=y), mods, ref)
        if t == "move":
            x, y = self._xy(act.get("x"), act.get("y"))
            return [_Item(Action("move", x=x, y=y), ref)]
        if t == "drag":
            path = act.get("path") or []
            if len(path) < 2:
                raise ActionParseError("bad_value", "drag path needs at least two points")
            (sx, sy), (ex, ey) = self._xy(path[0].get("x"), path[0].get("y")), self._xy(path[-1].get("x"),
                                                                                      path[-1].get("y"))
            return self._with_modifiers(Action("drag", x=sx, y=sy, x2=ex, y2=ey), mods, ref)
        if t == "scroll":
            x, y = self._xy(act.get("x"), act.get("y"))
            sx, sy = float(act.get("scroll_x") or 0), float(act.get("scroll_y") or 0)
            if abs(sy) >= abs(sx):
                direction, mag = ("down" if sy > 0 else "up"), abs(sy)
            else:
                direction, mag = ("right" if sx > 0 else "left"), abs(sx)
            return self._with_modifiers(Action("scroll", x=x, y=y, direction=direction,
                                               amount=max(1, min(100, round(mag / 100)))), mods, ref)
        if t == "keypress":
            keys = [_OAI_KEYS.get(str(k).lower(), str(k).lower()) for k in act.get("keys") or []]
            return [_Item(Action("hotkey", keys=keys), ref)]
        if t == "type":
            return [_Item(Action("type", text=str(act.get("text") or "")), ref)]
        if t == "wait":
            ms = act.get("ms")
            seconds = float(ms) / 1000.0 if isinstance(ms, (int, float)) and not isinstance(ms, bool) else 1.0
            return [_Item(Action("wait", seconds=min(max(seconds, 0.0), 30.0)), ref)]
        if t == "screenshot":
            return []
        raise ActionParseError("unknown_action", f"unsupported computer action {t!r}")

    def _send(self, input_items: list, first: bool) -> dict:
        body: dict = {"tools": self.tools(), "input": input_items, "truncation": "auto",
                      "instructions": OPENAI_INSTRUCTIONS.format(platform=self.platform)}
        if not first and self.prev_id:
            body["previous_response_id"] = self.prev_id
        resp = self.client.create(body)
        self.stats["model_turns"] += 1
        self.prev_id = resp.get("id") or self.prev_id
        return resp

    def _outputs(self, obs, feedback: str) -> list:
        items = []
        image = self._image(obs)
        b = self.batch
        for cid in self.call_ids:
            out = {"type": "computer_call_output", "call_id": cid,
                   "output": {"type": "computer_screenshot", "image_url": image, "detail": "original"}}
            if self.checks.get(cid) and cid in self.confirmed:
                out["acknowledged_safety_checks"] = self.checks[cid]   # only after a human approved
            items.append(out)
        notes = []
        if b is not None and b.failed:
            notes.append(f"An action failed: {b.results.get(b.failed, (False, ''))[1]}. "
                         "The remaining actions of that call were NOT executed.")
        if feedback:
            notes.append(f"Verifier feedback: {feedback}")
        if notes:
            items.append({"role": "user", "content": [{"type": "input_text", "text": " ".join(notes)}]})
        return items

    def _declined(self) -> bool:
        b = self.batch
        return bool(b and b.failed and self.checks.get(b.failed) and b.failed not in self.confirmed)

    def _parse(self, resp: dict) -> Optional[tuple[Action, str]]:
        calls = [o for o in resp.get("output") or [] if o.get("type") == "computer_call"]
        text = " ".join(c.get("text", "") for o in resp.get("output") or [] if o.get("type") == "message"
                        for c in o.get("content") or [] if c.get("type") in {"output_text", "text"}).strip()
        if not calls:
            up = text.upper()
            if up.startswith("FAIL"):
                return Action("fail", text=text[5:].strip(), reason=text), text
            if up.startswith("ASK"):
                return Action("ask_user", text=text[4:].strip() or "?", reason=text), text
            return Action("done", text=text, reason=text), text
        self.call_ids = [c["call_id"] for c in calls]
        self.batch = _Batch(refs=list(self.call_ids))
        for c in calls:
            cid = c["call_id"]
            self.checks[cid] = list(c.get("pending_safety_checks") or [])
            acts = c.get("actions") or ([c["action"]] if c.get("action") else [])
            try:
                items = [it for act in acts for it in self._map(act, cid)]
                for it in items:
                    it.action.validate()
                    if self.checks[cid]:
                        it.action.provider_checks = list(self.checks[cid])
            except (ActionParseError, ValueError, TypeError) as e:
                self.batch.results[cid] = (False, f"Error: {e}"[:400])
                self.batch.failed = cid
                break
            self.batch.queue += items
        return None

    def next_action(self, task, sg, total, obs, history, milestones, feedback="", notes="(none)"):
        self.obs = obs
        first = sg.id != self.sg_id or self.prev_id is None
        if first:
            self.sg_id, self.batch, self.inflight, self.call_ids, self.confirmed = sg.id, None, None, [], set()
            text = (f"Overall task: {task}\nCurrent sub-goal ({sg.id}/{total}): {sg.goal}\n"
                    f"Expected after this sub-goal: {sg.expected or '-'}\nDone milestones:\n{milestones}\n"
                    f"Recent steps:\n{history}\n" + (f"Verifier feedback: {feedback}\n" if feedback else ""))
            inp = [{"role": "user", "content": [{"type": "input_text", "text": text},
                                                {"type": "input_image", "image_url": self._image(obs),
                                                 "detail": "original"}]}]
        else:
            if self.inflight is not None:
                self._unrecorded(feedback)
            a = self._pop()
            if a is not None:
                return a, "continuing the model's computer_call"
            if self._declined():
                # A provider safety check was refused: never acknowledge it; restart the chain.
                reason = self.batch.results[self.batch.failed][1]
                self.prev_id, self.call_ids, self.batch = None, [], None
                inp = [{"role": "user", "content": [
                    {"type": "input_text", "text": f"Overall task: {task}\nCurrent sub-goal: {sg.goal}\n"
                     f"The previous action was declined by the user/safety policy ({reason}). Do not retry it; "
                     "choose another approach or answer FAIL."},
                    {"type": "input_image", "image_url": self._image(obs), "detail": "original"}]}]
                first = True
            elif self.call_ids:
                inp = self._outputs(obs, feedback)
            else:
                inp = [{"role": "user", "content": [{"type": "input_text", "text": f"Verifier feedback: {feedback}"
                                                     if feedback else "Continue."},
                                                    {"type": "input_image", "image_url": self._image(obs),
                                                     "detail": "original"}]}]
        thought = ""
        for _ in range(self.max_internal_turns + 1):
            resp = self._send(inp, first)
            first = False
            final = self._parse(resp)
            if final is not None:
                self.call_ids = []
                return final
            a = self._pop()
            if a is not None:
                return a, thought
            # screenshot-only call (or the whole call failed to parse): answer it right away
            inp = self._outputs(obs, "")
        return Action("wait", seconds=0.0, reason="model requested screenshots only; re-observe"), thought

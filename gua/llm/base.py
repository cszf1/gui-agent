"""模型调用基础设施：预算（硬上限）+ 图像准备（产生坐标变换）+ 测试用脚本模型。

v0.3：
- 审查条目 3：Budget 不再只是记账。每个后端在**发出请求之前**调用 `Budget.before_call(role)`，
  调用数 / token / 成本（美元）任一达到上限就抛 BudgetExceeded，请求不会发出；GUIAgent 把它当作终止状态
  "budget_exhausted"（绝不算成功）。注意 token / 成本只能在调用返回后才知道，所以“上限”的语义是：
  一旦累计值达到上限，之后的调用全部拒绝（最后一次调用本身可能让累计值略超上限，超出量不超过单次调用的用量）。
- 审查条目 4：`prepare_image` 在缩放截图的同时产生 `ImageTransform`（原图尺寸 → 实际发送尺寸），
  后端把它挂在回复 `LLMReply.transforms` 上返回，坐标换算用“实际发送尺寸”。
"""
from __future__ import annotations

import base64
import io
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from PIL import Image

from ..coords import ImageTransform
from ..errors import PrivacyBlocked


class BudgetExceeded(RuntimeError):
    """在模型调用边界上触顶：请求未发出。"""

    def __init__(self, reason: str, resource: str = "calls"):
        super().__init__(reason)
        self.reason = reason
        self.resource = resource


class LLMReply(str):
    """模型回复文本（str 子类，旧代码当字符串用不受影响）+ 本次请求里每张图的坐标变换。"""
    transforms: tuple = ()

    def __new__(cls, text: str, transforms=()):
        obj = super().__new__(cls, text or "")
        obj.transforms = tuple(transforms)
        return obj


def reply_transform(reply: Any, index: int = 0) -> Optional[ImageTransform]:
    tfs = getattr(reply, "transforms", ()) or ()
    return tfs[index] if len(tfs) > index else None


def prepare_image(img: Image.Image, max_side: Optional[int] = None) -> tuple[Image.Image, ImageTransform]:
    """按 max_side 缩放（与 ImageTransform.sent_size_for 同一公式），返回 (发送的图, 变换)。"""
    sent = ImageTransform.sent_size_for(img.size, max_side)
    out = img if sent == img.size else img.resize(sent, Image.LANCZOS)
    return out, ImageTransform(tuple(img.size), tuple(sent))


def image_to_b64(img: Image.Image, max_side: Optional[int] = None, fmt: str = "PNG") -> tuple[str, str, tuple[int, int]]:
    """返回 (base64, media_type, 发送给模型的图像尺寸)。"""
    img, _ = prepare_image(img, max_side)
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return base64.b64encode(buf.getvalue()).decode(), f"image/{fmt.lower()}", img.size


def image_to_data_url(img: Image.Image, max_side: Optional[int] = None, fmt: str = "PNG") -> str:
    b64, mt, _ = image_to_b64(img, max_side, fmt)
    return f"data:{mt};base64,{b64}"


def price_cost(prompt_tokens: int, completion_tokens: int, price: Optional[tuple[float, float]]) -> float:
    """price = (每百万输入 token 美元, 每百万输出 token 美元)；未配置价格时成本记 0。"""
    if not price:
        return 0.0
    return (prompt_tokens or 0) / 1e6 * float(price[0]) + (completion_tokens or 0) / 1e6 * float(price[1])


@dataclass
class Budget:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    by_role: dict[str, int] = field(default_factory=dict)
    max_calls: Optional[int] = None
    max_tokens: Optional[int] = None
    max_cost_usd: Optional[float] = None
    refused: int = 0                       # 被硬上限拒绝的调用次数（评测记录用）

    def add(self, role: str, prompt_tokens: int = 0, completion_tokens: int = 0, dt: float = 0.0,
            cost_usd: float = 0.0) -> None:
        self.calls += 1
        self.seconds += dt
        self.by_role[role] = self.by_role.get(role, 0) + 1
        self.prompt_tokens += prompt_tokens or 0
        self.completion_tokens += completion_tokens or 0
        self.cost_usd += cost_usd or 0.0

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def exhausted(self) -> Optional[str]:
        if self.max_calls is not None and self.calls >= self.max_calls:
            return f"model-call budget exhausted ({self.calls}/{self.max_calls})"
        if self.max_tokens is not None and self.tokens >= self.max_tokens:
            return f"token budget exhausted ({self.tokens}/{self.max_tokens})"
        if self.max_cost_usd is not None and self.cost_usd >= self.max_cost_usd:
            return f"cost budget exhausted (${self.cost_usd:.4f}/${self.max_cost_usd})"
        return None

    def before_call(self, role: str = "") -> None:
        """在发出任何模型请求之前调用：已触顶则拒绝（抛 BudgetExceeded，请求不发出）。"""
        why = self.exhausted()
        if why:
            self.refused += 1
            res = "calls" if why.startswith("model-call") else "tokens" if why.startswith("token") else "cost"
            raise BudgetExceeded(f"{why}; refused call by {role or 'llm'}", res)


class ChatModel(Protocol):
    role: str
    budget: Budget

    def chat(self, system: str, text: str, images: Optional[list[Image.Image]] = None) -> str: ...


class BudgetGate:
    """包装任意 chat 对象（例如测试/脚本注入的模型）：调用前强制检查预算，若内部没有记账则代为记一次。"""

    def __init__(self, inner: Any, budget: Budget, role: str):
        self.inner, self.budget, self.role = inner, budget, role
        if hasattr(inner, "budget"):
            inner.budget = budget

    def chat(self, system: str, text: str, images=None):
        self.budget.before_call(self.role)
        n0 = self.budget.calls
        out = self.inner.chat(system, text, images)
        if self.budget.calls == n0:
            self.budget.add(self.role)
        return out

    def __getattr__(self, name):          # post / image_max_side / prompts … 透传
        return getattr(self.inner, name)

    def __setattr__(self, name, value):
        if name in ("inner", "budget", "role"):
            super().__setattr__(name, value)
        elif hasattr(self, "inner"):
            setattr(self.inner, name, value)
        else:
            super().__setattr__(name, value)


NO_IMAGE_NOTICE = (
    "[privacy mode] No screenshot is provided for this request because sensitive data was detected in this run. "
    "Do not ask for or refer to a screenshot; work only from the text (task, elements, history) below.\n\n")


class EgressGate:
    """模型请求出口的统一门控（模型出口组）：清洗 + 严格截图阻断。

    - 清洗：每次 chat 的 system / text，以及 post() 的整个结构化 body（递归清洗字符串与 dict key / 额外字段）；
      替换出来的 *** 不再被反复处理（由 Scrubber 保证）。
    - 截图阻断：共享 Scrubber 一旦进入敏感状态（配置了秘密 / 观察到密码框 / 敏感输入），之后所有请求都
      不再携带图片。普通文本角色改为“无图 + 明确提示”继续；纯视觉角色（vision_required）改抛
      PrivacyBlocked，绝不无图伪装成功。
    - 透传：未覆盖的属性读写（budget / role / build_payload / image_max_side / transforms …）转发给内层，
      因此 BudgetGate 的预算记账与 LLMReply.transforms 坐标变换都不受影响。
    """
    _LOCAL = ("_inner", "scrubber", "role", "vision_required")

    def __init__(self, inner, scrubber, role: Optional[str] = None, vision_required: bool = False):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "scrubber", scrubber)
        object.__setattr__(self, "role", role or getattr(inner, "role", "llm"))
        object.__setattr__(self, "vision_required", vision_required)

    def __getattr__(self, name):                 # budget / image_max_side / build_payload / prompts …
        return getattr(self._inner, name)

    def __setattr__(self, name, value):
        if name in EgressGate._LOCAL:
            object.__setattr__(self, name, value)
        else:
            setattr(self._inner, name, value)

    @property
    def images_blocked(self) -> bool:
        return bool(getattr(self.scrubber, "images_blocked", False))

    def chat(self, system: str, text: str, images=None):
        text = self.scrubber.scrub(text)
        system = self.scrubber.scrub(system)
        if images and self.images_blocked:
            if self.vision_required:
                raise PrivacyBlocked(self.role, "this model needs a screenshot to act, but screenshots are blocked")
            images = None
            text = NO_IMAGE_NOTICE + (text or "")
        return self._inner.chat(system, text, images)

    def post(self, payload: dict, betas: Optional[list] = None) -> dict:
        """Anthropic computer-use 等直接发结构化 body 的出口：发请求前拦下被禁的图像。"""
        if self.images_blocked and _payload_has_images(payload):
            if self.vision_required:
                raise PrivacyBlocked(self.role, "computer-use needs a screenshot, but screenshots are blocked")
            payload = _strip_images(payload)
        return self._inner.post(_scrub_payload(payload, self.scrubber), betas)


def _payload_has_images(obj) -> bool:
    if isinstance(obj, dict):
        if obj.get("type") in {"image", "image_url"} or "image_url" in obj:
            return True
        return any(_payload_has_images(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(_payload_has_images(v) for v in obj)
    return False


def _strip_images(obj):
    """删掉 body 里所有图像 block，并补一个明确的“无图”文本块（保守，不把原图藏在别处传走）。"""
    def walk(o):
        if isinstance(o, dict):
            if o.get("type") in {"image", "image_url"} or "image_url" in o:
                return None
            out = {}
            for k, v in o.items():
                w = walk(v)
                if w is not None:
                    out[k] = w
            return out
        if isinstance(o, list):
            return [w for w in (walk(v) for v in o) if w is not None]
        if isinstance(o, tuple):
            return tuple(w for w in (walk(v) for v in o) if w is not None)
        return o
    out = walk(obj)
    try:                                        # 给最后一条 user 消息补上明确提示
        content = out["messages"][-1]["content"]
        if isinstance(content, list):
            content.append({"type": "text", "text": NO_IMAGE_NOTICE.strip()})
    except (KeyError, IndexError, TypeError):
        pass
    return out


def _scrub_payload(obj, scrubber):
    """递归清洗 body：字符串值、dict 的字符串 key、额外字段都过 Scrubber。

    图像 block 整体保留（此处图像本来就获准发送）；被禁时已在 post 里整体删除，不会藏在别的字段里传走。
    """
    def scrub(x):
        return scrubber.scrub(x) if isinstance(x, str) else x
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, dict):
        if obj.get("type") in {"image", "image_url"} or "image_url" in obj:
            return obj
        return {scrub(k) if isinstance(k, str) else k: _scrub_payload(v, scrubber) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_payload(v, scrubber) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_scrub_payload(v, scrubber) for v in obj)
    return obj


class ScriptedLLM:
    """测试 / 离线演示用：按顺序返回预设回复，或用函数 fn(system, text, images) 生成回复。

    image_max_side 可选：模拟后端在发送前缩放截图（回复上带有对应的 ImageTransform）。
    """

    def __init__(self, replies=None, fn: Optional[Callable[..., str]] = None, role: str = "scripted",
                 budget: Optional[Budget] = None, image_max_side: Optional[int] = None):
        self.replies = list(replies or [])
        self.fn = fn
        self.role = role
        self.budget = budget or Budget()
        self.image_max_side = image_max_side
        self.prompts: list[str] = []

    def chat(self, system: str, text: str, images=None) -> str:
        self.budget.before_call(self.role)
        sent, tfs = [], []
        for im in images or []:
            s, tf = prepare_image(im, self.image_max_side)
            sent.append(s)
            tfs.append(tf)
        self.prompts.append(text)
        self.budget.add(self.role, 0, 0, 0.0)
        if self.fn:
            return LLMReply(self.fn(system, text, sent if images is not None else images), tfs)
        if not self.replies:
            raise RuntimeError("ScriptedLLM ran out of replies")
        return LLMReply(self.replies.pop(0), tfs)


def timed(fn: Callable[[], Any]) -> tuple[Any, float]:
    t0 = time.time()
    r = fn()
    return r, time.time() - t0

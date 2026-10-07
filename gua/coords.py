"""坐标约定与换算。

常见模型输出坐标约定：
- "norm1000"：[0,1000) 归一化（Qwen2-VL / UI-TARS-1.0 / SeeClick / OS-Atlas 风格）
- "norm1"：0~1 小数
- "resized"：Qwen2.5-VL / UI-TARS-1.5 / OpenCUA(qwen25) 风格，基于 smart_resize 后输入图像的绝对像素
- "pixel"：原始截图像素（Claude computer-use 在缩放后的截图上给像素，见 llm/anthropic.py）

换算目标永远是 Observation.screenshot 的像素坐标。

v0.3（审查条目 4）：v0.2 在发送前按 image_max_side 缩小截图，但换算时用的是**原图**尺寸计算模型坐标空间，
两者不一致（例：UI-TARS max_pixels=12845056、image_max_side=1280 时原图中心 (960,300) 被换算成 (640,200)）。
现在由 `ImageTransform` 显式描述整条变换链：

    原始截图像素（物理像素） --缩放--> 实际发送的图像尺寸 --模型坐标约定--> 模型坐标
                    └─dpi_scale─> 输入坐标（macOS point / Windows 逻辑像素，由平台后端使用）

它在**准备图像的地方**（llm/base.prepare_image）产生，随模型回复（LLMReply.transforms）一起传回，
grounder / actor / Claude computer-use 都用同一个对象换算，不再各自假设尺寸。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .actions import Action


def smart_resize(h: int, w: int, factor: int = 28, min_pixels: int = 56 * 56,
                 max_pixels: int = 1280 * 28 * 28) -> tuple[int, int]:
    """与 Qwen2.5-VL 预处理一致的尺寸计算（UI-TARS README_coordinates.md 给出的同一算法）。"""
    hb = max(factor, round(h / factor) * factor)
    wb = max(factor, round(w / factor) * factor)
    if hb * wb > max_pixels:
        beta = math.sqrt((h * w) / max_pixels)
        hb = math.floor(h / beta / factor) * factor
        wb = math.floor(w / beta / factor) * factor
    elif hb * wb < min_pixels:
        beta = math.sqrt(min_pixels / (h * w))
        hb = math.ceil(h * beta / factor) * factor
        wb = math.ceil(w * beta / factor) * factor
    return hb, wb


CONVENTIONS = ("norm1000", "norm1", "resized", "pixel")


@dataclass(frozen=True)
class ImageTransform:
    """一张截图从“原始像素”到“模型坐标”的完整变换（以及到平台输入坐标的 DPI 变换）。"""
    orig_size: tuple[int, int]                 # Observation.screenshot 尺寸（物理像素）
    sent_size: tuple[int, int]                 # 实际编码发送给模型的图像尺寸
    convention: str = "pixel"                  # 模型输出坐标约定
    max_pixels: int = 1280 * 28 * 28           # resized 约定：模型侧 smart_resize 的 max_pixels
    dpi_scale: float = 1.0                     # 截图像素 / 输入坐标单位（macOS Retina = 2）

    def __post_init__(self) -> None:
        if self.convention not in CONVENTIONS:
            raise ValueError(f"unknown coordinate convention {self.convention!r}")

    @staticmethod
    def sent_size_for(orig: tuple[int, int], max_side: Optional[int]) -> tuple[int, int]:
        """与 llm/base.prepare_image 使用同一公式（不依赖 PIL.thumbnail 的取整细节）。"""
        w, h = orig
        if not max_side or max(w, h) <= max_side:
            return int(w), int(h)
        s = max_side / max(w, h)
        return max(1, round(w * s)), max(1, round(h * s))

    @classmethod
    def identity(cls, size: tuple[int, int], convention: str = "pixel", max_pixels: int = 1280 * 28 * 28,
                 dpi_scale: float = 1.0) -> "ImageTransform":
        return cls(tuple(size), tuple(size), convention, max_pixels, dpi_scale)

    def with_convention(self, convention: str, max_pixels: Optional[int] = None) -> "ImageTransform":
        return ImageTransform(self.orig_size, self.sent_size, convention,
                              self.max_pixels if max_pixels is None else max_pixels, self.dpi_scale)

    @property
    def model_space(self) -> tuple[float, float]:
        """模型坐标系的 (宽, 高)。"""
        sw, sh = self.sent_size
        if self.convention == "norm1000":
            return 1000.0, 1000.0
        if self.convention == "norm1":
            return 1.0, 1.0
        if self.convention == "resized":
            rh, rw = smart_resize(sh, sw, max_pixels=self.max_pixels)
            return float(rw), float(rh)
        return float(sw), float(sh)

    def model_to_screenshot(self, x: float, y: float) -> tuple[int, int]:
        mw, mh = self.model_space
        ow, oh = self.orig_size
        return int(round(x / mw * ow)), int(round(y / mh * oh))

    def screenshot_to_model(self, x: float, y: float) -> tuple[float, float]:
        mw, mh = self.model_space
        ow, oh = self.orig_size
        return x / ow * mw, y / oh * mh

    def screenshot_to_input(self, x: float, y: float) -> tuple[int, int]:
        return int(x / self.dpi_scale), int(y / self.dpi_scale)

    def in_model_range(self, x: float, y: float) -> bool:
        mw, mh = self.model_space
        return 0 <= x <= mw and 0 <= y <= mh


@dataclass
class CoordMapper:
    convention: str = "norm1000"
    max_pixels: int = 1280 * 28 * 28

    def transform(self, img_w: int, img_h: int, sent_size: Optional[tuple[int, int]] = None) -> ImageTransform:
        return ImageTransform((img_w, img_h), tuple(sent_size or (img_w, img_h)), self.convention, self.max_pixels)

    def to_image(self, x: float, y: float, img_w: int, img_h: int,
                 sent_size: Optional[tuple[int, int]] = None) -> tuple[int, int]:
        """模型坐标 → 截图像素。sent_size = 实际发送给模型的图像尺寸（缺省视为未缩放）。"""
        return self.transform(img_w, img_h, sent_size).model_to_screenshot(x, y)

    def from_image(self, x: int, y: int, img_w: int, img_h: int,
                   sent_size: Optional[tuple[int, int]] = None) -> tuple[float, float]:
        """反向：像素 → 模型坐标（用于给模型回放历史动作、或测试）。"""
        return self.transform(img_w, img_h, sent_size).screenshot_to_model(x, y)


def to_pixel_action(a: Action, img_w: int, img_h: int, max_pixels: int = 1280 * 28 * 28,
                    transform: Optional[ImageTransform] = None) -> Action:
    """把动作里的模型坐标就地换算成截图像素，并把 coord_space 置为 pixel。

    优先使用动作自带的 transform（actor 从模型回复里拿到的“实际发送尺寸”），其次是参数 transform，
    最后才退回“未缩放”的假设。
    """
    tf = getattr(a, "transform", None) or transform
    if a.coord_space == "pixel" and (tf is None or tf.sent_size == tf.orig_size):
        if a.x is not None and a.y is not None:
            a.x, a.y = int(round(a.x)), int(round(a.y))
        if a.x2 is not None and a.y2 is not None:
            a.x2, a.y2 = int(round(a.x2)), int(round(a.y2))
        a.transform = None
        return a
    if tf is None:
        tf = ImageTransform.identity((img_w, img_h), a.coord_space, max_pixels)
    elif tf.convention != a.coord_space:
        tf = tf.with_convention(a.coord_space)
    if a.x is not None and a.y is not None:
        a.x, a.y = tf.model_to_screenshot(a.x, a.y)
    if a.x2 is not None and a.y2 is not None:
        a.x2, a.y2 = tf.model_to_screenshot(a.x2, a.y2)
    a.coord_space = "pixel"
    a.transform = None
    return a

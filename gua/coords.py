"""坐标约定与换算。

常见模型输出坐标约定：
- "norm1000"：[0,1000) 归一化（Qwen2-VL / UI-TARS-1.0 / SeeClick / OS-Atlas 风格）
- "norm1"：0~1 小数
- "resized"：Qwen2.5-VL / UI-TARS-1.5 / OpenCUA(qwen25) 风格，基于 smart_resize 后输入图像的绝对像素
- "pixel"：原始截图像素（Claude computer-use 在缩放后的截图上给像素，见 llm/anthropic.py）

换算目标永远是 Observation.screenshot 的像素坐标。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

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


@dataclass
class CoordMapper:
    convention: str = "norm1000"
    max_pixels: int = 1280 * 28 * 28

    def to_image(self, x: float, y: float, img_w: int, img_h: int) -> tuple[int, int]:
        c = self.convention
        if c == "norm1000":
            return int(x / 1000 * img_w), int(y / 1000 * img_h)
        if c == "norm1":
            return int(x * img_w), int(y * img_h)
        if c == "resized":
            rh, rw = smart_resize(img_h, img_w, max_pixels=self.max_pixels)
            return int(x / rw * img_w), int(y / rh * img_h)
        if c == "pixel":
            return int(x), int(y)
        raise ValueError(c)

    def from_image(self, x: int, y: int, img_w: int, img_h: int) -> tuple[float, float]:
        """反向：像素 → 模型坐标（用于给模型回放历史动作、或测试）。"""
        c = self.convention
        if c == "norm1000":
            return x / img_w * 1000, y / img_h * 1000
        if c == "norm1":
            return x / img_w, y / img_h
        if c == "resized":
            rh, rw = smart_resize(img_h, img_w, max_pixels=self.max_pixels)
            return x / img_w * rw, y / img_h * rh
        return float(x), float(y)


def to_pixel_action(a: Action, img_w: int, img_h: int, max_pixels: int = 1280 * 28 * 28) -> Action:
    """把动作里的模型坐标就地换算成截图像素，并把 coord_space 置为 pixel。"""
    if a.coord_space == "pixel":
        if a.x is not None:
            a.x, a.y = int(a.x), int(a.y)
        if a.x2 is not None:
            a.x2, a.y2 = int(a.x2), int(a.y2)
        return a
    m = CoordMapper(a.coord_space, max_pixels)
    if a.x is not None and a.y is not None:
        a.x, a.y = m.to_image(a.x, a.y, img_w, img_h)
    if a.x2 is not None and a.y2 is not None:
        a.x2, a.y2 = m.to_image(a.x2, a.y2, img_w, img_h)
    a.coord_space = "pixel"
    return a

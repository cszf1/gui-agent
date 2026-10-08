"""廉价的像素级变化检测：先用规则筛掉大部分情况，再决定要不要花一次 VLM 调用。"""
from __future__ import annotations

from typing import Optional

import numpy as np
from PIL import Image


def _arr(img: Image.Image, size=(320, 200)) -> np.ndarray:
    return np.asarray(img.convert("L").resize(size), dtype=np.float32) / 255.0


def frame_diff(a: Image.Image, b: Image.Image) -> float:
    """全图平均差异比例（0~1）。"""
    return float(np.mean(np.abs(_arr(a) - _arr(b)) > 0.08))


def region_diff(a: Image.Image, b: Image.Image, center: tuple[int, int], radius: int = 120) -> float:
    """动作点附近的局部差异；点击按钮常常只改变局部（高亮、勾选）。"""
    w, h = a.size
    x, y = center
    box = (max(0, x - radius), max(0, y - radius), min(w, x + radius), min(h, y + radius))
    if box[2] <= box[0] or box[3] <= box[1]:
        return 0.0
    ra = np.asarray(a.convert("L").crop(box), dtype=np.float32) / 255.0
    rb = np.asarray(b.convert("L").crop(box), dtype=np.float32) / 255.0
    return float(np.mean(np.abs(ra - rb) > 0.08))


def region_crop_diff(a: Image.Image, b: Image.Image, box) -> float:
    """指定矩形区域 (l, t, r, b) 的差异比例（后置条件 pixel_change 用）。"""
    w, h = a.size
    l, t, r, bt = (int(v) for v in box)
    l, t, r, bt = max(0, l), max(0, t), min(w, r), min(h, bt)
    if r <= l or bt <= t:
        return 0.0
    ra = np.asarray(a.convert("L").crop((l, t, r, bt)), dtype=np.float32) / 255.0
    rb = np.asarray(b.convert("L").crop((l, t, r, bt)), dtype=np.float32) / 255.0
    return float(np.mean(np.abs(ra - rb) > 0.08))


def side_by_side(before: Image.Image, after: Image.Image, mark: Optional[tuple[int, int]] = None,
                 max_w: int = 1600) -> Image.Image:
    """把前后截图拼在一起交给 VLM 判断，并在动作点画一个红圈。"""
    from PIL import ImageDraw
    w, h = before.size
    canvas = Image.new("RGB", (w * 2 + 20, h), (255, 255, 255))
    canvas.paste(before, (0, 0))
    canvas.paste(after, (w + 20, 0))
    if mark:
        d = ImageDraw.Draw(canvas)
        for ox in (0, w + 20):
            x, y = mark[0] + ox, mark[1]
            d.ellipse((x - 18, y - 18, x + 18, y + 18), outline=(255, 0, 0), width=4)
    if canvas.width > max_w:
        r = max_w / canvas.width
        canvas = canvas.resize((max_w, int(canvas.height * r)))
    return canvas

"""廉价的像素级变化检测：先用规则筛掉大部分情况，再决定要不要花一次 VLM 调用。

v0.9：只用 Pillow 实现（原 numpy 版本按 |a-b|/255 > 0.08 计数，等价于 8 位灰度差 >= 21），
桌面安装包因此不再携带 numpy。
"""
from __future__ import annotations

from typing import Optional

from PIL import Image, ImageChops

# |a - b| / 255 > 0.08  <=>  |a - b| > 20.4  <=>  |a - b| >= 21（8 位整数灰度）
_THRESHOLD = 21
_LUT = [0] * _THRESHOLD + [255] * (256 - _THRESHOLD)


def _changed_ratio(a: Image.Image, b: Image.Image) -> float:
    diff = ImageChops.difference(a, b).point(_LUT)
    total = diff.size[0] * diff.size[1]
    return diff.histogram()[255] / total if total else 0.0


def frame_diff(a: Image.Image, b: Image.Image) -> float:
    """全图平均差异比例（0~1）。"""
    size = (320, 200)
    return _changed_ratio(a.convert("L").resize(size), b.convert("L").resize(size))


def region_diff(a: Image.Image, b: Image.Image, center: tuple[int, int], radius: int = 120) -> float:
    """动作点附近的局部差异；点击按钮常常只改变局部（高亮、勾选）。"""
    w, h = a.size
    x, y = center
    box = (max(0, x - radius), max(0, y - radius), min(w, x + radius), min(h, y + radius))
    if box[2] <= box[0] or box[3] <= box[1]:
        return 0.0
    return _changed_ratio(a.convert("L").crop(box), b.convert("L").crop(box))


def region_crop_diff(a: Image.Image, b: Image.Image, box) -> float:
    """指定矩形区域 (l, t, r, b) 的差异比例（后置条件 pixel_change 用）。"""
    w, h = a.size
    l, t, r, bt = (int(v) for v in box)
    l, t, r, bt = max(0, l), max(0, t), min(w, r), min(h, bt)
    if r <= l or bt <= t:
        return 0.0
    return _changed_ratio(a.convert("L").crop((l, t, r, bt)), b.convert("L").crop((l, t, r, bt)))


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

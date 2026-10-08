"""v0.7：Set-of-Mark 观察融合（无障碍树 + 截图）。

参考 SoM prompting（Yang et al. 2023）、WindowsAgentArena Navi、OmniParser / UFO² 的做法：把可交互控件的编号框
直接画在截图上，编号 = element_id，与提示词里的元素列表一一对应。模型可以“看图点编号”，坐标交给无障碍树，
避免 VLM 坐标漂移；元素列表里没有的目标仍可用 target 描述或坐标（走 grounder）。

只画：可交互、启用、在视口内、面积足够的元素；密码框只画框和编号，不写名字。编号标签放在框的左上角外侧
（放不下时放内侧），颜色按编号循环，保证与背景区分。几何不变（没有缩放 / 裁剪），所以坐标动作照常有效。
没有 OCR：纯视觉文字仍由模型自己读截图（诚实说明）。
"""
from __future__ import annotations

from typing import Iterable, Optional

from PIL import Image, ImageDraw

from .env.base import INTERACTIVE_ROLES, Observation, UIElement

PALETTE = [(230, 25, 75), (60, 180, 75), (0, 130, 200), (245, 130, 48), (145, 30, 180), (0, 128, 128),
           (170, 110, 40), (128, 0, 0), (0, 0, 128), (128, 128, 0)]


def select_marks(obs: Observation, max_marks: int = 60, min_side: int = 4) -> list[UIElement]:
    w, h = obs.screenshot.size
    out: list[UIElement] = []
    for e in obs.elements:
        l, t, r, b = e.rect
        if (e.role not in INTERACTIVE_ROLES or not e.enabled or e.offscreen or r - l < min_side or b - t < min_side
                or r <= 0 or b <= 0 or l >= w or t >= h):
            continue
        out.append(e)
    out.sort(key=lambda e: (e.rect[1] // 10, e.rect[0]))
    return out[:max_marks]


def render_som(obs: Observation, marks: Optional[Iterable[UIElement]] = None) -> Image.Image:
    img = obs.screenshot.convert("RGB").copy()
    d = ImageDraw.Draw(img)
    w, h = img.size
    for e in (select_marks(obs) if marks is None else marks):
        color = PALETTE[e.id % len(PALETTE)]
        l, t, r, b = (max(0, e.rect[0]), max(0, e.rect[1]), min(w - 1, e.rect[2]), min(h - 1, e.rect[3]))
        d.rectangle((l, t, r, b), outline=color, width=2)
        label = str(e.id)
        tw, th = 7 * len(label) + 4, 12
        ly = t - th if t - th >= 0 else t
        d.rectangle((l, ly, l + tw, ly + th), fill=color)
        d.text((l + 2, ly), label, fill=(255, 255, 255))
    return img


def legend(marks: Iterable[UIElement]) -> str:
    return "\n".join(e.brief() for e in marks)

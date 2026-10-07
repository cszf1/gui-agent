"""定位：把“点什么”（element_id / 自然语言描述）变成截图像素坐标。

Mixture-of-grounding（参考 Agent S2 的 Mixture-of-Grounding、UFO² 的 UIA+视觉混合检测、
WindowsAgentArena Navi 的 a11y+OmniParser 混合 SoM）：
1. 无障碍树精确匹配：element_id 或唯一同名控件 → 控件中心（最可靠、零模型调用）
2. VLM 全屏定位：专门的 grounding 模型（UI-TARS / OpenCUA / Qwen-VL / OS-Atlas / UGround）输出点，
   经 CoordMapper 按模型的坐标约定换算
3. RegionFocus / ScreenSeekeR 风格局部放大：点击无效后围绕上次点裁剪放大再定位
多个同名控件（两个“保存”）不猜，交给视觉，避免“身份失配”。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from PIL import Image

from .coords import CoordMapper
from .env.base import Observation, UIElement
from .parsing import parse_point

GROUND_SYSTEM = "You are a GUI grounding model. Output only the click point of the described element."
GROUND_PROMPT = ('Screenshot of {pdesc}. Locate this element and output its center point '
                 'as JSON {{"x": <x>, "y": <y>}}.\nElement: {target}')


@dataclass
class GroundingResult:
    x: int
    y: int
    source: str            # "a11y" | "a11y_fuzzy" | "vlm" | "vlm_zoom"
    element: Optional[UIElement] = None
    crop: Optional[tuple[int, int, int, int]] = None


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


class Grounder:
    def __init__(self, llm, mapper: CoordMapper, use_a11y: bool = True, zoom_factor: float = 2.5,
                 prompt: str = GROUND_PROMPT, platform_desc: str = "a computer screen", fuzzy: bool = True):
        self.llm = llm
        self.mapper = mapper
        self.use_a11y = use_a11y
        self.zoom_factor = zoom_factor
        self.prompt = prompt
        self.platform_desc = platform_desc
        self.fuzzy = fuzzy

    # 1) 无障碍树匹配
    def match_a11y(self, obs: Observation, target: str, element_id: Optional[int] = None) -> Optional[tuple[UIElement, str]]:
        if not self.use_a11y:
            return None
        if element_id is not None:
            e = obs.element(element_id)
            if e and e.enabled:
                return e, "a11y"
        t = _norm(target)
        if not t:
            return None
        cands = [e for e in obs.elements if e.enabled and e.role != "dialog"]
        exact = [e for e in cands if _norm(e.name) == t]
        if len(exact) == 1:
            return exact[0], "a11y"
        if exact:
            return None  # 多个同名 → 交给视觉，避免身份失配
        if self.fuzzy and len(t) >= 3:
            part = [e for e in cands if e.name and (t in _norm(e.name) or _norm(e.name) in t) and len(_norm(e.name)) >= 3]
            if len(part) == 1:
                return part[0], "a11y_fuzzy"
        return None

    # 2) VLM 全屏定位
    def ground_vlm(self, img: Image.Image, target: str) -> Optional[tuple[int, int]]:
        if self.llm is None:
            return None
        out = self.llm.chat(GROUND_SYSTEM, self.prompt.format(target=target, pdesc=self.platform_desc), [img])
        p = parse_point(out)
        if p is None:
            return None
        return self.mapper.to_image(p[0], p[1], img.width, img.height)

    # 3) 局部放大
    def ground_zoom(self, img: Image.Image, target: str, focus: tuple[int, int]) -> Optional[tuple[int, int, tuple]]:
        w, h = img.size
        cw, ch = int(w / self.zoom_factor), int(h / self.zoom_factor)
        l = max(0, min(w - cw, focus[0] - cw // 2))
        t = max(0, min(h - ch, focus[1] - ch // 2))
        crop = img.crop((l, t, l + cw, t + ch)).resize((w, h))
        p = self.ground_vlm(crop, target)
        if p is None:
            return None
        return l + int(p[0] / self.zoom_factor), t + int(p[1] / self.zoom_factor), (l, t, l + cw, t + ch)

    def ground(self, obs: Observation, target: str, element_id: Optional[int] = None,
               zoom_around: Optional[tuple[int, int]] = None) -> Optional[GroundingResult]:
        m = self.match_a11y(obs, target or "", element_id)
        if m is not None and zoom_around is None:
            e, src = m
            x, y = e.center
            return GroundingResult(x, y, src, element=e)
        if zoom_around is not None and target:
            r = self.ground_zoom(obs.screenshot, target, zoom_around)
            if r:
                return GroundingResult(r[0], r[1], "vlm_zoom", crop=r[2])
        if m is not None:  # 放大定位失败时退回控件中心
            e, src = m
            return GroundingResult(*e.center, src, element=e)
        if not target:
            return None
        p = self.ground_vlm(obs.screenshot, target)
        if p is None:
            return None
        return GroundingResult(p[0], p[1], "vlm")

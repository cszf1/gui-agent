"""定位：把“点什么”（element_id / 自然语言描述）变成截图像素坐标。

Mixture-of-grounding（参考 Agent S2 的 Mixture-of-Grounding、UFO² 的 UIA+视觉混合检测、
WindowsAgentArena Navi 的 a11y+OmniParser 混合 SoM）：
1. 无障碍树精确匹配：element_id 或唯一同名控件 → 控件中心（最可靠、零模型调用）
2. VLM 全屏定位：专门的 grounding 模型（UI-TARS / OpenCUA / Qwen-VL / OS-Atlas / UGround）输出点，
   经 CoordMapper 按模型的坐标约定换算
3. RegionFocus / ScreenSeekeR 风格局部放大：点击无效后围绕上次点裁剪放大再定位
多个同名控件（两个“保存”）不猜，交给视觉，避免“身份失配”。

v0.3：VLM 输出的坐标用“实际发送给模型的图像尺寸”换算（模型回复携带的 ImageTransform，审查条目 4）；
是否允许无障碍树匹配由 CapabilityPolicy.a11y_grounding 决定（审查条目 6，build_agent 传入 use_a11y）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from PIL import Image

from .coords import CoordMapper, ImageTransform
from .env.base import Observation, UIElement
from .llm.base import reply_transform
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
    confidence: float = 1.0      # v0.7：0..1；a11y 精确 1.0 / 模糊 0.8；VLM 由“全屏点 vs 放大复核点”一致性决定


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


_ROLE_WORDS = {
    "button": "button", "按钮": "button", "link": "link", "链接": "link",
    "checkbox": "checkbox", "check box": "checkbox", "复选框": "checkbox",
    "textbox": "textbox", "text box": "textbox", "field": "textbox",
    "input": "textbox", "输入框": "textbox", "文本框": "textbox",
    "tab": "tab", "标签页": "tab", "radio button": "radio", "单选框": "radio",
    "menu item": "menuitem", "菜单项": "menuitem",
}


def qualified_target(target: str):
    """Recognize an explicit role plus a name; do not guess synonyms."""
    target = (target or "").strip()
    for word in sorted(_ROLE_WORDS, key=len, reverse=True):
        escaped = re.escape(word)
        separator = r"\s+" if word.isascii() else r"\s*"
        match = re.fullmatch(r"(.+?)" + separator + escaped, target, re.I)
        if match is None:
            match = re.fullmatch(escaped + r"\s*[:：]\s*(.+)", target, re.I)
        if match:
            name = match[1].strip(" \t\"'“”「」")
            if name:
                return name, _ROLE_WORDS[word]
    return None


class Grounder:
    def __init__(self, llm, mapper: CoordMapper, use_a11y: bool = True, zoom_factor: float = 2.5,
                 prompt: str = GROUND_PROMPT, platform_desc: str = "a computer screen", fuzzy: bool = True,
                 refine: bool = False, agree_tol: float = 0.02):
        self.llm = llm
        self.mapper = mapper
        self.use_a11y = use_a11y
        self.zoom_factor = zoom_factor
        self.prompt = prompt
        self.platform_desc = platform_desc
        self.fuzzy = fuzzy
        # v0.7：两阶段视觉定位——全屏粗定位后围绕该点裁剪放大再定位一次；两次结果的距离（占对角线比例）
        # 超过 agree_tol 判为低置信（调用方可据此拒绝点击、要求换描述 / 用 element_id）。
        self.refine = refine
        self.agree_tol = agree_tol
        self.min_confidence = 0.0     # 低于它的视觉定位结果由 agent 当作定位失败

    # 1) 无障碍树匹配
    def match_a11y(self, obs: Observation, target: str, element_id: Optional[int] = None) -> Optional[tuple[UIElement, str]]:
        if not self.use_a11y:
            return None
        if element_id is not None:
            e = obs.element(element_id)
            if e and e.enabled:
                qualified = qualified_target(target)
                if qualified and (e.role != qualified[1] or _norm(e.name) != _norm(qualified[0])):
                    return None
                return e, "a11y"
            return None  # An invalid explicit ID must not select another same-name control.
        t = _norm(target)
        if not t:
            return None
        cands = [e for e in obs.elements if e.enabled and e.role != "dialog"]
        exact = [e for e in cands if _norm(e.name) == t]
        if len(exact) == 1:
            return exact[0], "a11y"
        if exact:
            return None  # 多个同名 → 交给视觉，避免身份失配
        qualified = qualified_target(target)
        if qualified:
            name, role = qualified
            exact_role = [e for e in cands if e.role == role and _norm(e.name) == _norm(name)]
            # A role-qualified description only takes the shortcut when both
            # the role and complete label agree, and the result is unique.
            return (exact_role[0], "a11y") if len(exact_role) == 1 else None
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
        tf = reply_transform(out)
        if tf is None:                  # 后端没有报告发送尺寸：视为未缩放
            tf = ImageTransform.identity(img.size)
        tf = tf.with_convention(self.mapper.convention, self.mapper.max_pixels)
        if not tf.in_model_range(p[0], p[1]):
            return None                 # 越出模型坐标系：当作定位失败，而不是点到屏幕外
        return tf.model_to_screenshot(p[0], p[1])

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
        if self.use_a11y and element_id is not None and m is None:
            return None
        if m is not None and zoom_around is None:
            e, src = m
            x, y = e.center
            return GroundingResult(x, y, src, element=e, confidence=1.0 if src == "a11y" else 0.8)
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
        if not self.refine:
            return GroundingResult(p[0], p[1], "vlm", confidence=0.6)
        return self._refined(obs, target, p)

    def _refined(self, obs: Observation, target: str, p: tuple[int, int]) -> GroundingResult:
        """放大复核：一致 → 用更精细的放大点（高置信）；不一致 → 仍给放大点但标低置信。"""
        r = self.ground_zoom(obs.screenshot, target, (int(p[0]), int(p[1])))
        w, h = obs.screenshot.size
        if r is None:
            return GroundingResult(p[0], p[1], "vlm", confidence=0.3)
        dist = ((r[0] - p[0]) ** 2 + (r[1] - p[1]) ** 2) ** 0.5 / max(1.0, (w * w + h * h) ** 0.5)
        conf = 0.9 if dist <= self.agree_tol else max(0.1, 0.5 - dist)
        el = obs.element_at(r[0], r[1]) if self.use_a11y else None
        if el is not None and el.role in {"button", "link", "textbox", "checkbox", "radio", "combobox",
                                          "menuitem", "tab", "listitem", "treeitem", "switch", "cell"}:
            conf = min(1.0, conf + 0.05)
        return GroundingResult(r[0], r[1], "vlm_refined", crop=r[2], confidence=round(conf, 3))

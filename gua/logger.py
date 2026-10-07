"""轨迹日志：每步的前后截图、动作、目标、窗口、时间、verdict，方便做失败分类、人工复核和 HTML 回放。

输出结构：
runs/<run_id>/
  meta.json          任务、平台、配置摘要、最终结果、预算
  steps.jsonl        每步一行（kind = plan | step | milestone | replan | reflection | safety | ask_user | final_check）
  shots/0003_before.png, 0003_after.png
  report.html        `gua replay runs/<run_id>` 或运行结束时自动生成

v0.3.1：scrubber（gua.sensitive.Scrubber）是落盘前的最后一道防线——GUIAgent 把已知秘密登记进去，
steps.jsonl / meta.json 写入前统一清洗，report.html 由这两个文件生成，因此同样不含秘密。
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Optional

from PIL import Image


def _jsonable(o: Any) -> Any:
    if is_dataclass(o):
        return asdict(o)
    if hasattr(o, "value"):
        return o.value
    if isinstance(o, (set, tuple)):
        return list(o)
    return str(o)


class TrajectoryLogger:
    def __init__(self, root: str | Path, run_id: Optional[str] = None, save_images: bool = True,
                 max_side: Optional[int] = 1280):
        self.run_id = run_id or time.strftime("%Y%m%d-%H%M%S")
        self.dir = Path(root) / self.run_id
        (self.dir / "shots").mkdir(parents=True, exist_ok=True)
        self.save_images = save_images
        self.max_side = max_side
        self._f = open(self.dir / "steps.jsonl", "a", encoding="utf-8")
        self._meta: dict[str, Any] = {}
        from .sensitive import Scrubber
        self.scrubber = Scrubber()

    def scrub(self, obj: Any) -> Any:
        return self.scrubber.scrub_obj(obj)

    def shot(self, step: int, tag: str, img: Image.Image) -> Optional[str]:
        if not self.save_images or img is None:
            return None
        if getattr(self.scrubber, "images_blocked", False):
            # 严格隐私阻断：本次运行识别到敏感信息后，任何截图都不再落盘（避免 HTML 回放泄漏）
            return None
        p = self.dir / "shots" / f"{step:04d}_{tag}.png"
        if self.max_side and max(img.size) > self.max_side:
            img = img.copy()
            img.thumbnail((self.max_side, self.max_side))
        img.save(p)
        return str(p.relative_to(self.dir)).replace("\\", "/")

    def step(self, **rec: Any) -> None:
        rec.setdefault("t", time.time())
        clean_rec = self.scrubber.scrub_obj(rec)
        line = json.dumps(clean_rec, ensure_ascii=False, default=_jsonable)
        self._f.write(line + "\n")
        self._f.flush()

    def meta(self, **meta: Any) -> None:
        self._meta.update(meta)
        clean_meta = self.scrubber.scrub_obj(self._meta)
        txt = json.dumps(clean_meta, ensure_ascii=False, indent=2, default=_jsonable)
        (self.dir / "meta.json").write_text(txt, encoding="utf-8")

    def close(self, report: bool = True) -> Optional[Path]:
        if not self._f.closed:
            self._f.close()
        if report:
            from .report import build_report
            return build_report(self.dir)
        return None

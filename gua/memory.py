"""任务记忆：动作历史 + 里程碑（参考 OS-Symphony 的 milestone-driven memory）+ 反思笔记 + 循环检测。

- steps：短期记忆，按子目标过滤、只给模型看最近 window 步（Agent S 的 max_trajectory_length 思路）
- milestones：已核验完成的子目标及其证据；重规划时让之后的里程碑失效（旧完成证据何时失效）
- notes：反思器产出的经验（Mobile-Agent-v3 的 Reflector / Notetaker、Agent S 的 reflection），跨子目标保留
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image


@dataclass
class StepRecord:
    step: int
    subgoal_id: int
    action: str
    verdict: str
    evidence: str
    recovery: Optional[str] = None


@dataclass
class Milestone:
    subgoal_id: int
    goal: str
    evidence: str
    timestamp: float
    thumbnail: Optional[Image.Image] = None


@dataclass
class Memory:
    window: int = 8
    steps: list[StepRecord] = field(default_factory=list)
    milestones: list[Milestone] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    max_notes: int = 5
    _recent: deque = field(default_factory=lambda: deque(maxlen=6))

    def add_step(self, rec: StepRecord) -> None:
        self.steps.append(rec)
        self._recent.append((rec.action, rec.verdict))

    def add_milestone(self, m: Milestone) -> None:
        if m.thumbnail is not None:
            m.thumbnail = m.thumbnail.copy()
            m.thumbnail.thumbnail((480, 300))
        self.milestones.append(m)

    def add_note(self, note: str) -> None:
        note = (note or "").strip()
        if note and note not in self.notes:
            self.notes.append(note[:300])
            self.notes = self.notes[-self.max_notes:]

    def notes_text(self) -> str:
        return "\n".join(f"- {n}" for n in self.notes) if self.notes else "(none)"

    def invalidate_after(self, subgoal_id: int) -> None:
        """要求变化或重规划时，让之后的“已完成”记录失效（报告 C1：旧完成证据何时失效）。"""
        self.milestones = [m for m in self.milestones if m.subgoal_id < subgoal_id]

    def is_looping(self, n: int = 3) -> bool:
        """同一动作连续 n 次且都没有成功 → 卡住了。"""
        if len(self._recent) < n:
            return False
        last = list(self._recent)[-n:]
        return len({a for a, _ in last}) == 1 and all(v != "success" for _, v in last)

    def history_text(self, subgoal_id: Optional[int] = None) -> str:
        recs = [r for r in self.steps if subgoal_id is None or r.subgoal_id == subgoal_id][-self.window:]
        if not recs:
            return "(none)"
        lines = []
        for r in recs:
            extra = f" -> recovery:{r.recovery}" if r.recovery else ""
            lines.append(f"#{r.step} {r.action} => {r.verdict} ({r.evidence[:80]}){extra}")
        return "\n".join(lines)

    def milestones_text(self) -> str:
        if not self.milestones:
            return "(none)"
        return "\n".join(f"[done] subgoal {m.subgoal_id}: {m.goal} | evidence: {m.evidence[:80]}"
                         for m in self.milestones)

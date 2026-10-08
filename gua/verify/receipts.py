"""“已验证完成”凭据（v0.6）：每个子目标 / 整个任务的完成结论都附带可复查的证据。

现有 computer-use agent 通常只给一句“我完成了”；本项目把完成结论变成结构化凭据：
- 结论（verified_done / re_verified_on_resume / unverified / failed / uncertain）与证据等级（L1 规则 / L2 模型）；
- 证据文字（期望文本片段及其新鲜度、后置条件逐条结果、无障碍状态变化）；
- 该子目标内每一步的动作摘要、验证结论、执行模态与实际路由（GUI / 语义 / shell / API，是否后台、是否回退）；
- 判定时刻屏幕的摘要（截图 SHA-256、无障碍树摘要 SHA-256、前台窗口、URL），可与 shots/ 下的截图对照。
凭据经过与日志相同的清洗器（秘密 / 密码不会出现）。它证明的是“界面上可观察到的状态”，不是服务器端业务事实。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any, Optional


def image_digest(img) -> str:
    if img is None:
        return ""
    try:
        return hashlib.sha256(img.convert("RGB").tobytes()).hexdigest()[:32]
    except Exception:  # noqa: BLE001
        return ""


def obs_digest(obs) -> dict:
    if obs is None:
        return {}
    briefs = "\n".join(e.brief() for e in (getattr(obs, "elements", None) or []))
    return {"screenshot_sha256": image_digest(getattr(obs, "screenshot", None)),
            "a11y_sha256": hashlib.sha256(briefs.encode("utf-8")).hexdigest()[:32],
            "window": getattr(obs, "active_window", ""), "url": getattr(obs, "url", ""),
            "observed_at": getattr(obs, "timestamp", 0.0)}


def make_receipt(kind: str, subject: str, verdict: str, level: str, evidence: str, obs=None,
                 steps: Optional[list] = None, extra: Optional[dict] = None) -> dict[str, Any]:
    steps = list(steps or [])
    modalities: dict[str, int] = {}
    for s in steps:
        m = s.get("modality") or "?"
        modalities[m] = modalities.get(m, 0) + 1
    r = {"kind": kind, "subject": subject, "verdict": verdict, "level": level, "evidence": evidence,
         "screen": obs_digest(obs), "steps": steps, "modalities": modalities, "issued_at": time.time()}
    if extra:
        r.update(extra)
    return r


def summarize_postconditions(pcs) -> list[str]:
    out = []
    for p in pcs or []:
        if isinstance(p, dict):
            out.append(f"{p.get('kind')}:{p.get('status')}")
    return out

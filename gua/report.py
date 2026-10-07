"""HTML 回放报告：把 steps.jsonl + 截图渲染成一个可离线打开的单页（`gua replay <run_dir>`）。"""
from __future__ import annotations

import html
import json
from pathlib import Path

COLORS = {"success": "#1a7f37", "in_progress": "#9a6700", "no_effect": "#bc4c00", "failed": "#cf222e",
          "blocked": "#8250df", "uncertain": "#6e7781", "off": "#6e7781", "rejected": "#cf222e", "done": "#1a7f37", "fail": "#cf222e",
          "step_limit": "#bc4c00", "budget_limit": "#bc4c00", "time_limit": "#bc4c00"}

CSS = """body{font-family:-apple-system,Segoe UI,Microsoft YaHei,sans-serif;margin:24px;color:#1f2328;background:#f6f8fa}
h1{font-size:20px} .card{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:12px 16px;margin:12px 0}
.row{display:flex;gap:12px;flex-wrap:wrap} .row img{max-width:48%;border:1px solid #d0d7de;border-radius:4px}
.tag{display:inline-block;padding:1px 8px;border-radius:10px;color:#fff;font-size:12px;margin-right:6px}
code{background:#eff1f3;padding:1px 4px;border-radius:4px;font-size:12px;word-break:break-all}
.muted{color:#656d76;font-size:12px} table{border-collapse:collapse} td{padding:2px 10px 2px 0;vertical-align:top} td:first-child{white-space:nowrap;color:#656d76}"""


def _e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def _tag(v: str) -> str:
    return f'<span class="tag" style="background:{COLORS.get(v, "#57606a")}">{_e(v)}</span>'


def build_report(run_dir: str | Path) -> Path:
    d = Path(run_dir)
    steps = []
    p = d / "steps.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    steps.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    meta = {}
    if (d / "meta.json").exists():
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    res = meta.get("result") or {}
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><title>gua replay {_e(d.name)}</title>"
             f"<style>{CSS}</style></head><body>",
             f"<h1>轨迹回放 · {_e(d.name)}</h1><div class='card'><table>",
             f"<tr><td>任务</td><td>{_e(meta.get('task_text') or (meta.get('task') or {}).get('instruction', ''))}</td></tr>",
             f"<tr><td>平台</td><td>{_e(meta.get('platform', ''))}</td></tr>",
             f"<tr><td>结果</td><td>{_tag(res.get('status', '?'))} claimed_done={_e(res.get('claimed_done'))} "
             f"steps={_e(res.get('steps'))} replans={_e(res.get('replans'))} {_e(res.get('message', ''))}</td></tr>",
             f"<tr><td>预算</td><td><code>{_e(json.dumps((res.get('budget') or {}), ensure_ascii=False))}</code></td></tr>"]
    ev = meta.get("eval")
    if ev:
        parts.append(f"<tr><td>判分</td><td>{_tag('success' if ev.get('passed') else 'failed')}"
                     f"{_e('; '.join(ev.get('check_notes', [])))}</td></tr>")
    parts.append("</table></div>")
    for s in steps:
        k = s.get("kind")
        if k == "plan" or k == "replan":
            sg = s.get("subgoals", [])
            items = "".join(f"<li>{_e(x.get('goal'))} <span class='muted'>expect: {_e(x.get('expected'))}"
                            f"{' | text: ' + _e(x.get('expect_text')) if x.get('expect_text') else ''}</span></li>"
                            for x in sg)
            extra = f"<div class='muted'>failed: {_e((s.get('failed') or {}).get('goal'))} — {_e(s.get('notes'))}</div>" if k == "replan" else ""
            parts.append(f"<div class='card'><b>{'规划' if k == 'plan' else '重规划'}</b>{extra}<ol>{items}</ol></div>")
        elif k == "step":
            imgs = "".join(f"<img src='{_e(s[t])}' loading='lazy'>" for t in ("before", "after") if s.get(t))
            parts.append(
                f"<div class='card'><b>#{_e(s.get('step'))}</b> 子目标 {_e(s.get('subgoal'))} "
                f"{_tag(s.get('verdict', '?'))}<span class='muted'>{_e(s.get('level'))} · grounding={_e(s.get('grounding'))}"
                f"{' · recovery=' + _e(s.get('recovery')) if s.get('recovery') else ''}</span>"
                f"<div>动作 <code>{_e(json.dumps(s.get('action'), ensure_ascii=False))}</code></div>"
                f"<div class='muted'>thought: {_e(s.get('thought'))}</div>"
                f"<div>证据: {_e(s.get('evidence'))}</div>"
                f"<div class='muted'>signals: <code>{_e(json.dumps(s.get('signals'), ensure_ascii=False))}</code></div>"
                f"<div class='row'>{imgs}</div></div>")
        elif k == "milestone":
            parts.append(f"<div class='card'>{_tag('success')}<b>里程碑</b> {_e((s.get('subgoal') or {}).get('goal'))}"
                         f" <span class='muted'>{_e(s.get('evidence'))}</span></div>")
        elif k in {"reflection", "safety", "ask_user", "final_check", "rejected_done", "disturbance"}:
            parts.append(f"<div class='card'><b>{_e(k)}</b> <code>{_e(json.dumps({x: y for x, y in s.items() if x not in ('kind', 't')}, ensure_ascii=False))}</code></div>")
    parts.append("</body></html>")
    out = d / "report.html"
    out.write_text("\n".join(parts), encoding="utf-8")
    return out

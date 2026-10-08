"""任务集运行器：初始化 → 运行 agent → 客观判分 → 汇总指标（跨平台）。

任务文件（JSON），示例见 tasks/<platform>/：
{
  "id": "web_form_submit",
  "platform": "web",                         # windows | macos | linux | android | web | mock
  "instruction": "...",
  "task_window": "Contact form",             # 焦点检查用：窗口标题片段 / Android 包名 / 页面标题
  "start_url": "../web_assets/form.html",    # web：相对任务文件目录
  "setup": [{"type": "copy", ...}, {"type": "launch", ...}, {"type": "adb", "cmd": "..."}],
  "checks": [{"type": "web_text", "selector": "#status", "text": "Submitted"}],
  "teardown": [...],
  "disturbance": {"kind": "new_tab", "at_step": 2},     # 可选；Windows 也支持 {"delay": 8}
  "demo": {...}                               # 可选：脚本策略（--policy scripted）用的演示脚本
}

核心指标（调研报告 10.4，分母写清楚）：
- success_rate        = 判分通过 / 总运行数
- false_done_rate     = agent 宣称完成但判分失败 / agent 宣称完成数   ← 方向 A 最关心
- recovery_rate       = 有干扰且判分通过 / 有干扰的运行数
- 平均步数、模型调用数、token、耗时
- v0.3：budget_exhausted_runs（预算硬上限触顶，绝不计为成功）、uncertain_runs（收尾核验不确定）、
  user_abort（人工紧急停止，记录后停止整个任务集）
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .checkers import run_checks


def _x(p: str) -> str:
    return os.path.expandvars(os.path.expanduser(p))


def do_setup(steps: list[dict], env=None, task_dir: Optional[Path] = None) -> None:
    for s in steps:
        t = s["type"]
        if t == "copy":
            dst = Path(_x(s["dst"]))
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(_x(s["src"]), dst)
        elif t == "mkdir":
            Path(_x(s["path"])).mkdir(parents=True, exist_ok=True)
        elif t == "remove":
            p = Path(_x(s["path"]))
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink()
        elif t == "launch":
            subprocess.Popen([_x(c) for c in s["cmd"]])
            time.sleep(s.get("wait", 2))
        elif t == "kill":
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/IM", s["process"], "/F"], capture_output=True)
            else:
                subprocess.run(["pkill", "-f", s["process"]], capture_output=True)
        elif t == "sleep":
            time.sleep(s["seconds"])
        elif t == "open_url":
            from ..env.web import to_url
            env.open(to_url(s["url"], task_dir))
        elif t == "adb":
            env.shell(s["cmd"])
            time.sleep(s.get("wait", 0.5))
        elif t == "open_app":
            from ..actions import Action
            env.execute(Action("open_app", app=s["app"]))
            time.sleep(s.get("wait", 2))
        elif t == "sandbox_shell":            # v0.6：沙箱里的 setup 命令（守护进程需 --shell）
            from ..actions import Action
            r = env.run_tool(Action("shell", command=list(s["argv"])))
            if not r.ok:
                raise RuntimeError(f"sandbox setup command failed: {r.error}")
        elif t == "sandbox_launch":           # v0.6：在沙箱电脑里启动应用，并等待某个元素出现
            env.launch(list(s["argv"]))
            deadline = time.monotonic() + s.get("timeout", 15)
            while s.get("wait_for") and time.monotonic() < deadline:
                if any(e.name == s["wait_for"] for e in env.observe().elements):
                    break
                time.sleep(0.2)
        else:
            raise ValueError(f"unknown setup step {t}")


def load_tasks(path: str | Path, platform: Optional[str] = None) -> list[dict]:
    p = Path(path)
    files = sorted(p.rglob("*.json")) if p.is_dir() else [p]
    out = []
    for f in files:
        try:
            t = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(t, dict) or "instruction" not in t:
            continue
        if "evaluator" in t and "checks" not in t:     # v0.6：OSWorld 风格任务 → 沙箱任务（子集适配）
            from .osworld import UnsupportedTask, convert
            try:
                t = convert(t)
            except UnsupportedTask as e:
                print(f"skip {f.name}: {e}", file=sys.stderr)
                continue
        t.setdefault("platform", "windows")   # v0.1 任务都是 Windows
        t["_dir"] = str(f.parent.resolve())
        if platform is None or t["platform"] == platform:
            out.append(t)
    return out


def run_task(cfg: dict, task: dict, env, runs_root: str = "runs", rep: int = 0, with_disturbance: bool = True,
             tag: str = "", policy: str = "model", confirm_fn=None, ask_fn=None) -> dict:
    from ..config import build_agent
    from ..logger import TrajectoryLogger
    from .disturb import DisturbanceScheduler

    task_dir = Path(task.get("_dir", "."))
    run_id = f"{tag or 'run'}-{task['id']}-r{rep}-{time.strftime('%H%M%S')}-{os.getpid()}-{id(env) % 10000}"
    if getattr(env, "platform", "") == "remote":
        env.reset("pristine")              # 每个任务从同一沙箱快照开始（可复现）
    else:
        env.reset()
    setup = list(task.get("setup", []))
    if task.get("start_url"):
        setup.insert(0, {"type": "open_url", "url": task["start_url"]})
    do_setup(setup, env, task_dir)
    log = TrajectoryLogger(runs_root, run_id)
    llms = None
    if policy == "scripted":
        from ..scripted import ScriptedPolicy
        llms = ScriptedPolicy(task["demo"]).llms()
        ask_fn = ask_fn or (lambda q: task["demo"].get("answers", {}).get(q, task["demo"].get("default_answer")))
    agent = build_agent(cfg, env, log, llms=llms, task_window=task.get("task_window", ""),
                        confirm_fn=confirm_fn, ask_fn=ask_fn)
    dist = None
    if with_disturbance and task.get("disturbance"):
        d = task["disturbance"]
        dist = DisturbanceScheduler(env, d["kind"], d.get("at_step"), d.get("delay"), task.get("task_window", ""))
        dist.attach(agent)
    try:
        res = agent.run(task["instruction"])
        err = ""
    except Exception as e:  # noqa: BLE001
        res, err = None, f"{type(e).__name__}: {e}"
    ok, notes = run_checks(task.get("checks", []), env)
    row = {
        "task": task["id"], "platform": task.get("platform"), "rep": rep, "passed": ok, "check_notes": notes,
        "status": res.status if res else "crash", "claimed_done": bool(res and res.claimed_done),
        "steps": res.steps if res else 0, "replans": res.replans if res else 0,
        "recoveries": res.recoveries if res else [],
        "calls": res.budget.calls if res else 0,
        "tokens": (res.budget.prompt_tokens + res.budget.completion_tokens) if res else 0,
        "cost_usd": round(res.budget.cost_usd, 6) if res else 0.0,
        "budget_exhausted": bool(res and res.status == "budget_exhausted"),
        "budget_refused_calls": res.budget.refused if res else 0,
        "seconds": round(res.seconds, 1) if res else 0, "disturbed": bool(dist and dist.fired_at),
        "error": err, "run_dir": str(log.dir), "policy": policy,
        "modality": (res.modality or {}).get("modality", {}) if res else {},
        "fallbacks": (res.modality or {}).get("fallbacks", 0) if res else 0,
        "intrusions": (res.modality or {}).get("intrusions", 0) if res else 0,
        "verified_receipts": sum(1 for r in (res.receipts if res else []) if r.get("verdict") == "verified_done"),
    }
    row = log.scrub(row)          # v0.3.1：评测结果行（summary 文件 / stdout）也经过秘密清洗
    log.meta(task={k: v for k, v in task.items() if k != "_dir"}, platform=task.get("platform"),
             result=asdict(res) if res else None, eval=row)
    log.close(report=True)
    do_setup(task.get("teardown", []), env, task_dir)
    return row


def _make_env(cfg: dict, p: str, sandbox=None):
    from ..config import build_env
    if p == "remote" and sandbox is not None:
        env = sandbox.remote_env()
        env.snapshot("pristine")
        return env
    env = build_env(cfg, p)
    if p == "remote":
        env.snapshot("pristine")
    return env


def run_suite_parallel(cfg: dict, tasks: list[dict], runs_root: str = "runs", repeats: int = 1,
                       with_disturbance: bool = True, tag: str = "", policy: str = "model",
                       platform: Optional[str] = None, quiet: bool = False, workers: int = 2,
                       local_sandboxes: bool = True) -> dict:
    """v0.6：多会话并行。每个 worker 线程有自己的环境（remote = 自己的一台本机沙箱电脑，web = 自己的浏览器）。

    任务按 (task, rep) 放进队列；每个 worker 在自己的环境里串行执行，结果行合并后统一汇总。
    """
    import queue
    import threading
    work: "queue.Queue" = queue.Queue()
    for task in tasks:
        if policy == "scripted" and "demo" not in task:
            continue
        for rep in range(repeats):
            work.put((task, rep))
    rows: list[dict] = []
    lock = threading.Lock()
    need_remote = any((platform or t.get("platform")) == "remote" for t in tasks)
    pool_ctx = None
    boxes: list = []
    if need_remote and local_sandboxes:
        from ..sandbox.local import SandboxPool
        pool_ctx = SandboxPool(workers, liveview=False)
        boxes = pool_ctx.__enter__()

    def worker(i: int) -> None:
        envs: dict[str, object] = {}
        try:
            while True:
                try:
                    task, rep = work.get_nowait()
                except queue.Empty:
                    return
                p = platform or task.get("platform", "windows")
                if p not in envs:
                    envs[p] = _make_env(cfg, p, boxes[i] if boxes else None)
                import copy
                row = run_task(copy.deepcopy(cfg), task, envs[p], runs_root, rep, with_disturbance, tag, policy)
                row["worker"] = i
                with lock:
                    rows.append(row)
                if not quiet:
                    print(json.dumps({k: row[k] for k in ("task", "rep", "worker", "passed", "status", "steps")},
                                     ensure_ascii=False), flush=True)
        finally:
            for e in envs.values():
                try:
                    e.close()
                except Exception:
                    pass
    t0 = time.monotonic()
    try:
        threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(max(1, workers))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        if pool_ctx is not None:
            pool_ctx.__exit__(None, None, None)
    summary = summarize(rows)
    summary["workers"] = workers
    summary["wall_seconds"] = round(time.monotonic() - t0, 1)
    Path(runs_root).mkdir(parents=True, exist_ok=True)
    out = Path(runs_root) / f"summary-{tag or 'run'}-parallel-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"config": cfg, "summary": summary, "rows": rows}, ensure_ascii=False, indent=2,
                              default=str), encoding="utf-8")
    if not quiet:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    summary["_file"] = str(out)
    summary["_rows"] = rows
    return summary


def run_suite(cfg: dict, tasks: list[dict], runs_root: str = "runs", repeats: int = 1,
              with_disturbance: bool = True, tag: str = "", policy: str = "model",
              platform: Optional[str] = None, quiet: bool = False) -> dict:
    from ..config import build_env

    envs: dict[str, object] = {}
    rows = []
    try:
        for task in tasks:
            p = platform or task.get("platform", "windows")
            if policy == "scripted" and "demo" not in task:
                if not quiet:
                    print(f"skip {task['id']}: no demo script for --policy scripted")
                continue
            if p not in envs:
                envs[p] = _make_env(cfg, p) if p == "remote" else build_env(cfg, p)
            for rep in range(repeats):
                row = run_task(cfg, task, envs[p], runs_root, rep, with_disturbance, tag, policy)
                rows.append(row)
                if not quiet:
                    print(json.dumps({k: row[k] for k in ("task", "rep", "passed", "status", "steps", "calls",
                                                          "recoveries")}, ensure_ascii=False))
                if row["status"] == "user_abort":
                    break
            if rows and rows[-1]["status"] == "user_abort":
                if not quiet:
                    print("user abort (fail-safe): stopping the suite")
                break
    finally:
        for e in envs.values():
            try:
                e.close()
            except Exception:
                pass
    summary = summarize(rows)
    Path(runs_root).mkdir(parents=True, exist_ok=True)
    out = Path(runs_root) / f"summary-{tag or 'run'}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"config": cfg, "summary": summary, "rows": rows}, ensure_ascii=False, indent=2,
                              default=str), encoding="utf-8")
    if not quiet:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    summary["_file"] = str(out)
    summary["_rows"] = rows
    return summary


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {"runs": 0}
    claimed = [r for r in rows if r["claimed_done"]]
    disturbed = [r for r in rows if r["disturbed"]]

    def mean(k):
        return round(statistics.mean(r[k] for r in rows), 2)
    return {
        "runs": n,
        "success_rate": round(sum(r["passed"] for r in rows) / n, 3),
        "false_done_rate": round(sum(not r["passed"] for r in claimed) / len(claimed), 3) if claimed else None,
        "claimed_done": len(claimed),
        "recovery_rate": round(sum(r["passed"] for r in disturbed) / len(disturbed), 3) if disturbed else None,
        "disturbed_runs": len(disturbed),
        "budget_exhausted_runs": sum(1 for r in rows if r.get("budget_exhausted") or r["status"] == "budget_exhausted"),
        "uncertain_runs": sum(1 for r in rows if r["status"] == "uncertain"),
        "user_abort_runs": sum(1 for r in rows if r["status"] == "user_abort"),
        "avg_steps": mean("steps"), "avg_calls": mean("calls"), "avg_tokens": mean("tokens"),
        "avg_seconds": mean("seconds"),
        "modality_totals": _sum_modality(rows),
        "fallbacks": sum(r.get("fallbacks", 0) for r in rows),
        "background_intrusions": sum(r.get("intrusions", 0) for r in rows),
    }


def _sum_modality(rows: list[dict]) -> dict:
    out: dict = {}
    for r in rows:
        for k, v in (r.get("modality") or {}).items():
            out[k] = out.get(k, 0) + v
    return out

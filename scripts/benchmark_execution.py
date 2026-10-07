"""Reproducible local Chromium benchmark, scripted decisions and no API keys.

Compare with an exact old checkout:
  python scripts/benchmark_execution.py --compare-with /path/to/old/checkout --repeat 3 --output result.json
This measures execution overhead and scripted call counts, not LLM quality.
"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


def measure(root, mode, repeat):
    sys.path.insert(0, str(root))
    from gua.actions import Action
    from gua.config import build_agent, load_config
    from gua.env.web import WebEnv
    from gua.scripted import ScriptedPolicy
    class CountedEnv(WebEnv):
        observations = 0
        def observe(self, *args, **kw):
            self.observations += 1
            return super().observe(*args, **kw)
    cfg = load_config(root / "configs/web_local.yaml", {"agent": {"settle_interval": 0.4},
        "reflection": {"enabled": False}})
    source = json.loads((root / "tasks/web/form_submit.json").read_text())
    demo = source["demo"]
    if mode == "current":
        demo["subgoals"][0]["steps"] = [{"type": "Alice", "target": "Name", "clear": True},
                                        {"type": "alice@example.com", "target": "Email", "clear": True}]
    env = CountedEnv(start_url=str(root / "tasks/web_assets/form.html"))
    samples, drift = [], []
    try:
        for _ in range(repeat):
            env.open(str(root / "tasks/web_assets/form.html"))
            env.observe()  # Exclude launch and first paint equally for both engines.
            env.observations = 0
            agent = build_agent(cfg, env, llms=ScriptedPolicy(demo).llms(), task_window="Contact form")
            started = time.perf_counter()
            result = agent.run(source["instruction"])
            seconds = time.perf_counter() - started
            actual = env.page.evaluate("window.__submitted")
            expected = {"name": "Alice", "email": "alice@example.com", "plan": "pro", "news": True}
            passed = result.claimed_done and actual == expected
            samples.append({"seconds": round(seconds, 4), "steps": result.steps, "calls": result.budget.calls,
                            "observations": env.observations, "passed": passed,
                            "false_done": result.claimed_done and not passed,
                            "performance": getattr(result, "performance", {})})
            if not passed:
                raise RuntimeError(f"form outcome failed: {result.status}, {result.message}")
            env.open("about:blank")
            env.page.set_content('<button id="target" onclick="window.correct=true">Continue</button>')
            obs = env.observe()
            element = next(e for e in obs.elements if e.name == "Continue")
            action = Action("click", x=element.center[0], y=element.center[1], element_id=element.id)
            if mode == "current": action = env.bind_action(action, obs)
            env.page.evaluate("target.style.marginLeft='400px'; document.body.insertAdjacentHTML('beforeend', '<button style=\"position:absolute;left:8px;top:8px\" onclick=\"window.wrong=true\">Other</button>')")
            env.execute(action)
            drift.append(env.page.evaluate("({correct:!!window.correct, wrong:!!window.wrong})"))
        return {"mode": mode, "engine_root": str(root), "python": platform.python_version(),
                "browser": env._browser.version, "viewport": [1280, 800], "samples": samples, "layout_drift": drift}
    finally:
        env.close()


def compare(args):
    rows = {"legacy": [], "current": []}
    motion = {"legacy": [], "current": []}
    with tempfile.TemporaryDirectory(prefix="gui-agent-benchmark-") as directory:
        # Alternate order to reduce warmup/order bias; no competing browsers.
        for i in range(args.repeat):
            for mode in (["legacy", "current"] if i % 2 == 0 else ["current", "legacy"]):
                output = Path(directory) / f"{mode}-{i}.json"
                root = args.compare_with if mode == "legacy" else args.engine_root
                child = subprocess.run([sys.executable, __file__, "--mode", mode, "--engine-root", str(root),
                                        "--repeat", "1", "--output", str(output)], timeout=180,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if child.returncode:
                    raise RuntimeError(f"{mode} benchmark failed:\n{child.stderr}")
                report = json.loads(output.read_text())
                rows[mode].extend(report["samples"])
                motion[mode].extend(report["layout_drift"])
    summary = {}
    for mode, samples in rows.items():
        summary[mode] = {"median_seconds": round(statistics.median(s["seconds"] for s in samples), 4),
                         "median_calls": statistics.median(s["calls"] for s in samples),
                         "median_observations": statistics.median(s["observations"] for s in samples),
                         "passed": sum(s["passed"] for s in samples),
                         "false_done": sum(s["false_done"] for s in samples),
                         "drift_correct": sum(s["correct"] for s in motion[mode]),
                         "drift_wrong": sum(s["wrong"] for s in motion[mode])}
    return {"date": datetime.now(timezone.utc).isoformat(), "baseline_checkout": str(args.compare_with),
            "scope": "Local Chromium execution; fixed scripted decisions; no real model calls or Windows desktop",
            "repeat": args.repeat, "summary": summary, "samples": rows, "layout_drift": motion,
            "speedup": round(summary["legacy"]["median_seconds"] / summary["current"]["median_seconds"], 3)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--compare-with", type=Path)
    parser.add_argument("--mode", choices=["legacy", "current"], default="current")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.repeat <= 50: parser.error("repeat must be 1–50")
    result = compare(args) if args.compare_with else measure(args.engine_root, args.mode, args.repeat)
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(json.dumps(result.get("summary", result), ensure_ascii=False))


if __name__ == "__main__":
    main()

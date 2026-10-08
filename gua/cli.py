"""命令行入口 `gua`。

  gua run --platform web --task "在表单里填写 Alice 并提交" --url tasks/web_assets/form.html
  gua run --platform windows --task "打开记事本输入 hello 并保存" --window 记事本 --model configs/models/qwen_dashscope.yaml
  gua eval tasks/web --policy scripted                 # 无需 API key 的离线演示
  gua eval tasks/windows -c configs/ablations/raw_loop.yaml --repeats 3 --tag raw
  gua replay runs/<run_id> [--open]
  gua doctor
  gua sandbox up [--shell] [--apps gedit ...]           # v0.6：本机启动一台沙箱电脑（Xvfb + AT-SPI + noVNC）
  gua sandbox takeover|handback|status|snapshot|reset --remote-url URL   # 人工接管 / 交还 / 快照
  gua run --platform remote --remote-url URL --app gua-form --task "..."   # 在沙箱电脑里执行任务
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _cfg_path(p: str) -> Path:
    q = Path(p)
    if q.exists():
        return q
    alt = ROOT / p
    if alt.exists():
        return alt
    raise SystemExit(f"config not found: {p}")


def _load(a) -> dict:
    from .config import load_config
    extra = [_cfg_path(m) for m in (a.model or [])]
    over: dict = {}
    if getattr(a, "platform", None):
        over.setdefault("env", {})["platform"] = a.platform
    if getattr(a, "headed", False):
        over.setdefault("env", {}).setdefault("web", {})["headless"] = False
    if getattr(a, "yes", False):
        over.setdefault("safety", {})["mode"] = "allow"
    if getattr(a, "deny", False):
        over.setdefault("safety", {})["mode"] = "deny"
    if getattr(a, "max_steps", None):
        over.setdefault("agent", {})["max_steps"] = a.max_steps
    if getattr(a, "remote_url", None):
        over.setdefault("env", {}).setdefault("remote", {})["url"] = a.remote_url
    if getattr(a, "gui_only", False):
        over.setdefault("hybrid", {})["mode"] = "gui_only"
    return load_config(_cfg_path(a.config), over, extra)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="gua", description="gui-agent：跨平台、可验证执行与失败恢复的 GUI Agent")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("-c", "--config", default="configs/default.yaml")
        p.add_argument("-m", "--model", action="append", help="叠加的模型配置 YAML，可多次")
        p.add_argument("--platform", choices=["auto", "windows", "macos", "linux", "android", "web", "mock"])
        p.add_argument("--runs", default="runs")
        p.add_argument("--headed", action="store_true", help="web：显示浏览器窗口")
        p.add_argument("--yes", action="store_true", help="危险动作自动放行（仅在沙箱/虚拟机里用）")
        p.add_argument("--deny", action="store_true", help="危险动作一律拒绝")
        p.add_argument("--max-steps", type=int)
        p.add_argument("--remote-url", help="remote：沙箱守护进程地址（token 从 GUA_SANDBOX_TOKEN 读取）")
        p.add_argument("--gui-only", action="store_true", help="消融：关闭混合动作空间（语义动作改写成前台输入）")

    r = sub.add_parser("run", help="运行单个自然语言任务")
    r.add_argument("instruction", nargs="?")
    r.add_argument("-t", "--task", help="任务描述（与位置参数二选一）")
    r.add_argument("--window", default="", help="任务窗口标题片段 / Android 包名 / 页面标题，用于焦点检查")
    r.add_argument("--url", help="web：起始页面（URL 或本地 HTML 路径）")
    r.add_argument("--app", help="android/desktop：先打开的应用")
    common(r)

    e = sub.add_parser("eval", help="运行任务集并判分")
    e.add_argument("tasks")
    e.add_argument("--repeats", type=int, default=1)
    e.add_argument("--no-disturb", action="store_true")
    e.add_argument("--tag", default="")
    e.add_argument("--policy", choices=["model", "scripted"], default="model",
                   help="scripted = 用任务里的 demo 脚本代替模型（离线、无需 API key）")
    common(e)

    rp = sub.add_parser("replay", help="把一次运行渲染成 HTML 回放报告")
    rp.add_argument("run_dir")
    rp.add_argument("--open", action="store_true")

    sub.add_parser("doctor", help="检查各平台后端依赖")
    sb = sub.add_parser("sandbox", help="v0.6：沙箱电脑（启动 / 接管 / 交还 / 快照 / 重置）")
    sb.add_argument("op", choices=["up", "status", "takeover", "handback", "snapshot", "reset", "liveview"])
    sb.add_argument("--remote-url", default="http://127.0.0.1:8765")
    sb.add_argument("--name", default="default", help="snapshot / reset 的快照名")
    sb.add_argument("--shell", action="store_true", help="up：允许在沙箱里执行 shell 工具")
    sb.add_argument("--apps", nargs="*", default=[], help="up：沙箱里允许启动的应用")
    sb.add_argument("--port", type=int, default=8765)
    d = sub.add_parser("demo", help="离线演示：用脚本策略跑 tasks/web（需要 playwright）")
    d.add_argument("--headed", action="store_true")
    d.add_argument("--runs", default="runs")

    a = ap.parse_args(argv)

    if a.cmd == "doctor":
        from .env import detect_platform, platform_status
        print(f"auto-detected platform: {detect_platform()}")
        for k, v in platform_status().items():
            print(f"  {k:8s} {v}")
        return
    if a.cmd == "sandbox":
        return _sandbox(a)
    if a.cmd == "replay":
        from .report import build_report
        out = build_report(a.run_dir)
        print(out)
        if a.open:
            import webbrowser
            webbrowser.open(out.resolve().as_uri())
        return
    if a.cmd == "demo":
        a.config, a.model, a.platform, a.yes, a.deny, a.max_steps = "configs/default.yaml", [], "web", False, True, None
        a.tasks, a.repeats, a.no_disturb, a.tag, a.policy = str(ROOT / "tasks" / "web"), 1, False, "demo", "scripted"
        a.cmd = "eval"

    cfg = _load(a)
    if a.cmd == "run":
        text = a.task or a.instruction
        if not text:
            raise SystemExit("请用 --task 或位置参数给出任务描述")
        from .config import build_agent, build_env, resolve_platform
        from .logger import TrajectoryLogger
        env = build_env(cfg)
        try:
            if a.url:
                from .env.web import to_url
                env.open(to_url(a.url, Path.cwd()))
            if a.app:
                from .actions import Action
                if getattr(env, "platform", "") == "remote":
                    print(json.dumps(env.launch([a.app]), ensure_ascii=False))
                    time.sleep(2.0)
                else:
                    env.execute(Action("open_app", app=a.app))
            log = TrajectoryLogger(a.runs)
            agent = build_agent(cfg, env, log, task_window=a.window)
            res = agent.run(text)
            report = log.close()
        finally:
            env.close()
        print(json.dumps(asdict(res), ensure_ascii=False, indent=2, default=str))
        print(f"platform: {resolve_platform(cfg)}\ntrajectory: {log.dir}\nreport: {report}")
    elif a.cmd == "eval":
        from .eval.runner import load_tasks, run_suite
        tasks = load_tasks(a.tasks)
        if not tasks:
            raise SystemExit(f"no tasks found in {a.tasks}")
        plat = a.platform if a.platform and a.platform != "auto" else None
        s = run_suite(cfg, tasks, a.runs, a.repeats, not a.no_disturb, a.tag, a.policy, platform=plat)
        print(f"summary file: {s['_file']}")


def _sandbox(a) -> None:
    import os
    if a.op == "up":
        from .sandbox.local import LocalSandbox, requirements
        print(json.dumps(requirements()))
        sb = LocalSandbox(port=a.port, shell=a.shell, apps=a.apps).start()
        print(json.dumps({"url": sb.url, "token_env": "GUA_SANDBOX_TOKEN", "token": sb.token,
                          "liveview": sb.liveview_url, "takeover": sb.takeover_url, "workdir": sb.workdir,
                          "apps": ["gua-form"] + list(a.apps)}, ensure_ascii=False, indent=2))
        print("Ctrl-C 结束并清理沙箱", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            sb.stop()
        return
    from .env.remote import RemoteEnv
    env = RemoteEnv(a.remote_url, os.environ.get("GUA_SANDBOX_TOKEN", ""))
    fn = {"status": env.health, "takeover": env.takeover, "handback": env.handback, "liveview": env.live_view,
          "snapshot": lambda: env.snapshot(a.name), "reset": lambda: env.reset(a.name) or {"ok": True}}[a.op]
    print(json.dumps(fn(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])

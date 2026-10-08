"""OSWorld 风格任务 JSON → gua 沙箱任务（v0.6，**子集**适配，不是 OSWorld 官方 harness）。

OSWorld 任务的大致形状（https://github.com/xlang-ai/OSWorld，evaluation_examples/）：
  {"id", "instruction", "config": [{"type": "launch"|"execute"|"command"|"sleep"|..., "parameters": {...}}],
   "evaluator": {"func": "...", "result": {"type": "vm_file"|"vm_command_line", ...}, "expected": {...}}}

这里只转换能在 gua 沙箱里忠实复现的部分：
  config：launch（白名单应用）/ execute、command（沙箱 shell，需 --shell）/ sleep
  evaluator.func：exact_match、check_include_exclude；result：vm_file（相对工作目录或绝对路径）、vm_command_line
其余（download、open 远程 URL、chrome/libreoffice 专用比较器、getter 组合……）一律抛 UnsupportedTask，
**不会**被悄悄换成更宽松的判分。适配器的价值在于：同一套“应用自己写出的状态”判分逻辑，可以比较
gua 的不同配置（gui_only / hybrid、前台 / 后台、有无恢复），而不是宣称 OSWorld 成绩。
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from ..actions import Action


class UnsupportedTask(ValueError):
    pass


def _argv(cmd: Any) -> list[str]:
    if isinstance(cmd, str):
        return shlex.split(cmd)
    if isinstance(cmd, list) and all(isinstance(c, str) for c in cmd):
        return list(cmd)
    raise UnsupportedTask(f"command must be a string or argv list, got {type(cmd).__name__}")


def convert(task: dict) -> dict:
    setup = []
    for step in task.get("config", []) or []:
        t, p = step.get("type"), step.get("parameters") or {}
        if t == "launch":
            setup.append({"type": "sandbox_launch", "argv": _argv(p.get("command")), "wait_for": p.get("wait_for"),
                          "timeout": p.get("timeout", 15)})
        elif t in {"execute", "command"}:
            setup.append({"type": "sandbox_shell", "argv": _argv(p.get("command"))})
        elif t == "sleep":
            setup.append({"type": "sleep", "seconds": float(p.get("seconds", 1))})
        else:
            raise UnsupportedTask(f"config step type {t!r} is not supported by the gua adapter")
    ev = task.get("evaluator") or {}
    func = ev.get("func")
    if isinstance(func, list):
        raise UnsupportedTask("multi-metric evaluators are not supported")
    if func not in {"exact_match", "check_include_exclude"}:
        raise UnsupportedTask(f"evaluator func {func!r} is not supported")
    res = ev.get("result") or {}
    if res.get("type") not in {"vm_file", "vm_command_line"}:
        raise UnsupportedTask(f"result getter {res.get('type')!r} is not supported")
    return {"id": f"osworld_{task.get('id', 'task')}", "platform": "remote", "instruction": task["instruction"],
            "setup": setup, "checks": [{"type": "osworld_eval", "func": func, "result": res,
                                        "expected": ev.get("expected") or {}}],
            "source": {"format": "osworld-subset", "id": task.get("id")}}


def load_osworld(path: str | Path) -> list[dict]:
    p = Path(path)
    files = sorted(p.rglob("*.json")) if p.is_dir() else [p]
    out, skipped = [], []
    for f in files:
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
            if "evaluator" not in raw:
                continue
            t = convert(raw)
            t["_dir"] = str(f.parent.resolve())
            out.append(t)
        except (UnsupportedTask, KeyError, json.JSONDecodeError) as e:
            skipped.append((str(f), str(e)))
    return out


def get_result(env, res: dict) -> str:
    if res["type"] == "vm_file":
        path = res.get("path", "")
        if path.startswith("/"):
            r = env.run_tool(Action("shell", command=["cat", path]))
            return r.output if r.ok else ""
        return env.read_file(path)
    r = env.run_tool(Action("shell", command=_argv(res.get("command"))))
    return r.output if r.ok else ""


def evaluate(env, func: str, result: dict, expected: dict) -> tuple[bool, str]:
    got = get_result(env, result)
    rules = expected.get("rules", expected) if isinstance(expected, dict) else {}
    if func == "exact_match":
        want = rules.get("expected", "")
        ok = got.strip() == str(want).strip()
        return ok, f"exact_match: got {got.strip()[:80]!r}, expected {str(want)[:80]!r}"
    inc, exc = list(rules.get("include") or []), list(rules.get("exclude") or [])
    ok = all(s in got for s in inc) and not any(s in got for s in exc)
    return ok, f"check_include_exclude include={inc} exclude={exc}: {ok}"

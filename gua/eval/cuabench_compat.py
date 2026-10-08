"""Cua-Bench 风格任务适配（v0.6，子集兼容层，**不是**官方 cua-bench 包）。

Cua-Bench 的任务是一个带 `main.py` 的目录：`@cb.tasks_config` 列出变体（cb.Task），`@cb.setup_task` 准备沙箱，
`@cb.solve_task` 是 oracle（“oracle 失败 = 任务本身坏了”），`@cb.evaluate_task` 返回 `list[float]`，奖励取平均。
接口来源：https://cua.ai/docs/cua-bench/reference/task-definition

这里提供同名的 `Task` / 装饰器 / `DesktopSession` 子集，跑在 gua 的沙箱电脑（RemoteEnv）上，于是：
- 官方文档里的示例任务（hello_file_env）可以不改代码地运行（需要沙箱以 --shell 启动，因为它用绝对路径与 shell）；
- 同一个任务可以用 oracle 跑（检查任务本身）或用 gua agent 跑（检查 agent），判分完全由任务自己的 evaluate 决定。

未实现：bench_ui / pywebview 窗口（launch_window、execute_javascript、get_element_rect、click_element）、
Fleet / 云端 provider、split 之外的 CLI 选项。调用未实现的方法会抛 NotImplementedError，而不是静默成功。
"""
from __future__ import annotations

import asyncio
import importlib.util
import statistics
import sys
import time
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..actions import Action

_REG: dict[str, dict[str, Callable]] = {}


@dataclass
class Task:
    description: str
    task_id: Optional[str] = None
    metadata: Optional[dict] = None
    computer: Optional[dict] = None


def _decorator(kind: str):
    def outer(arg=None, split: str = "train"):
        def register(fn, sp):
            _REG.setdefault(sp, {})[kind] = fn
            return fn
        if callable(arg):
            return register(arg, split)
        sp = arg if isinstance(arg, str) else split
        return lambda fn: register(fn, sp)
    return outer


tasks_config = _decorator("tasks_config")
setup_task = _decorator("setup_task")
solve_task = _decorator("solve_task")
evaluate_task = _decorator("evaluate_task")


class DesktopSession:
    """cb.DesktopSession 的子集，委托给 gua RemoteEnv（沙箱守护进程）。"""

    def __init__(self, env):
        self.env = env

    # -- shell / files（绝对路径经 shell 访问，需要沙箱 --shell；相对路径走受限文件接口）
    async def run_command(self, command: str, *, check: bool = True, timeout: Optional[float] = None) -> dict:
        r = self.env.run_tool(Action("shell", command=["sh", "-c", command]))
        res = {"stdout": r.output, "stderr": "", "returncode": 0 if r.ok else 1, "success": r.ok}
        if check and not r.ok:
            raise RuntimeError(f"command failed: {r.error}")
        return res

    shell_command = run_command

    async def read_file(self, path: str) -> str:
        if path.startswith("/"):
            r = self.env.run_tool(Action("shell", command=["cat", path]))
            if not r.ok:
                raise FileNotFoundError(path)
            return r.output
        return self.env.read_file(path)

    async def write_file(self, path: str, content: str) -> None:
        if path.startswith("/"):
            r = self.env.run_tool(Action("shell", command=["python3", "-c",
                                                         "import sys;open(sys.argv[1],'w').write(sys.argv[2])",
                                                         path, content]))
        else:
            r = self.env.run_tool(Action("file", method="write", path=path, text=content))
        if not r.ok:
            raise OSError(r.error)

    async def file_exists(self, path: str) -> bool:
        if path.startswith("/"):
            return self.env.run_tool(Action("shell", command=["test", "-f", path])).ok
        return self.env.run_tool(Action("file", method="read", path=path)).ok

    async def directory_exists(self, path: str) -> bool:
        return self.env.run_tool(Action("shell", command=["test", "-d", path])).ok

    async def list_dir(self, path: str) -> list[str]:
        r = self.env.run_tool(Action("file", method="list", path=path))
        return [x.rstrip("/") for x in r.output.splitlines()] if r.ok else []

    # -- apps / state
    async def launch_application(self, app_name: str) -> None:
        r = self.env.launch([app_name])
        if not r.get("ok"):
            raise RuntimeError(r.get("error"))

    launch_app = launch_application

    async def screenshot(self) -> bytes:
        return self.env._req("GET", "/screenshot", raw=True)

    async def get_accessibility_tree(self) -> dict:
        return (self.env._req("GET", "/observe?elements=1") or {}).get("tree") or {}

    async def check_status(self) -> bool:
        return bool(self.env.health().get("ok"))

    async def wait_until_ready(self, timeout: int = 60, poll_interval: float = 2.0) -> bool:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if await self.check_status():
                return True
            await asyncio.sleep(poll_interval)
        return False

    @property
    def vnc_url(self) -> str:
        return (self.env.live_view() or {}).get("liveview", "")

    @property
    def os_type(self) -> str:
        return "linux"

    # -- input
    def _do(self, **a) -> None:
        r = self.env.execute(Action(**a))
        if not r.ok:
            raise RuntimeError(r.error)

    async def click(self, x: int, y: int) -> None:
        self._do(type="click", x=x, y=y)

    async def right_click(self, x: int, y: int) -> None:
        self._do(type="right_click", x=x, y=y)

    async def double_click(self, x: int, y: int) -> None:
        self._do(type="double_click", x=x, y=y)

    async def move_to(self, x: int, y: int) -> None:
        self._do(type="move", x=x, y=y)

    async def drag(self, from_x: int, from_y: int, to_x: int, to_y: int) -> None:
        self._do(type="drag", x=from_x, y=from_y, x2=to_x, y2=to_y)

    async def type(self, text: str) -> None:  # noqa: A003
        self._do(type="type", text=text)

    async def key(self, key: str) -> None:
        self._do(type="hotkey", keys=[key])

    async def hotkey(self, keys: list[str]) -> None:
        self._do(type="hotkey", keys=list(keys))

    async def scroll(self, direction: str = "down", amount: int = 300) -> None:
        self._do(type="scroll", direction=direction, amount=max(1, int(amount) // 100))

    def __getattr__(self, name):
        if name in {"launch_window", "execute_javascript", "get_element_rect", "click_element",
                    "right_click_element", "serve_static", "install_app", "page", "sandbox", "computer"}:
            raise NotImplementedError(f"cb.DesktopSession.{name} is not supported by the gua adapter")
        raise AttributeError(name)


def shim_module() -> types.ModuleType:
    m = types.ModuleType("cua_bench")
    m.Task, m.DesktopSession = Task, DesktopSession
    m.tasks_config, m.setup_task, m.solve_task, m.evaluate_task = tasks_config, setup_task, solve_task, evaluate_task
    m.__gua_shim__ = True
    return m


def load_task_module(task_dir: str | Path, force_shim: bool = True) -> dict[str, dict[str, Callable]]:
    """导入任务目录的 main.py，返回 {split: {kind: fn}}。默认把本兼容层注册为 `cua_bench`。"""
    _REG.clear()
    prev = sys.modules.get("cua_bench")
    if force_shim or prev is None:
        sys.modules["cua_bench"] = shim_module()
    try:
        path = Path(task_dir) / "main.py"
        spec = importlib.util.spec_from_file_location(f"cb_task_{abs(hash(str(path)))}", path)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
    finally:
        if prev is not None:
            sys.modules["cua_bench"] = prev
        else:
            sys.modules.pop("cua_bench", None)
    return {k: dict(v) for k, v in _REG.items()}


def _call(fn, *a):
    r = fn(*a)
    return asyncio.run(r) if asyncio.iscoroutine(r) else r


def run_task_dir(task_dir: str | Path, env, mode: str = "oracle", split: str = "train",
                 agent_factory: Optional[Callable[[Any, Task], Any]] = None) -> list[dict]:
    """逐变体运行：reset → setup → (oracle | gua agent) → evaluate。返回每个变体的结果行。

    agent_factory(env, task) 返回一个有 .run(description) 的对象（通常是 build_agent 的结果）。
    """
    reg = load_task_module(task_dir).get(split) or {}
    if "tasks_config" not in reg or "evaluate_task" not in reg:
        raise ValueError(f"{task_dir}: needs @tasks_config and @evaluate_task")
    session = DesktopSession(env)
    rows = []
    for i, task in enumerate(_call(reg["tasks_config"])):
        env.reset("pristine")
        if "setup_task" in reg:
            _call(reg["setup_task"], task, session)
        status, receipts = "", []
        t0 = time.monotonic()
        if mode == "oracle":
            if "solve_task" not in reg:
                raise ValueError("no @solve_task oracle in this task")
            _call(reg["solve_task"], task, session)
            status = "oracle"
        else:
            agent = agent_factory(env, task)
            res = agent.run(task.description)
            status, receipts = res.status, res.receipts
        scores = _call(reg["evaluate_task"], task, session)
        scores = [float(x) for x in (scores if isinstance(scores, (list, tuple)) else [scores])]
        reward = statistics.mean(scores) if scores else 0.0
        rows.append({"task_dir": str(task_dir), "variant": task.task_id or i, "mode": mode, "status": status,
                     "scores": scores, "reward": reward, "passed": reward >= 1.0,
                     "verified_receipts": sum(1 for r in receipts if r.get("verdict") == "verified_done"),
                     "seconds": round(time.monotonic() - t0, 2)})
    return rows

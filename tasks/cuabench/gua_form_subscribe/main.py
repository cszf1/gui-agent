"""Cua-Bench 风格 GUI 任务：在 gua 沙箱的 Gua Form（GTK + AT-SPI）里填表并保存。

oracle 只用真实输入（按无障碍树里的元素中心点击 / 输入），评分只读应用自己写出的 result.json。
路径 result.json 是相对沙箱工作目录的（gua 兼容层约定；官方 cua-bench 通常用绝对路径）。
"""
import json

import cua_bench as cb

NAMES = ["Ada", "Lin"]


@cb.tasks_config(split="train")
def load():
    return [cb.Task(description=f"In Gua Form, type {n} into Name, tick Subscribe and press Save.",
                    task_id=f"subscribe-{n.lower()}", metadata={"name": n},
                    computer={"provider": "native", "setup_config": {"os_type": "linux"}}) for n in NAMES]


def _walk(node):
    yield node
    for c in node.get("children", []) or []:
        yield from _walk(c)


async def _center(session, name):
    tree = await session.get_accessibility_tree()
    for n in _walk(tree):
        if n.get("name") == name and n.get("extents"):
            x, y, w, h = n["extents"]
            return x + w // 2, y + h // 2
    raise LookupError(name)


@cb.setup_task(split="train")
async def setup(task_cfg, session: cb.DesktopSession):
    await session.launch_application("gua-form")
    for _ in range(60):
        try:
            await _center(session, "Save")
            return
        except LookupError:
            import asyncio
            await asyncio.sleep(0.25)


@cb.solve_task(split="train")
async def solve(task_cfg, session: cb.DesktopSession):
    await session.click(*await _center(session, "Name"))
    await session.type(task_cfg.metadata["name"])
    await session.click(*await _center(session, "Subscribe"))
    await session.click(*await _center(session, "Save"))


@cb.evaluate_task(split="train")
async def evaluate(task_cfg, session: cb.DesktopSession) -> list[float]:
    if not await session.file_exists("result.json"):
        return [0.0, 0.0]
    got = json.loads(await session.read_file("result.json"))
    return [1.0 if got.get("name") == task_cfg.metadata["name"] else 0.0,
            1.0 if got.get("subscribe") is True else 0.0]

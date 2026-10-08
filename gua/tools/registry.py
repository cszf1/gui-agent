"""代码 / 文件 / 应用 API 通道（v0.6 混合动作空间的非 GUI 部分）。

参考 UFO² 的 GUI–API 混合动作层（API 优先、GUI 兜底）、Agent S3 的原生 coding agent、
Cua 的 “Computer-Use 2.0”（同一任务里混用代码、API 与 GUI）。本项目的取舍：

- **默认全部关闭**。shell 必须显式 `tools.shell.enabled: true` 且可执行文件在 `allow` 白名单里；
  文件工具必须给出 `tools.files.root`，所有路径限定在根目录内（拒绝 ..、绝对路径、符号链接逃逸）；
  API 工具只能调用通过 `ToolRegistry.register_api` 注册、带 JSON 参数说明的 Python 函数。
- shell 从不经过 shell 解释器（argv 直接 exec），工作目录固定为沙箱目录，环境变量最小化（不继承密钥），
  有超时、输出截断；POSIX 上再加 CPU / 内存 / 文件大小 rlimit。可选 `sandbox_prefix`（例如 bwrap / firejail）
  包一层。真正的隔离建议用 RemoteEnv：shell / file 直接在一次性沙箱电脑里执行（见 gua/sandbox）。
- 这里只负责“能不能执行、怎么执行”；“该不该执行”仍由 SafetyGuard 统一决定（危险模式、确认、拒绝记忆），
  执行输出由 agent 的 Scrubber 清洗后才进入日志与提示词。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..env.base import ExecResult

SHELL_METACHARS = set("|;&`$<>\n\r")


class ToolDenied(Exception):
    """配置层面不允许（与 SafetyGuard 的 confirm/deny 区分：这里是能力边界）。"""


@dataclass
class ApiTool:
    name: str
    fn: Callable[..., Any]
    description: str = ""
    params: dict = field(default_factory=dict)     # {参数名: "类型 / 说明"}
    risky: bool = False                            # True → SafetyGuard 要求确认（对外可见 / 不可逆副作用）

    def doc(self) -> str:
        ps = ", ".join(f"{k}: {v}" for k, v in self.params.items())
        return f"{self.name}({ps}){' [needs confirmation]' if self.risky else ''} — {self.description}"


@dataclass
class ShellConfig:
    enabled: bool = False
    allow: list[str] = field(default_factory=list)   # 可执行文件名（basename）白名单
    workdir: Optional[str] = None
    timeout: float = 20.0
    max_output: int = 8000
    confirm: bool = True                             # True：每条命令都要人工确认（SafetyGuard）
    sandbox_prefix: list[str] = field(default_factory=list)
    env_passthrough: list[str] = field(default_factory=lambda: ["PATH", "LANG", "LC_ALL", "SYSTEMROOT", "TEMP",
                                                                "TMP"])


@dataclass
class FilesConfig:
    root: Optional[str] = None                       # None = 文件工具关闭
    max_bytes: int = 200_000
    writable: bool = True


class ToolRegistry:
    def __init__(self, shell: Optional[ShellConfig] = None, files: Optional[FilesConfig] = None):
        self.shell = shell or ShellConfig()
        self.files = files or FilesConfig()
        self.apis: dict[str, ApiTool] = {}

    # ------------------------------------------------------------------ 配置
    @classmethod
    def from_config(cls, cfg: dict) -> "ToolRegistry":
        t = (cfg or {}).get("tools") or {}
        sh = t.get("shell") or {}
        fl = t.get("files") or {}
        reg = cls(ShellConfig(enabled=bool(sh.get("enabled", False)), allow=list(sh.get("allow") or []),
                              workdir=sh.get("workdir"), timeout=float(sh.get("timeout", 20.0)),
                              max_output=int(sh.get("max_output", 8000)), confirm=bool(sh.get("confirm", True)),
                              sandbox_prefix=list(sh.get("sandbox_prefix") or [])),
                  FilesConfig(root=fl.get("root"), max_bytes=int(fl.get("max_bytes", 200_000)),
                              writable=bool(fl.get("writable", True))))
        return reg

    def register_api(self, name: str, fn: Callable[..., Any], description: str = "", params: Optional[dict] = None,
                     risky: bool = False) -> ApiTool:
        if not name or not name.replace("_", "").replace(".", "").isalnum():
            raise ValueError(f"invalid api tool name {name!r}")
        tool = ApiTool(name, fn, description, dict(params or {}), risky)
        self.apis[name] = tool
        return tool

    def available(self) -> dict[str, bool]:
        return {"shell": self.shell.enabled and bool(self.shell.allow), "file": self.files.root is not None,
                "api": bool(self.apis)}

    def prompt_docs(self) -> list[str]:
        """给 actor 的动作说明（只列出真正启用的通道）。"""
        out = []
        av = self.available()
        if av["shell"]:
            out.append('{"type":"shell", "command":["<exe>", "arg", ...]}   run an allowlisted program (no shell '
                       f'syntax); allowed: {", ".join(sorted(self.shell.allow))}')
        if av["file"]:
            out.append('{"type":"file", "method":"read|write|append|list", "path":"<relative path>", "text":"..."} '
                       '  files inside the task workspace')
        for t in self.apis.values():
            out.append('{"type":"api", "tool":"' + t.name + '", "args":{...}}   ' + t.doc())
        return out

    # ------------------------------------------------------------------ 能力边界（SafetyGuard 也会调用）
    def check(self, a) -> Optional[str]:
        """返回拒绝原因（能力边界）；允许返回 None。"""
        if a.type == "shell":
            if not self.shell.enabled:
                return "shell tool is disabled (tools.shell.enabled=false)"
            exe = os.path.basename(a.command[0]) if a.command else ""
            if sys.platform == "win32":
                exe = exe.lower().removesuffix(".exe")
            allow = {(x.lower().removesuffix(".exe") if sys.platform == "win32" else x) for x in self.shell.allow}
            if exe not in allow:
                return f"executable {exe!r} not in tools.shell.allow"
            if os.sep in a.command[0] or (os.altsep and os.altsep in a.command[0]):
                return "executable must be a bare allowlisted name, not a path"
            return None
        if a.type == "file":
            if self.files.root is None:
                return "file tool is disabled (tools.files.root not set)"
            if a.method in {"write", "append"} and not self.files.writable:
                return "file tool is read-only"
            try:
                self._resolve(a.path or ".")
            except ToolDenied as e:
                return str(e)
            return None
        if a.type == "api":
            if a.tool not in self.apis:
                return f"api tool {a.tool!r} is not registered"
            return None
        return f"not a tool action: {a.type}"

    def is_risky(self, a) -> Optional[str]:
        """需要人工确认的原因（交给 SafetyGuard 合并进 confirm）。"""
        if a.type == "shell" and self.shell.confirm:
            return "shell command (tools.shell.confirm=true)"
        if a.type == "api" and a.tool in self.apis and self.apis[a.tool].risky:
            return f"api tool {a.tool!r} is marked risky"
        if a.type == "file" and a.method == "write":
            try:
                if self._resolve(a.path).exists():
                    return "overwriting an existing file"
            except ToolDenied:
                return None
        return None

    def _resolve(self, rel: str) -> Path:
        root = Path(self.files.root or ".").resolve()
        if rel is None:
            raise ToolDenied("file path missing")
        if "\x00" in rel:
            raise ToolDenied("path contains NUL")
        p = Path(rel)
        if p.is_absolute() or p.drive or any(part == ".." for part in p.parts):
            raise ToolDenied("path must be relative to the workspace root and must not contain '..'")
        full = (root / p).resolve()
        if full != root and root not in full.parents:
            raise ToolDenied("path escapes the workspace root (symlink?)")
        return full

    # ------------------------------------------------------------------ 执行
    def execute(self, a) -> ExecResult:
        t0 = time.time()
        why = self.check(a)
        if why:
            return ExecResult(False, f"blocked_by_safety: {why}", t0, time.time(), route=f"{a.type}:denied")
        try:
            if a.type == "shell":
                return self._shell(a, t0)
            if a.type == "file":
                return self._file(a, t0)
            return self._api(a, t0)
        except ToolDenied as e:
            return ExecResult(False, f"blocked_by_safety: {e}", t0, time.time(), route=f"{a.type}:denied")
        except Exception as e:  # noqa: BLE001
            return ExecResult(False, f"tool_error: {type(e).__name__}: {str(e)[:200]}", t0, time.time(),
                              route=a.type)

    def _truncate(self, s: str) -> str:
        m = self.shell.max_output
        return s if len(s) <= m else s[:m] + f"\n...[truncated {len(s) - m} chars]"

    def _shell(self, a, t0: float) -> ExecResult:
        wd = Path(self.shell.workdir or self.files.root or ".").resolve()
        wd.mkdir(parents=True, exist_ok=True)
        env = {k: os.environ[k] for k in self.shell.env_passthrough if k in os.environ}
        env["HOME"] = str(wd)
        argv = list(self.shell.sandbox_prefix) + list(a.command)
        kw: dict = {}
        if os.name == "posix":
            def limits():  # pragma: no cover - runs in the child
                import resource
                for lim, val in ((resource.RLIMIT_CPU, int(self.shell.timeout) + 1),
                                 (resource.RLIMIT_FSIZE, 50 * 1024 * 1024),
                                 (resource.RLIMIT_AS, 2 * 1024 * 1024 * 1024)):
                    try:
                        resource.setrlimit(lim, (val, val))
                    except (ValueError, OSError):
                        pass
                os.setsid()
            kw["preexec_fn"] = limits
        try:
            p = subprocess.run(argv, cwd=str(wd), env=env, capture_output=True, text=True,
                               timeout=self.shell.timeout, stdin=subprocess.DEVNULL, shell=False,
                               encoding="utf-8", errors="replace", **kw)
        except subprocess.TimeoutExpired:
            return ExecResult(False, f"tool_error: timeout after {self.shell.timeout}s", t0, time.time(),
                              route="shell")
        except FileNotFoundError:
            return ExecResult(False, f"tool_error: executable not found: {a.command[0]}", t0, time.time(),
                              route="shell")
        out = self._truncate((p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr else ""))
        if p.returncode != 0:
            return ExecResult(False, f"tool_error: exit code {p.returncode}", t0, time.time(), output=out,
                              route="shell")
        return ExecResult(True, "", t0, time.time(), output=out, route="shell")

    def _file(self, a, t0: float) -> ExecResult:
        full = self._resolve(a.path or ".")
        m = a.method
        if m == "list":
            if not full.is_dir():
                return ExecResult(False, "tool_error: not a directory", t0, time.time(), route="file")
            names = sorted((p.name + ("/" if p.is_dir() else "")) for p in full.iterdir())[:500]
            return ExecResult(True, "", t0, time.time(), output="\n".join(names), route="file")
        if m == "read":
            if not full.is_file():
                return ExecResult(False, "tool_error: file not found", t0, time.time(), route="file")
            data = full.read_bytes()[: self.files.max_bytes]
            return ExecResult(True, "", t0, time.time(), output=data.decode("utf-8", errors="replace"), route="file")
        text = a.text or ""
        if len(text.encode("utf-8")) > self.files.max_bytes:
            return ExecResult(False, "tool_error: content too large", t0, time.time(), route="file")
        full.parent.mkdir(parents=True, exist_ok=True)
        if full.is_symlink():
            raise ToolDenied("refusing to write through a symlink")
        with open(full, "a" if m == "append" else "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return ExecResult(True, "", t0, time.time(), output=f"{m} {len(text)} chars to {a.path}", route="file")

    def _api(self, a, t0: float) -> ExecResult:
        tool = self.apis[a.tool]
        res = tool.fn(**(a.args or {}))
        out = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False, default=str)
        return ExecResult(True, "", t0, time.time(), output=self._truncate(out), route=f"api:{tool.name}")

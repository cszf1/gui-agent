"""本机启动沙箱电脑（不需要 Docker）：Xvfb + dbus + AT-SPI + openbox + （可选）x11vnc/noVNC + gua 守护进程。

与 sandbox/Dockerfile 的 entrypoint 做同样的事，只是直接在当前 Linux 上起进程，便于 CI / 开发机 / 没有
Docker 的环境做端到端测试。每个 LocalSandbox 用独立的 DISPLAY、端口、工作目录，因此可以并行开多个
（SandboxPool），对应“多台一次性沙箱电脑并行跑评测”。

隔离程度说明（诚实）：本机模式下沙箱进程与当前用户同权限，只是一个独立的 X 显示器 + 独立工作目录；
真正的隔离请用 Docker 镜像（--cap-drop ALL、只读根文件系统、独立网络命名空间），或 VM。
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
DAEMON = HERE / "daemon.py"
FORM_APP = HERE / "apps" / "gua_form.py"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def free_display(start: int = 50) -> int:
    for n in range(start, start + 200):
        if not Path(f"/tmp/.X11-unix/X{n}").exists() and not Path(f"/tmp/.X{n}-lock").exists():
            return n
    raise RuntimeError("no free X display number")


def python_with(module: str) -> Optional[str]:
    for cand in (sys.executable, "/usr/bin/python3", shutil.which("python3") or ""):
        if cand and subprocess.run([cand, "-c", f"import {module}"], capture_output=True).returncode == 0:
            return cand
    return None


def requirements() -> dict[str, bool]:
    return {"Xvfb": bool(shutil.which("Xvfb")), "xdotool": bool(shutil.which("xdotool")),
            "dbus-launch": bool(shutil.which("dbus-launch")), "openbox": bool(shutil.which("openbox")),
            "x11vnc": bool(shutil.which("x11vnc")), "websockify": bool(shutil.which("websockify")),
            "pyatspi": python_with("pyatspi") is not None, "gtk": python_with("gi") is not None}


@dataclass
class LocalSandbox:
    display: Optional[int] = None
    port: int = 0
    workdir: Optional[str] = None
    size: tuple[int, int] = (1280, 800)
    shell: bool = False
    apps: list[str] = field(default_factory=list)
    liveview: bool = True
    procs: list = field(default_factory=list)
    token: str = field(default_factory=lambda: secrets.token_urlsafe(18))
    url: str = ""
    liveview_url: str = ""
    takeover_url: str = ""
    _tmp: Optional[str] = None
    env: dict = field(default_factory=dict)

    def _spawn(self, argv, **kw):
        p = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.env,
                             start_new_session=True, **kw)
        self.procs.append(p)
        return p

    def start(self, timeout: float = 20.0) -> "LocalSandbox":
        req = requirements()
        missing = [k for k in ("Xvfb", "xdotool") if not req[k]]
        if missing:
            raise RuntimeError(f"local sandbox needs {', '.join(missing)} (apt install xvfb xdotool)")
        self.display = self.display if self.display is not None else free_display()
        self.port = self.port or free_port()
        self._tmp = tempfile.mkdtemp(prefix="gua-sandbox-")
        self.workdir = self.workdir or os.path.join(self._tmp, "work")
        os.makedirs(self.workdir, exist_ok=True)
        disp = f":{self.display}"
        self.env = {k: v for k, v in os.environ.items() if not k.upper().endswith("_PROXY")}
        self.env.update(DISPLAY=disp, NO_AT_BRIDGE="0", GTK_MODULES="gail:atk-bridge")
        self._spawn(["Xvfb", disp, "-screen", "0", f"{self.size[0]}x{self.size[1]}x24", "-nolisten", "tcp"])
        deadline = time.monotonic() + timeout
        while not Path(f"/tmp/.X11-unix/X{self.display}").exists():
            if time.monotonic() > deadline:
                self.stop()
                raise RuntimeError("Xvfb did not start")
            time.sleep(0.05)
        if shutil.which("dbus-launch"):
            out = subprocess.run(["dbus-launch", "--sh-syntax"], capture_output=True, text=True, env=self.env).stdout
            for line in out.splitlines():
                if line.startswith("DBUS_SESSION_BUS_ADDRESS="):
                    self.env["DBUS_SESSION_BUS_ADDRESS"] = line.split("=", 1)[1].rstrip(";").strip("'")
                if line.startswith("DBUS_SESSION_BUS_PID="):
                    self.env["GUA_DBUS_PID"] = line.split("=", 1)[1].rstrip(";")
            launcher = next((p for p in ("/usr/libexec/at-spi-bus-launcher", "/usr/lib/at-spi2-core/at-spi-bus-launcher")
                             if Path(p).exists()), None)
            if launcher:
                self._spawn([launcher, "--launch-immediately"])
        if shutil.which("openbox"):
            self._spawn(["openbox"])
        if self.liveview and shutil.which("x11vnc") and shutil.which("websockify") and Path("/usr/share/novnc").exists():
            vnc, ws = free_port(), free_port()
            self._spawn(["x11vnc", "-display", disp, "-rfbport", str(vnc), "-localhost", "-shared", "-forever",
                         "-nopw", "-quiet"])
            self._spawn(["websockify", "--web", "/usr/share/novnc", str(ws), f"127.0.0.1:{vnc}"])
            base = f"http://127.0.0.1:{ws}/vnc.html?autoconnect=1&resize=scale"
            self.liveview_url, self.takeover_url = base + "&view_only=1", base
        bindir = Path(self._tmp) / "bin"
        bindir.mkdir(exist_ok=True)
        gpy = python_with("gi") or "/usr/bin/python3"
        wrapper = bindir / "gua-form"
        wrapper.write_text(f"#!/bin/sh\nexec {gpy} {FORM_APP} \"$@\"\n")
        wrapper.chmod(0o755)
        apps = list(self.apps) + [str(wrapper), "gua-form"]
        py = python_with("pyatspi") or sys.executable
        argv = [py, str(DAEMON), "--port", str(self.port), "--display", disp, "--token", self.token,
                "--workdir", self.workdir, "--apps", *apps, "--liveview", self.liveview_url,
                "--takeover-url", self.takeover_url]
        if self.shell:
            argv.append("--shell")
        self.env["PATH"] = f"{bindir}:{self.env.get('PATH', '')}"
        self._spawn(argv)
        self.url = f"http://127.0.0.1:{self.port}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while True:
            try:
                with opener.open(self.url + "/health", timeout=1) as r:
                    if json.loads(r.read()).get("ok"):
                        break
            except Exception:  # noqa: BLE001
                pass
            if time.monotonic() > deadline:
                self.stop()
                raise RuntimeError("sandbox daemon did not become healthy")
            time.sleep(0.1)
        return self

    @property
    def form_app(self) -> list[str]:
        return [str(Path(self._tmp) / "bin" / "gua-form")]

    def remote_env(self, **kw):
        from ..env.remote import RemoteEnv
        return RemoteEnv(self.url, self.token, **kw)

    def stop(self) -> None:
        for p in reversed(self.procs):
            if p.poll() is None:
                try:
                    os.killpg(p.pid, 15)
                except OSError:
                    p.terminate()
        for p in reversed(self.procs):
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(p.pid, 9)
                except OSError:
                    pass
        self.procs.clear()
        pid = self.env.get("GUA_DBUS_PID")
        if pid and pid.isdigit():
            try:
                os.kill(int(pid), 15)
            except OSError:
                pass
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


class SandboxPool:
    """N 台并行的本机沙箱电脑（每台独立 DISPLAY / 端口 / 工作目录）。"""

    def __init__(self, n: int, **kw):
        self.boxes = [LocalSandbox(**kw) for _ in range(max(1, n))]

    def __enter__(self):
        started = []
        try:
            for b in self.boxes:
                b.display = free_display(50 + 10 * len(started))
                started.append(b.start())
        except Exception:
            for b in started:
                b.stop()
            raise
        return self.boxes

    def __exit__(self, *exc):
        for b in self.boxes:
            b.stop()

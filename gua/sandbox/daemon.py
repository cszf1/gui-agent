#!/usr/bin/env python3
"""gua 沙箱电脑守护进程（v0.6）：在一次性 Linux 桌面（Xvfb + 窗口管理器）里提供截图 / 输入 / 无障碍树 /
语义动作 / shell / 文件 / 人工接管 / 快照与重置，供 gua.env.remote.RemoteEnv 通过 HTTP 调用。

设计参考（只用公开资料）：Anthropic computer-use 参考实现（Docker + Xvfb + 窗口管理器 + VNC/noVNC）、
Cua Sandbox / Cua Driver（屏幕 + 无障碍树 + 动作层、后台语义动作）、ChatGPT agent / Grok Bot 的“人工接管”
（接管期间不截图、交还后 agent 重新观察）、Cua-Bench 的“任务即环境”（每个任务从同一快照开始）。

这个文件刻意只依赖标准库（截图用 Pillow，可选；无障碍用 pyatspi，可选），可以直接 COPY 进 Docker 镜像，
用系统 Python 运行，不需要安装 gua。

端点（除 /health 外都需要 `X-Gua-Token`；默认只监听 127.0.0.1）：
  GET  /health                       状态（显示器、AT-SPI、接管、版本、epoch）
  GET  /screenshot                   PNG
  GET  /observe?elements=1           前台窗口 / 窗口列表 / 指针 / AT-SPI 树（节点带 path）/ snapshot_id
  GET  /pointer                      指针位置与前台窗口 id（后台动作侵入检测）
  POST /input      {action...}       xdotool 真实输入（会移动指针）
  POST /semantic   {path, role, name, method, text, snapshot_id}   AT-SPI 动作（不移动指针）
  POST /shell      {argv, timeout}   仅当以 --shell 启动；argv 直接 exec，工作目录 = workdir
  POST /files      {method, path, text}   路径限定在 workdir 内
  POST /launch     {argv}            启动 --apps 白名单里的应用（任务 setup / open_app）
  POST /takeover   {}                人工接管：暂停 agent 的所有动作与截图
  POST /handback   {}                交还控制：epoch+1，之前的观察全部失效
  POST /snapshot   {name}            记录 workdir 内容 + 正在运行的已启动应用
  POST /reset      {name}            关闭已启动应用 → 还原 workdir → 重启快照里的应用
  GET  /liveview                     live view / 接管 URL（由启动器通过参数传入）
"""
from __future__ import annotations

import argparse
import base64
import hmac
import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

if __package__:
    from .process import run_bounded
else:
    from process import run_bounded

VERSION = "0.6.0"

XKEYS = {
    "ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift", "meta": "super", "cmd": "super",
    "win": "super", "super": "super", "enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape",
    "tab": "Tab", "backspace": "BackSpace", "delete": "Delete", "del": "Delete", "up": "Up", "down": "Down",
    "left": "Left", "right": "Right", "pageup": "Prior", "pagedown": "Next", "home": "Home", "end": "End",
    "space": "space", "insert": "Insert",
}
SCROLL_BUTTON = {"up": "4", "down": "5", "left": "6", "right": "7"}
ACTION_NAMES = {
    "invoke": ("click", "press", "activate", "jump"),
    "toggle": ("toggle", "click", "press", "activate"),
    "select": ("select", "click", "press", "activate"),
    "expand": ("expand or contract", "expand", "press", "click"),
    "collapse": ("expand or contract", "collapse", "press", "click"),
}


class State:
    def __init__(self, args):
        self.token = args.token
        self.display = args.display
        self.workdir = Path(args.workdir).resolve()
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.snapdir = Path(args.snapdir or (str(self.workdir) + ".snapshots")).resolve()
        self.snapdir.mkdir(parents=True, exist_ok=True)
        self.allow_shell = bool(args.shell)
        self.apps = set(args.apps or [])
        self.liveview = args.liveview or ""
        self.takeover_url = args.takeover_url or ""
        self.vnc_enabled = bool(self.liveview)
        self.takeover = False
        self.takeover_since = 0.0
        self.epoch = 0
        self.counter = 0
        self.nodes: dict[str, object] = {}
        self.identities: dict[object, str] = {}
        self.procs: list[tuple[list[str], subprocess.Popen]] = []
        self.lock = threading.RLock()
        self.env = dict(os.environ, DISPLAY=self.display)


S: State


# ---------------------------------------------------------------------------- 系统工具
def sh(args: list[str], timeout: float = 5.0) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=S.env).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def xdo(*args: str, timeout: float = 10.0) -> subprocess.CompletedProcess:
    return subprocess.run(["xdotool", *args], capture_output=True, text=True, timeout=timeout, env=S.env)


def active_window() -> tuple[str, str, str]:
    wid = sh(["xdotool", "getactivewindow"])
    if not wid:
        return "", "", ""
    title = sh(["xdotool", "getwindowname", wid])
    pid = sh(["xdotool", "getwindowpid", wid])
    proc = ""
    if pid.isdigit():
        try:
            proc = Path(f"/proc/{pid}/comm").read_text().strip()
        except OSError:
            pass
    return wid, title, proc


def window_titles() -> list[str]:
    if shutil.which("wmctrl"):
        out = sh(["wmctrl", "-l"])
        return [ln.split(None, 3)[-1] for ln in out.splitlines() if len(ln.split(None, 3)) == 4][:30]
    ids = sh(["xdotool", "search", "--onlyvisible", "--name", "."]).split()[:30]
    return [t for t in (sh(["xdotool", "getwindowname", i]) for i in ids) if t]


def pointer() -> tuple[int, int]:
    out = sh(["xdotool", "getmouselocation", "--shell"])
    vals = dict(ln.split("=", 1) for ln in out.splitlines() if "=" in ln)
    try:
        return int(vals.get("X", 0)), int(vals.get("Y", 0))
    except ValueError:
        return 0, 0


def screenshot_png() -> bytes:
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab(xdisplay=S.display)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        p = subprocess.run(["import", "-window", "root", "png:-"], capture_output=True, timeout=10, env=S.env)
        return p.stdout


# ---------------------------------------------------------------------------- AT-SPI
def _atspi():
    try:
        import pyatspi  # noqa: WPS433
        return pyatspi
    except Exception:  # noqa: BLE001
        return None


def _node(acc, pyatspi, path: str, depth: int, budget: list) -> dict:
    budget[0] -= 1
    d: dict = {"role": acc.getRoleName(), "name": acc.name or "", "path": path}
    # Bind the accessible object, not a tree index that can be reused after
    # replacement. Keep the handle only for the current observation.
    d["identity"] = S.identities.setdefault(acc, secrets.token_hex(12))
    S.nodes[path] = acc
    try:
        d["states"] = [pyatspi.stateToString(s) for s in acc.getState().getStates()]
    except Exception:  # noqa: BLE001
        d["states"] = []
    try:
        e = acc.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
        d["extents"] = [e.x, e.y, e.width, e.height]
    except Exception:  # noqa: BLE001
        pass
    if "password text" != d["role"]:
        try:
            t = acc.queryText()
            d["text"] = t.getText(0, min(t.characterCount, 200))
        except Exception:  # noqa: BLE001
            pass
    try:
        act = acc.queryAction()
        d["actions"] = [act.getName(i) for i in range(act.nActions)]
    except Exception:  # noqa: BLE001
        d["actions"] = []
    if depth < 30 and budget[0] > 0 and ("showing" in d["states"] or depth < 2):
        kids = []
        for i in range(min(acc.childCount, 300)):
            try:
                kids.append(_node(acc.getChildAtIndex(i), pyatspi, f"{path}/{i}", depth + 1, budget))
            except Exception:  # noqa: BLE001
                continue
        d["children"] = kids
    return d


def atspi_tree(title: str) -> dict | None:
    pyatspi = _atspi()
    if pyatspi is None:
        return None
    desktop = pyatspi.Registry.getDesktop(0)
    fallback = None
    for ai in range(desktop.childCount):
        try:
            app = desktop.getChildAtIndex(ai)
            for wi in range(app.childCount):
                win = app.getChildAtIndex(wi)
                st = win.getState()
                hit = st.contains(pyatspi.STATE_ACTIVE) or (title and title == (win.name or ""))
                if hit:
                    return _node(win, pyatspi, f"{ai}/{wi}", 0, [4000])
                if fallback is None and st.contains(pyatspi.STATE_SHOWING):
                    fallback = (win, f"{ai}/{wi}")
        except Exception:  # noqa: BLE001
            continue
    return _node(fallback[0], pyatspi, fallback[1], 0, [4000]) if fallback else None


def resolve(path: str):
    pyatspi = _atspi()
    if pyatspi is None:
        raise LookupError("AT-SPI unavailable")
    acc = S.nodes.get(path)
    if acc is None or acc.getState().contains(pyatspi.STATE_DEFUNCT):
        raise LookupError("observed node vanished")
    return acc, pyatspi


def semantic(body: dict) -> dict:
    """AT-SPI 语义动作：先按 path 找回节点，再核对 role / name 未变（身份检查），然后调用接口。"""
    method = body.get("method")
    try:
        acc, pyatspi = resolve(str(body.get("path", "")))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"stale_target: node not found ({type(e).__name__})"}
    role, name = acc.getRoleName(), acc.name or ""
    if role != body.get("native_role") or (role != "password text" and name != body.get("name_raw", name)):
        return {"ok": False, "error": "stale_target: node identity changed since observation"}
    st = acc.getState()
    if not (st.contains(pyatspi.STATE_ENABLED) or st.contains(pyatspi.STATE_SENSITIVE)):
        return {"ok": False, "error": "stale_target: control disabled"}
    route = f"atspi:{method}"
    try:
        if method in ACTION_NAMES:
            try:
                act = acc.queryAction()
            except Exception:  # noqa: BLE001
                return {"ok": False, "error": "background_unavailable: no AT-SPI action interface", "route": route}
            names = [act.getName(i).lower() for i in range(act.nActions)]
            idx = next((names.index(n) for n in ACTION_NAMES[method] if n in names), None)
            if idx is None:
                return {"ok": False, "error": f"background_unavailable: no suitable action in {names}", "route": route}
            if not act.doAction(idx):
                return {"ok": False, "error": "native_action_error: action not acknowledged; do not replay",
                        "route": route}
            return {"ok": True, "route": route}
        if method == "set_value":
            states = {pyatspi.stateToString(x) for x in st.getStates()}
            if role == "password text" or "password" in states:
                return {"ok": False, "error": "blocked_by_safety: semantic set_value never writes password fields",
                        "route": route}
            try:
                et = acc.queryEditableText()
            except Exception:  # noqa: BLE001
                return {"ok": False, "error": "background_unavailable: no editable text interface", "route": route}
            text = str(body.get("text") or "")
            if acc.getRoleName() == "password text" or acc.getState().contains(pyatspi.STATE_DEFUNCT):
                return {"ok": False, "error": "stale_target: security or identity changed", "route": route}
            if not et.setTextContents(text):
                return {"ok": False, "error": "native_action_error: value not acknowledged", "route": route}
            try:
                if acc.getRoleName() == "password text":
                    return {"ok": False, "error": "native_action_error: security changed; no value read", "route": route}
                t = acc.queryText()
                now = t.getText(0, -1)
            except Exception:  # noqa: BLE001
                return {"ok": False, "error": "native_action_error: cannot verify actual value", "route": route}
            if now != text:
                return {"ok": False, "error": "native_action_error: value not verified; observe again",
                        "route": route}
            return {"ok": True, "route": route}
        if method == "focus":
            ok = acc.queryComponent().grabFocus()
            return {"ok": bool(ok), "route": route, "error": "" if ok else "native_action_error: focus refused"}
        if method == "scroll_into_view":
            comp = acc.queryComponent()
            if hasattr(comp, "scrollTo"):
                comp.scrollTo(getattr(pyatspi, "SCROLL_ANYWHERE", 6))
                return {"ok": True, "route": route}
            return {"ok": False, "error": "background_unavailable: scrollTo unsupported", "route": route}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"native_action_error: {type(e).__name__}; effect uncertain, do not replay",
                "route": route}
    return {"ok": False, "error": f"unsupported: {method}", "route": route}


# ---------------------------------------------------------------------------- 真实输入
def key_combo(keys: list[str]) -> str:
    out = []
    for k in keys:
        kl = k.lower()
        if kl in XKEYS:
            out.append(XKEYS[kl])
        elif len(kl) >= 2 and kl[0] == "f" and kl[1:].isdigit():
            out.append(kl.upper())
        else:
            out.append(k)
    return "+".join(out)


def do_input(a: dict) -> dict:
    t = a.get("type")
    x, y = a.get("x"), a.get("y")
    xy = [str(int(x)), str(int(y))] if x is not None and y is not None else None
    if a.get("target_path"):
        try:
            target, pyatspi = resolve(str(a["target_path"]))
            state = target.getState()
            extents = target.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
            if (target.getRoleName() != a.get("target_role")
                    or active_window()[0] != a.get("target_window")
                    or (target.getRoleName() != "password text" and (target.name or "") != a.get("target_name"))
                    or (target.getRoleName() == "password text") != bool(a.get("target_password"))
                    or not state.contains(pyatspi.STATE_ENABLED)
                    or a.get("target_checked") is not None
                    and state.contains(pyatspi.STATE_CHECKED) != a["target_checked"]
                    or x is None or y is None
                    or not (extents.x <= x < extents.x + extents.width and extents.y <= y < extents.y + extents.height)):
                return {"ok": False, "error": "stale_target: pointer target changed before input"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"stale_target: pointer target unavailable ({type(exc).__name__})"}
    r = None
    if t in {"click", "double_click", "right_click", "move", "long_press"}:
        if xy is None:
            return {"ok": False, "error": "missing coordinate"}
        if t == "move":
            r = xdo("mousemove", *xy)
        elif t == "long_press":
            r = xdo("mousemove", *xy, "mousedown", "1", "sleep", str(max(0.5, a.get("seconds") or 0.8)), "mouseup", "1")
        else:
            btn = "3" if t == "right_click" else "1"
            rep = ["--repeat", "2", "--delay", "80"] if t == "double_click" else []
            r = xdo("mousemove", *xy, "click", *rep, btn)
    elif t == "drag":
        if xy is None or a.get("x2") is None:
            return {"ok": False, "error": "missing coordinate"}
        r = xdo("mousemove", *xy, "mousedown", "1", "mousemove", str(int(a["x2"])), str(int(a["y2"])), "mouseup", "1")
    elif t == "scroll":
        if xy:
            xdo("mousemove", *xy)
        btn = SCROLL_BUTTON.get(a.get("direction") or "down", "5")
        r = xdo("click", "--repeat", str(max(1, int(a.get("amount") or 3))), "--delay", "30", btn)
    elif t == "type":
        text = str(a.get("text") or "")
        try:
            acc, pyatspi = resolve(str(a.get("focus_path") or ""))
            def checked_input(*args):
                state = acc.getState()
                if (not state.contains(pyatspi.STATE_FOCUSED) or state.contains(pyatspi.STATE_DEFUNCT)
                        or active_window()[0] != a.get("focus_window")
                        or acc.getRoleName() != a.get("focus_role")
                        or (acc.getRoleName() == "password text") != bool(a.get("focus_password"))):
                    raise ValueError("keyboard focus, window or security changed; stop typing")
                outcome = xdo(*args)
                if outcome.returncode:
                    raise ValueError("keyboard input failed; do not replay")
                return outcome
            if a.get("clear"):
                checked_input("key", "ctrl+a")
                checked_input("key", "BackSpace")
            for char in text:
                r = checked_input("type", "--delay", "0", "--", char)
            if a.get("submit"):
                r = checked_input("key", "Return")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"stale_target: guarded input stopped ({type(exc).__name__})", "route": "xdotool"}
        if r is None:
            return {"ok": True, "route": "xdotool"}
    elif t in {"hotkey", "key_down", "key_up"}:
        combo = key_combo(a.get("keys") or [])
        r = xdo({"hotkey": "key", "key_down": "keydown", "key_up": "keyup"}[t], combo)
    elif t == "wait":
        time.sleep(min(float(a.get("seconds") or 1.0), 10.0))
        return {"ok": True, "route": "wait"}
    elif t == "focus_window":
        ids = sh(["xdotool", "search", "--name", str(a.get("text") or "")]).split()
        if not ids:
            return {"ok": False, "error": f"window_not_found {a.get('text')!r}"}
        xdo("windowmap", ids[-1])
        r = xdo("windowactivate", "--sync", ids[-1])
    elif t == "open_app":
        return launch([str(a.get("app") or "")])
    else:
        return {"ok": False, "error": f"unsupported action {t} on remote sandbox"}
    if r is not None and r.returncode != 0:
        return {"ok": False, "error": f"xdotool failed: {r.stderr.strip()[:160]}", "route": "xdotool"}
    return {"ok": True, "route": "xdotool"}


# ---------------------------------------------------------------------------- 应用 / shell / 文件 / 快照
def launch(argv: list[str]) -> dict:
    if not argv or not argv[0]:
        return {"ok": False, "error": "invalid_argument: empty argv"}
    if os.path.basename(argv[0]) not in S.apps and argv[0] not in S.apps:
        return {"ok": False, "error": f"blocked_by_safety: app {argv[0]!r} not in sandbox --apps allowlist"}
    env = dict(S.env, GTK_MODULES="gail:atk-bridge", NO_AT_BRIDGE="0", GUA_SANDBOX_WORKDIR=str(S.workdir))
    p = subprocess.Popen(argv, cwd=str(S.workdir), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    with S.lock:
        S.procs.append((list(argv), p))
    return {"ok": True, "route": "launch", "pid": p.pid}


def kill_launched() -> None:
    with S.lock:
        procs, S.procs = S.procs, []
    for _, p in procs:
        if p.poll() is None:
            try:
                os.killpg(p.pid, 15)
            except OSError:
                p.terminate()
    for _, p in procs:
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, 9)
            except OSError:
                p.kill()


def safe_path(rel: str) -> Path:
    p = Path(rel or ".")
    if p.is_absolute() or any(part == ".." for part in p.parts) or "\x00" in str(rel):
        raise PermissionError("path must be relative to the sandbox workdir")
    full = (S.workdir / p).resolve()
    if full != S.workdir and S.workdir not in full.parents:
        raise PermissionError("path escapes the sandbox workdir")
    return full


def files(body: dict) -> dict:
    m = body.get("method")
    try:
        full = safe_path(str(body.get("path") or "."))
    except PermissionError as e:
        return {"ok": False, "error": f"blocked_by_safety: {e}", "route": "file"}
    if m == "list":
        if not full.is_dir():
            return {"ok": False, "error": "tool_error: not a directory", "route": "file"}
        return {"ok": True, "output": "\n".join(sorted(p.name + ("/" if p.is_dir() else "") for p in full.iterdir())),
                "route": "file"}
    if m == "read":
        if not full.is_file():
            return {"ok": False, "error": "tool_error: file not found", "route": "file"}
        with full.open("rb") as stream:
            data = stream.read(200_000)
        return {"ok": True, "output": data.decode("utf-8", "replace"), "route": "file"}
    if m in {"write", "append"}:
        full.parent.mkdir(parents=True, exist_ok=True)
        with open(full, "a" if m == "append" else "w", encoding="utf-8") as f:
            f.write(str(body.get("text") or ""))
        return {"ok": True, "output": f"{m} ok", "route": "file"}
    return {"ok": False, "error": f"unsupported file method {m}", "route": "file"}


def shell(body: dict) -> dict:
    if not S.allow_shell:
        return {"ok": False, "error": "blocked_by_safety: sandbox started without --shell", "route": "shell"}
    argv = body.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
        return {"ok": False, "error": "invalid_argument: argv must be a list of strings", "route": "shell"}
    timeout = min(float(body.get("timeout") or 20), 120)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(S.workdir), "LANG": "C.UTF-8",
           "DISPLAY": S.display}
    try:
        p = run_bounded(argv, cwd=str(S.workdir), env=env, timeout=timeout, max_output=8000, limits=True)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"tool_error: timeout after {timeout}s", "route": "shell"}
    except FileNotFoundError:
        return {"ok": False, "error": f"tool_error: executable not found: {argv[0]}", "route": "shell"}
    out = (p.stdout + (("\n[stderr]\n" + p.stderr) if p.stderr else ""))[:8000]
    if p.returncode:
        return {"ok": False, "error": f"tool_error: exit code {p.returncode}", "output": out, "route": "shell"}
    return {"ok": True, "output": out, "route": "shell"}


def vnc_control(enabled: bool) -> bool:
    """Enforce view-only at the VNC server; a URL flag is only UI state."""
    if not getattr(S, "vnc_enabled", False):
        return True
    try:
        # -remote alone only posts a request. Query on the same invocation
        # waits for server processing, then verifies the effective mode.
        result = subprocess.run(["x11vnc", "-display", S.display, "-remote",
                                 "noviewonly" if enabled else "viewonly", "-query", "viewonly"],
                                env=S.env, capture_output=True, text=True, timeout=5)
        expected = "ans=viewonly:" + ("0" if enabled else "1")
        return result.returncode == 0 and expected in result.stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return False


def snapshot(name: str) -> dict:
    name = "".join(c for c in (name or "default") if c.isalnum() or c in "-_") or "default"
    tar = S.snapdir / f"{name}.tar"
    with tarfile.open(tar, "w") as tf:
        tf.add(str(S.workdir), arcname=".")
    with S.lock:
        apps = [argv for argv, p in S.procs if p.poll() is None]
    (S.snapdir / f"{name}.json").write_text(json.dumps({"apps": apps, "time": time.time()}))
    return {"ok": True, "name": name, "apps": apps}


def reset(name: str) -> dict:
    name = "".join(c for c in (name or "default") if c.isalnum() or c in "-_") or "default"
    tar, meta = S.snapdir / f"{name}.tar", S.snapdir / f"{name}.json"
    if not tar.exists():
        return {"ok": False, "error": f"no snapshot named {name!r}"}
    kill_launched()
    for child in S.workdir.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    with tarfile.open(tar) as tf:
        members = [m for m in tf.getmembers() if not (m.name.startswith("/") or ".." in Path(m.name).parts)
                   and (m.isfile() or m.isdir())]
        try:
            tf.extractall(str(S.workdir), members=members, filter="data")
        except TypeError:          # Python < 3.12
            tf.extractall(str(S.workdir), members=members)
    apps = json.loads(meta.read_text()).get("apps", []) if meta.exists() else []
    for argv in apps:
        launch(argv)
    with S.lock:
        S.epoch += 1          # 重置也让之前的观察全部失效
    return {"ok": True, "name": name, "relaunched": apps}


# ---------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "gua-sandbox/" + VERSION

    def log_message(self, fmt, *args):  # noqa: D401  — 安静（不记录请求内容，可能含输入文本）
        return

    def _send(self, code: int, obj=None, body: bytes | None = None, ctype="application/json"):
        data = body if body is not None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _auth(self) -> bool:
        tok = self.headers.get("X-Gua-Token", "")
        if S.token and hmac.compare_digest(tok, S.token):
            return True
        self._send(401, {"ok": False, "error": "unauthorized"})
        return False

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > 2_000_000:
            raise ValueError("request too large")
        raw = self.rfile.read(n) if n else b"{}"
        obj = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(obj, dict):
            raise ValueError("body must be a JSON object")
        return obj

    def _paused(self) -> bool:
        if S.takeover:
            self._send(423, {"ok": False, "error": "paused_for_human: a human has control of the sandbox; "
                                                   "wait for hand-back, then observe again",
                             "takeover_since": S.takeover_since})
            return True
        return False

    def _stale(self, body: dict) -> bool:
        snap = str(body.get("snapshot_id") or "")
        if snap and snap != f"{S.epoch}:{S.counter}":
            self._send(200, {"ok": False, "error": "stale_target: sandbox was taken over or reset since the "
                                                   "observation; observe again"})
            return True
        return False

    def do_GET(self):  # noqa: N802
        # Takeover is acknowledged only after an in-flight observation/action
        # has completed. No screenshot can escape after that acknowledgement.
        with S.lock:
            return self._get_locked()

    def _get_locked(self):
        u = urlparse(self.path)
        if u.path == "/health":
            return self._send(200, {"ok": True, "version": VERSION, "display": S.display, "epoch": S.epoch,
                                    "atspi": _atspi() is not None, "takeover": S.takeover,
                                    "shell": S.allow_shell, "apps": sorted(S.apps)})
        if not self._auth():
            return None
        if u.path == "/liveview":
            return self._send(200, {"ok": True, "liveview": S.liveview, "takeover": S.takeover_url})
        if u.path == "/pointer":
            wid, _, _ = active_window()
            return self._send(200, {"ok": True, "pointer": list(pointer()), "foreground": wid})
        if self._paused():          # 接管期间不截图、不读无障碍树（参考 ChatGPT agent 的接管隐私做法）
            return None
        if u.path == "/screenshot":
            return self._send(200, body=screenshot_png(), ctype="image/png")
        if u.path == "/observe":
            q = parse_qs(u.query)
            wid, title, proc = active_window()
            tree = None
            if q.get("elements", ["1"])[0] != "0":
                S.nodes = {}
                try:
                    tree = atspi_tree(title)
                    S.identities = {acc: S.identities[acc] for acc in S.nodes.values()}
                except Exception as e:  # noqa: BLE001
                    tree = {"error": f"{type(e).__name__}"}
            with S.lock:
                S.counter += 1
                snap = f"{S.epoch}:{S.counter}"
            payload = {"ok": True, "active_window": title, "active_process": proc, "foreground": wid,
                                    "windows": window_titles(), "pointer": list(pointer()), "tree": tree,
                                    "snapshot_id": snap, "epoch": S.epoch}
            if q.get("image", ["0"])[0] == "1":
                payload["image"] = base64.b64encode(screenshot_png()).decode("ascii")
            return self._send(200, payload)
        return self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):  # noqa: N802
        if not self._auth():
            return None
        try:
            self.connection.settimeout(5)
            body = self._body()
        except Exception as e:  # noqa: BLE001
            return self._send(400, {"ok": False, "error": f"bad request: {e}"})
        with S.lock:
            return self._post_locked(body)

    def _post_locked(self, body):
        u = urlparse(self.path)
        if u.path == "/takeover":
            with S.lock:
                S.takeover, S.takeover_since = True, time.time()
                if not vnc_control(True):
                    return self._send(503, {"ok": False, "error": "paused_for_human: unable to enable VNC control"})
            return self._send(200, {"ok": True, "takeover": S.takeover_url, "liveview": S.liveview})
        if u.path == "/handback":
            with S.lock:
                if not vnc_control(False):
                    S.takeover = True
                    return self._send(503, {"ok": False, "error": "paused_for_human: unable to revoke VNC control"})
                was = S.takeover
                S.takeover = False
                S.epoch += 1
                S.nodes = {}
                S.identities = {}
            return self._send(200, {"ok": True, "was_taken_over": was, "epoch": S.epoch})
        if self._paused():
            return None
        if u.path in {"/snapshot", "/reset"}:
            fn = snapshot if u.path == "/snapshot" else reset
            return self._send(200, fn(str(body.get("name") or "default")))
        if u.path == "/launch":
            return self._send(200, launch(list(body.get("argv") or [])))
        if u.path == "/input":
            if self._stale(body):
                return None
            return self._send(200, do_input(body))
        if u.path == "/semantic":
            if self._stale(body):
                return None
            return self._send(200, semantic(body))
        if u.path == "/shell":
            if self._stale(body):
                return None
            return self._send(200, shell(body))
        if u.path == "/files":
            if self._stale(body):
                return None
            return self._send(200, files(body))
        return self._send(404, {"ok": False, "error": "not found"})


def main(argv=None) -> None:
    global S
    ap = argparse.ArgumentParser(description="gua sandbox daemon")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--display", default=os.environ.get("DISPLAY", ":1"))
    ap.add_argument("--token", default=os.environ.get("GUA_SANDBOX_TOKEN") or "")
    ap.add_argument("--token-file", default="")
    ap.add_argument("--workdir", default=os.path.expanduser("~/gua-workdir"))
    ap.add_argument("--snapdir", default="")
    ap.add_argument("--shell", action="store_true", help="allow /shell inside this disposable sandbox")
    ap.add_argument("--apps", nargs="*", default=[], help="executables /launch and open_app may start")
    ap.add_argument("--liveview", default="")
    ap.add_argument("--takeover-url", default="")
    args = ap.parse_args(argv)
    if not args.token:
        args.token = secrets.token_urlsafe(24)
    if args.token_file:
        Path(args.token_file).write_text(args.token)
        os.chmod(args.token_file, 0o600)
    S = State(args)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(json.dumps({"listening": f"http://{args.host}:{srv.server_address[1]}", "display": args.display}),
          flush=True)
    try:
        srv.serve_forever()
    finally:
        kill_launched()


if __name__ == "__main__":
    main(sys.argv[1:])

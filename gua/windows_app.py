"""Windows desktop app: system Edge UI, private loopback API, embedded Python."""
from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import secrets
import sys
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from .app_native import ChildJob, EdgeWindow, InstanceLock, StopShortcut
from .app_storage import ACTIVE, AppStorage, dpapi, remove_owned, validate_settings
from .app_worker import AppWorker


# These are application-owned web assets. Windows MIME mappings can be changed
# by other software (notably .js -> text/plain), which breaks module loading in
# Edge with nosniff enabled. Never consult system mappings for this bundle.
UI_CONTENT_TYPES = {
    ".html": "text/html", ".htm": "text/html",
    ".js": "text/javascript", ".mjs": "text/javascript",
    ".css": "text/css", ".json": "application/json", ".map": "application/json",
    ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf",
    ".wasm": "application/wasm", ".txt": "text/plain",
}


def program_root():
    candidate = Path(sys.executable).parent.parent
    return candidate if (candidate / ".gui-agent-program").is_file() else Path(__file__).resolve().parent.parent


def default_data(root):
    portable = (root / "portable.flag").is_file()
    if portable: return root / "data", True
    if sys.platform == "win32": return Path(os.environ["APPDATA"]) / "GUI Agent", False
    return Path.home() / ".local/share/gui-agent", False


class AppController:
    def __init__(self, storage, *, headless=False, ui=None):
        self.storage, self.headless, self.ui = storage, headless, ui
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.worker, self.active, self.pending = None, None, None
        self.worker_session, self.testing, self.cleaning, self.stopping = "", False, False, False
        self.events, self.sequence = deque(maxlen=128), 0
        self.ui_polls = 0
        self.shutdown = lambda: None
        self.shortcut = StopShortcut(self.stop)
        storage.prune_runs(); storage.clean_transient()
        if not storage.sessions: storage.new_session()

    def publish(self, event):
        with self.condition:
            self.sequence += 1; self.events.append((self.sequence, event)); self.condition.notify_all()

    def state(self):
        with self.lock:
            for s in self.storage.sessions:
                for r in s["runs"]: r["reportReady"] = (self.storage.runs / r["id"] / "report.html").is_file()
            return dict(settings=self.storage.public_settings(), sessions=self.storage.sessions,
                runtime=dict(platform="win32" if sys.platform == "win32" else sys.platform,
                             shortcutAvailable=self.shortcut.available, bundledWorker=True,
                             engine="embedded Python + system Edge", uiConnected=self.ui_polls > 0))

    def idle(self):
        if self.active or self.testing or self.cleaning: raise ValueError("请先结束当前任务")

    def accept(self, value):
        with self.lock:
            if not self.active or value.get("runId") != self.active["id"]: return
            kind = value.get("type")
            if kind not in {"state", "log", "preview", "privacy", "request", "result", "error"}: return
            event = dict(value)
            if kind == "result":
                expected = self.storage.runs / self.active["id"] / "report.html"
                ready = event.pop("report", None) == str(expected) and expected.is_file()
                event["reportReady"] = ready; self.active["reportReady"] = ready
                self.active["result"] = event.get("result", {})
                status = self.active["result"].get("status")
                self.active["status"] = "done" if status == "done" else "stopped" if status == "user_abort" else "uncertain" if status in {"uncertain", "privacy_blocked"} else "fail"
            elif kind == "state": self.active["status"] = event.get("state", "running")
            elif kind == "request": self.active["status"] = "waiting"; self.pending = event
            elif kind == "error": self.active["status"] = "error"
            if kind != "preview":
                self.active["events"] = [*self.active["events"], event][-300:]
                self.storage.persist()
            self.publish(event)
            if kind in {"request", "error", "result"} or (kind == "state" and event.get("state") == "paused"):
                if self.ui: self.ui.show()
            if kind in {"error", "result"}:
                self.active, self.pending = None, None
                self.storage.prune_runs()

    def worker_exit(self, owner):
        with self.lock:
            if self.worker is not owner: return
            self.worker = None
            if self.active:
                if self.stopping:
                    self.accept(dict(type="result", runId=self.active["id"], result=dict(status="user_abort", claimed_done=False)))
                else: self.accept(dict(type="error", runId=self.active["id"], message="执行进程意外退出，请检查当前结果"))

    def stop(self):
        with self.lock:
            self.stopping = True
            old = self.worker
        if old: old.terminate()
        with self.lock:
            if self.active:
                self.accept(dict(type="result", runId=self.active["id"], result=dict(status="user_abort", claimed_done=False)))
            self.worker, self.pending = None, None
        if self.ui: self.ui.show()

    def start(self, body):
        with self.lock:
            self.idle()
            task = body.get("task")
            if not isinstance(task, str) or not task.strip() or len(task) > 20000: raise ValueError("请输入有效任务")
            session = next((s for s in self.storage.sessions if s["id"] == body.get("sessionId")), None)
            if session is None: raise ValueError("对话不存在")
            demo = body.get("demo") is True
            if not demo and not self.storage.settings["model"]: raise ValueError("请先填写模型名称")
            run = dict(id=str(uuid.uuid4()), sessionId=session["id"], task=task.strip(), createdAt=int(time.time()*1000),
                       demo=demo, status="starting", events=[])
            context = "\n\n".join(r["task"] + "\n结果: " + r.get("result", {}).get("status", r["status"]) for r in session["runs"][-6:])
            if not session["runs"]: session["title"] = task[:24]
            session["runs"].append(run)
            self.active, self.stopping = run, False
            self.storage.persist(); self.storage.prune_runs(run["id"])
            self.publish(dict(type="run-created", run=run))
            if not demo and self.storage.settings["target"] == "desktop" and self.ui: self.ui.show(minimize=True)
        def launch():
            try:
                if self.worker and self.worker_session != session["id"]:
                    old, self.worker = self.worker, None; old.terminate()
                with self.lock:
                    if self.active is not run: return
                    if self.worker is None: self.worker = AppWorker(self.storage, self.accept, self.worker_exit)
                    owner = self.worker; self.worker_session = session["id"]
                owner.boot()
                with self.lock:
                    if self.active is not run: return
                    owner.send(dict(command="run", runId=run["id"], task=run["task"], context=context, demo=demo,
                                    headless=self.headless, runsRoot=str(self.storage.runs),
                                    settings={**self.storage.settings, "apiKey": self.storage.key}))
            except Exception:
                self.accept(dict(type="error", runId=run["id"], message="无法启动执行引擎或系统 Edge，请检查安装环境"))
        threading.Thread(target=launch, daemon=True).start()
        return run

    def call(self, name, body):
        if name == "load": return self.state()
        if name == "storage": return self.storage.info()
        if name == "start": return self.start(body)
        if name == "poll":
            cursor = body.get("cursor", 0)
            if type(cursor) is not int or cursor < 0: raise ValueError("无效的事件游标")
            with self.condition:
                self.ui_polls += 1
                if cursor >= self.sequence: self.condition.wait(15)
                if self.events and cursor < self.events[0][0] - 1:
                    events = [dict(type="snapshot", state=self.state())]
                else: events = [e for i, e in self.events if i > cursor]
                return dict(cursor=self.sequence, events=events)
        if name == "show":
            if self.ui: self.ui.show()
            return dict(ok=True)
        if name == "shutdown":
            threading.Thread(target=self.shutdown, daemon=True).start(); return dict(ok=True)
        if name == "control":
            with self.lock:
                if not self.active or body.get("runId") != self.active["id"]: raise ValueError("任务已结束")
                action = body.get("action")
                if action not in {"stop", "pause", "resume"}: raise ValueError("无效的控制操作")
                if self.pending and action != "stop": raise ValueError("请先处理确认请求")
                if action != "stop":
                    if action == "resume" and self.ui: self.ui.show(minimize=self.storage.settings["target"] == "desktop")
                    self.worker.send(dict(command=action, runId=self.active["id"])); return None
            self.stop(); return None
        if name == "respond":
            with self.lock:
                if not self.active or body.get("runId") != self.active["id"] or not self.pending or body.get("requestId") != self.pending["requestId"]:
                    raise ValueError("确认请求已失效")
                if self.pending["kind"] == "confirm" and type(body.get("approved")) is not bool: raise ValueError("请选择允许或拒绝")
                if self.pending["kind"] == "ask" and (not isinstance(body.get("answer"), str) or len(body["answer"]) > 10000): raise ValueError("请输入有效回答")
                self.worker.send(dict(command="respond", **body)); self.pending = None
            return None
        if name == "test":
            with self.lock:
                self.idle(); value = validate_settings(body); key = self.storage.credentials(value); self.testing = True
            result, done = {}, threading.Event()
            probe = str(uuid.uuid4())
            def receive(e):
                if e.get("runId") == probe and e.get("type") in {"tested", "error"}:
                    result.update(ok=e["type"] == "tested", message=e.get("message", "连接测试失败")); done.set()
            test_worker = None
            try:
                test_worker = AppWorker(self.storage, receive, lambda _: done.set()); test_worker.boot()
                test_worker.send(dict(command="test", runId=probe, settings={**value, "apiKey": key}))
                done.wait(45)
                return result or dict(ok=False, message="连接测试超时或执行进程退出")
            finally:
                if test_worker: test_worker.terminate()
                with self.lock: self.testing = False
        if name in {"settings", "new-session", "clear-history"}:
            with self.lock: self.idle(); self.cleaning = True; old, self.worker = self.worker, None
            try:
                if old: old.terminate()
                with self.lock:
                    if name == "settings": return self.storage.save_settings(body)
                    if name == "new-session": return self.storage.new_session()
                    self.storage.clear_history(); self.storage.clean_transient(cache=False)
                    return self.state()
            finally:
                with self.lock: self.cleaning = False
        if name == "open-data":
            if sys.platform == "win32": os.startfile(self.storage.root)
            return None
        if name == "report":
            identifier = str(uuid.UUID(body.get("runId", "")))
            path = self.storage.runs / identifier / "report.html"
            if not path.is_file(): raise ValueError("报告尚未生成或已自动清理")
            if self.ui: self.ui.launch(path.as_uri())
            return None
        raise ValueError("无效的操作")

    def close(self):
        self.stop(); self.shortcut.close()
        if self.ui: self.ui.close()
        self.storage.clean_transient()


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, controller, ui_root, token=None):
        self.controller, self.ui_root = controller, Path(ui_root).resolve()
        self.token = token or secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", 0), AppHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        controller.shutdown = self.shutdown


class AppHandler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass  # Never log credential-bearing headers/URLs.

    def reply(self, status, value, mime="application/json"):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", mime + ("; charset=utf-8" if mime in {"text/html", "application/json", "text/javascript", "text/css"} else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'")
        self.end_headers()
        try: self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError): pass

    def host_ok(self):
        return self.headers.get("Host") == self.server.origin.split("//", 1)[1]

    def do_GET(self):
        if not self.host_ok(): self.reply(403, dict(error="invalid host")); return
        path = unquote(urlsplit(self.path).path)
        if path.startswith("/api/"): self.reply(405, dict(error="POST required")); return
        candidate = (self.server.ui_root / ("index.html" if path == "/" else path.lstrip("/"))).resolve()
        if not candidate.is_relative_to(self.server.ui_root) or not candidate.is_file():
            self.reply(404, dict(error="not found")); return
        mime = UI_CONTENT_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
        self.reply(200, candidate.read_bytes(), mime)

    def do_POST(self):
        origin = self.headers.get("Origin")
        site = self.headers.get("Sec-Fetch-Site")
        token = self.headers.get("X-Gua-Control", "")
        if (not self.host_ok() or (origin and origin != self.server.origin)
                or site not in {None, "same-origin", "none"}
                or not hmac.compare_digest(token.encode(), self.server.token.encode())):
            self.reply(403, dict(error="请求未通过本机应用认证")); return
        try:
            self.connection.settimeout(5)
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 2 * 1024 * 1024: raise ValueError("请求大小不正确")
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict): raise ValueError("无效的请求")
            path = urlsplit(self.path).path
            if not path.startswith("/api/"): raise ValueError("无效的操作")
            value = self.server.controller.call(path[5:], body)
            self.reply(200, dict(value=value))
        except (ValueError, TypeError, KeyError): self.reply(400, dict(error="请求内容不正确或当前操作不可用"))
        except Exception: self.reply(500, dict(error="本机操作失败，请检查运行环境"))


def read_connection(data_root):
    value = json.loads((data_root / "cache/instance.json").read_text())
    token = dpapi(base64.b64decode(value["sealedToken"]), decrypt=True).decode() if "sealedToken" in value else value["token"]
    return value["origin"], token


def existing_command(root, operation):
    origin, token = read_connection(root)
    if not origin.startswith("http://127.0.0.1:"): raise ValueError("无效的本机地址")
    opener = build_opener(ProxyHandler({}))
    req = Request(origin + "/api/" + operation, data=b"{}", headers={"X-Gua-Control": token, "Content-Type": "application/json"})
    with opener.open(req, timeout=5): pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve-test", action="store_true")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--ui-root", type=Path)
    args = parser.parse_args()
    root = program_root()
    data_root, portable = default_data(root)
    data_root = args.data_dir or data_root
    storage = AppStorage(data_root, portable=portable)
    instance = InstanceLock(storage.root)
    if not instance.acquired:
        for _ in range(30):
            try: existing_command(storage.root, "show"); break
            except (OSError, ValueError, KeyError): time.sleep(0.1)
        instance.close(); return
    controller = server = None
    storage.job = ChildJob()
    try:
        ui_root = args.ui_root or (root / "ui" if (root / "ui").is_dir() else root / "desktop/dist-ui")
        if not (ui_root / "index.html").is_file(): raise RuntimeError("安装包缺少界面资源")
        storage.clean_transient()
        ui = None if args.serve_test else EdgeWindow(storage)
        controller = AppController(storage, headless=args.headless, ui=ui)
        server = AppServer(controller, ui_root)
        info = dict(origin=server.origin, pid=os.getpid())
        if sys.platform == "win32":
            import psutil
            info["created"] = psutil.Process().create_time()
        try: info["sealedToken"] = base64.b64encode(dpapi(server.token.encode())).decode()
        except RuntimeError:
            if not args.serve_test: raise
            info["token"] = server.token
        connection = storage.cache / "instance.json"
        connection.write_text(json.dumps(info), encoding="utf-8"); os.chmod(connection, 0o600)
        if args.serve_test:
            print(json.dumps(dict(origin=server.origin, token=server.token)), flush=True)
        else:
            ui.launch(server.origin + "/#token=" + server.token)
            def watch_window():
                started, missing = False, None
                deadline = time.monotonic() + 90
                while True:
                    if ui.hwnd(): started, missing = True, None
                    elif started:
                        missing = missing or time.monotonic()
                        if time.monotonic() - missing > 3: server.shutdown(); return
                    elif time.monotonic() > deadline: server.shutdown(); return
                    time.sleep(1)
            threading.Thread(target=watch_window, daemon=True).start()
        server.serve_forever(poll_interval=0.2)
    finally:
        if server: server.server_close()
        if controller: controller.close()
        storage.job.close()
        storage.clean_transient()
        instance.close()


if __name__ == "__main__":
    try: main()
    except Exception:
        if sys.platform == "win32": ctypes = __import__("ctypes"); ctypes.windll.user32.MessageBoxW(None, "GUI Agent 无法启动。请检查系统 Edge 和安装包是否完整。", "GUI Agent", 0x10)
        else: raise

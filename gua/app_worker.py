"""Private child-process protocol; the HTTP server never executes GUI actions."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from .app_storage import remove_owned


class AppWorker:
    def __init__(self, data, on_event, on_exit):
        self.on_event, self.on_exit = on_event, on_exit
        self.ready = threading.Event()
        self.send_lock = threading.Lock()
        self.temp = Path(tempfile.mkdtemp(prefix="worker-", dir=data.temp))
        executable = Path(sys.executable)
        if executable.name.lower() == "pythonw.exe": executable = executable.with_name("python.exe")
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
               "PYTHONDONTWRITEBYTECODE": "1", "GUA_APP_TEMP": str(self.temp),
               "TMP": str(self.temp), "TEMP": str(self.temp), "TMPDIR": str(self.temp)}
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.process = subprocess.Popen([str(executable), "-B", "-u", "-m", "gua.desktop"],
            cwd=data.root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", creationflags=flags, start_new_session=sys.platform != "win32")
        job = getattr(data, "job", None)
        if job: job.attach(self.process)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for line in self.process.stdout:
                if len(line) > 8_000_000: continue
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict): continue
                    if event.get("type") == "ready": self.ready.set()
                    else: self.on_event(event)
                except (ValueError, TypeError): continue
        finally:
            self.ready.set()
            try: remove_owned(self.temp)
            except OSError: pass
            self.on_exit(self)

    def boot(self):
        if not self.ready.wait(25) or self.process.poll() is not None:
            self.terminate()
            raise RuntimeError("执行引擎启动失败，请重新安装完整安装包")

    def send(self, message):
        with self.send_lock:
            if self.process.poll() is not None: raise RuntimeError("执行引擎已退出")
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def terminate(self):
        if self.process.poll() is not None: return
        try: self.send({"command": "shutdown"}); self.process.wait(timeout=0.8)
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            if self.process.poll() is None:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=8)
                else:
                    import signal
                    try: os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                self.process.wait(timeout=8)


"""System Edge window and process ownership, without touching personal profiles."""
from __future__ import annotations

import ctypes
import hashlib
import os
import subprocess
import sys
import threading
from pathlib import Path


class ChildJob:
    """Windows closes owned child process trees even if the backend crashes."""
    def __init__(self):
        self.handle = None
        if sys.platform != "win32": return
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [("processTime", ctypes.c_longlong), ("jobTime", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("minWorking", ctypes.c_size_t), ("maxWorking", ctypes.c_size_t),
                        ("processLimit", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                        ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("readOps", "writeOps", "otherOps", "readBytes", "writeBytes", "otherBytes")]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("processMemory", ctypes.c_size_t),
                        ("jobMemory", ctypes.c_size_t), ("peakProcessMemory", ctypes.c_size_t), ("peakJobMemory", ctypes.c_size_t)]
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]; k.CreateJobObjectW.restype = wintypes.HANDLE
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel, self.handle = k, k.CreateJobObjectW(None, None)
        if not self.handle: raise ctypes.WinError(ctypes.get_last_error())
        info = Extended(); info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.get_last_error(); self.close(); raise ctypes.WinError(error)

    def attach(self, process):
        if self.handle and not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            error = ctypes.get_last_error()
            process.kill(); process.wait()
            raise ctypes.WinError(error)

    def close(self):
        if self.handle: self.kernel.CloseHandle(self.handle); self.handle = None


def configure_com_cache(directory: Path):
    """Keep UIA's generated COM wrappers in the owned worker temp directory."""
    if sys.platform != "win32": return
    import importlib.machinery
    import types
    import comtypes
    directory.mkdir(parents=True, exist_ok=True)
    package = types.ModuleType("comtypes.gen")
    package.__path__ = [str(directory)]
    package.__package__ = "comtypes.gen"
    package.__spec__ = importlib.machinery.ModuleSpec("comtypes.gen", loader=None, is_package=True)
    sys.modules["comtypes.gen"] = package
    comtypes.gen = package


def find_edge() -> Path:
    candidates = [Path(os.environ.get(k, "")) / "Microsoft/Edge/Application/msedge.exe"
                  for k in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA") if os.environ.get(k)]
    if sys.platform == "win32":
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe") as key:
                    candidates.insert(0, Path(winreg.QueryValue(key, None)))
            except OSError: pass
    for p in candidates:
        if p.is_file(): return p
    raise RuntimeError("没有找到系统 Microsoft Edge。请先安装或修复 Edge，再打开 GUI Agent。")


class InstanceLock:
    def __init__(self, root):
        self.handle = self.file = None
        if sys.platform == "win32":
            from ctypes import wintypes
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            k.CreateMutexW.restype = wintypes.HANDLE
            k.CloseHandle.argtypes = [wintypes.HANDLE]
            self.kernel = k
            name = "Local\\GUIAgent-" + hashlib.sha256(str(root).lower().encode()).hexdigest()[:24]
            self.handle = k.CreateMutexW(None, False, name)
            if not self.handle: raise ctypes.WinError(ctypes.get_last_error())
            self.acquired = ctypes.get_last_error() != 183
        else:
            import fcntl
            self.file = (root / ".instance.lock").open("a+b")
            try: fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB); self.acquired = True
            except BlockingIOError: self.acquired = False

    def close(self):
        if self.handle: self.kernel.CloseHandle(self.handle); self.handle = None
        if self.file: self.file.close(); self.file = None


class EdgeWindow:
    def __init__(self, data):
        self.edge = find_edge()
        self.job = getattr(data, "job", None)
        self.profile = data.cache / "edge-ui"
        self.profile.mkdir(exist_ok=True)
        self.temp = data.temp / "edge-ui"
        self.temp.mkdir(exist_ok=True)
        self.processes = {}
        self.lock = threading.Lock()

    def launch(self, url):
        self.profile.mkdir(exist_ok=True)
        self.temp.mkdir(exist_ok=True)
        process = subprocess.Popen([str(self.edge), "--user-data-dir=" + str(self.profile), "--app=" + url,
             "--no-first-run", "--no-default-browser-check", "--disable-background-mode", "--window-size=1380,900"],
             env={**os.environ, "TMP": str(self.temp), "TEMP": str(self.temp)},
             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if self.job: self.job.attach(process)
        return process

    def owned_processes(self):
        import psutil
        roots = []
        expected = os.path.normcase(str(self.profile.absolute()))
        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                if p.info["name"].lower() != "msedge.exe": continue
                for arg in p.info["cmdline"] or []:
                    if arg.startswith("--user-data-dir=") and os.path.normcase(arg.split("=", 1)[1]) == expected:
                        roots.append(p); break
            except (psutil.Error, AttributeError): continue
        # Remember creation times while the root is alive: Edge's root can exit
        # before its renderer children. PID reuse must never target another app.
        with self.lock:
            for root in roots:
                try:
                    for p in [root, *root.children(recursive=True)]:
                        self.processes[p.pid] = p.create_time()
                except psutil.Error: pass
            owned = []
            for pid, created in list(self.processes.items()):
                try:
                    p = psutil.Process(pid)
                    if p.create_time() == created: owned.append(p)
                    else: self.processes.pop(pid, None)
                except psutil.Error: self.processes.pop(pid, None)
        return owned

    def hwnd(self):
        if sys.platform != "win32": return 0
        from ctypes import wintypes
        pids = set()
        for p in self.owned_processes():
            pids.add(p.pid)
            try: pids.update(c.pid for c in p.children(recursive=True))
            except Exception: pass
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        found = []
        callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def check(hwnd, _):
            pid = wintypes.DWORD(); user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            title = ctypes.create_unicode_buffer(512); user.GetWindowTextW(hwnd, title, 512)
            if pid.value in pids and title.value.startswith("GUI Agent"): found.append(hwnd)
            return True
        user.EnumWindows(callback(check), 0)
        return found[0] if found else 0

    def show(self, minimize=False):
        hwnd = self.hwnd()
        if hwnd:
            from ctypes import wintypes
            u = ctypes.WinDLL("user32")
            u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            u.SetForegroundWindow.argtypes = [wintypes.HWND]
            u.ShowWindow(hwnd, 6 if minimize else 9)
            if not minimize: u.SetForegroundWindow(hwnd)

    def close(self):
        import psutil
        targets = self.owned_processes()
        for p in reversed(targets):
            try: p.terminate()
            except psutil.Error: pass
        _, alive = psutil.wait_procs(targets, timeout=3)
        for p in alive:
            try: p.kill()
            except psutil.Error: pass
        psutil.wait_procs(alive, timeout=3)


class StopShortcut:
    def __init__(self, stop):
        self.available, self.thread_id = False, None
        if sys.platform != "win32": return
        ready = threading.Event()
        def pump():
            from ctypes import wintypes
            k, u = ctypes.windll.kernel32, ctypes.windll.user32
            self.thread_id = k.GetCurrentThreadId()
            self.available = bool(u.RegisterHotKey(None, 1, 0x4007, 0x1B))
            ready.set()
            if not self.available: return
            msg = wintypes.MSG()
            try:
                while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                    if msg.message == 0x312: stop()
            finally: u.UnregisterHotKey(None, 1)
        threading.Thread(target=pump, daemon=True).start()
        ready.wait(1)

    def close(self):
        if self.available and self.thread_id: ctypes.windll.user32.PostThreadMessageW(self.thread_id, 0x12, 0, 0)

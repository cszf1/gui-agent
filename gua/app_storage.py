"""App-owned storage, bounded history and deletion without following links."""
from __future__ import annotations

import base64
import ctypes
import json
import os
import shutil
import stat
import sys
import time
import uuid
from pathlib import Path

DATA_MARKER = "gui-agent-desktop-v1"
RUN_MARKER = "gui-agent-desktop-run-v1"
HISTORY_DAYS, MAX_RUNS, MAX_RUN_BYTES = 30, 50, 200 * 1024 * 1024
MAX_STATE_BYTES = 8 * 1024 * 1024
ACTIVE = {"starting", "running", "pausing", "paused", "waiting"}
DEFAULTS = dict(provider="openai", baseUrl="https://api.openai.com/v1", model="", target="desktop",
                startUrl="https://example.com", taskWindow="", safetyMode="confirm", maxSteps=50,
                pythonPath="", saveScreenshots=False)


def is_link(path: Path) -> bool:
    try:
        s = path.lstat()
        return stat.S_ISLNK(s.st_mode) or bool(getattr(s, "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


def remove_owned(path: Path) -> None:
    if is_link(path):
        if path.is_symlink(): path.unlink(missing_ok=True)
        elif stat.S_ISDIR(path.lstat().st_mode): path.rmdir()  # Also handles a dangling Windows junction.
        else: path.unlink(missing_ok=True)
    elif path.is_dir():
        for child in path.iterdir(): remove_owned(child)
        path.rmdir()
    else: path.unlink(missing_ok=True)


def bytes_in(path: Path) -> int:
    if is_link(path) or not path.exists(): return 0
    if path.is_file(): return path.stat().st_size
    total = 0
    for child in path.iterdir():
        try: total += bytes_in(child)
        except OSError: pass
    return total


def dpapi(value: bytes, *, decrypt=False) -> bytes:
    """Windows current-user DPAPI; never machine-wide or a plaintext fallback."""
    if sys.platform != "win32": raise RuntimeError("secure key storage requires Windows")
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buf = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    incoming, outgoing = Blob(len(value), buf), Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    crypt.CryptProtectData.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                                      ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    if decrypt:
        ok = crypt.CryptUnprotectData(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing))
    else:
        ok = crypt.CryptProtectData(ctypes.byref(incoming), "GUI Agent", None, None, None, 1, ctypes.byref(outgoing))
    if not ok: raise ctypes.WinError(ctypes.get_last_error())
    try: return ctypes.string_at(outgoing.data, outgoing.size)
    finally: kernel.LocalFree(outgoing.data)


def validate_settings(value: dict) -> dict:
    from urllib.parse import urlsplit
    if not isinstance(value, dict) or set(value) - (set(DEFAULTS) | {"apiKey", "clearApiKey"}):
        raise ValueError("无效的设置字段")
    out = {**DEFAULTS, **value}
    if out["provider"] not in {"openai", "anthropic"} or out["target"] not in {"desktop", "browser"}:
        raise ValueError("无效的接口或执行环境")
    if out["safetyMode"] not in {"confirm", "deny"}: raise ValueError("无效的确认策略")
    if type(out["maxSteps"]) is not int or not 1 <= out["maxSteps"] <= 200: raise ValueError("步骤数应为 1–200")
    for name, limit in [("baseUrl", 2000), ("model", 200), ("startUrl", 2000), ("taskWindow", 300),
                        ("pythonPath", 2000), ("apiKey", 8192)]:
        if name not in out: continue
        if not isinstance(out[name], str) or len(out[name]) > limit or any(c in out[name] for c in "\r\n\0"):
            raise ValueError("无效的设置内容")
        out[name] = out[name].strip()
    p = urlsplit(out["baseUrl"])
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError("Base URL 必须为不含凭据或查询参数的 HTTP(S) API 地址")
    if type(out["saveScreenshots"]) is not bool or type(out.get("clearApiKey", False)) is not bool:
        raise ValueError("无效的设置选项")
    return out


class AppStorage:
    def __init__(self, root: Path, *, portable=False, encrypt=dpapi, decrypt=lambda x: dpapi(x, decrypt=True)):
        self.root = root.absolute()
        if is_link(self.root): raise ValueError("应用数据目录不能是链接")
        self.root.mkdir(parents=True, exist_ok=True)
        marker = self.root / ".gui-agent-data"
        if marker.exists() and (is_link(marker) or marker.read_text().strip() != DATA_MARKER):
            raise ValueError("数据目录归属标记不正确")
        marker.write_text(DATA_MARKER + "\n", encoding="utf-8")
        self.runs, self.cache, self.temp = (self.root / n for n in ("runs", "cache", "tmp"))
        for p in (self.runs, self.cache, self.temp):
            if is_link(p): raise ValueError("应用数据子目录不能是链接")
            p.mkdir(exist_ok=True)
        self.portable, self.encrypt, self.decrypt = portable, encrypt, decrypt
        self.settings, self.sessions, self.key, self.encrypted_key = dict(DEFAULTS), [], "", None
        self.file = self.root / "state.json"
        if self.file.is_file() and not is_link(self.file):
            try:
                old = json.loads(self.file.read_text(encoding="utf-8"))
                self.settings = {k: v for k, v in validate_settings(old.get("settings", {})).items() if k in DEFAULTS}
                self.sessions = [s for s in old.get("sessions", []) if isinstance(s, dict) and isinstance(s.get("runs"), list)][-50:]
                self.encrypted_key = old.get("encryptedKey")
                if self.encrypted_key:
                    try: self.key = self.decrypt(base64.b64decode(self.encrypted_key)).decode()
                    except Exception: self.key = ""
                for s in self.sessions:
                    s["runs"] = [r for r in s["runs"] if isinstance(r, dict) and isinstance(r.get("events"), list)]
                    for r in s["runs"]:
                        if r.get("status") in ACTIVE: r["status"] = "interrupted"
            except (ValueError, TypeError, OSError): pass
        self.prune_history()

    def public_settings(self):
        return {**self.settings, "hasApiKey": bool(self.key), "keyPersisted": bool(self.key and self.encrypted_key)}

    def credentials(self, input=None):
        if input is None: return self.key
        if input.get("clearApiKey"): return ""
        if input.get("apiKey", "").strip(): return input["apiKey"].strip()
        same = input["provider"] == self.settings["provider"] and input["baseUrl"].rstrip("/") == self.settings["baseUrl"].rstrip("/")
        return self.key if same else ""

    def save_settings(self, value):
        value = validate_settings(value)
        key = self.credentials(value)
        sealed = None
        if key:
            try: sealed = base64.b64encode(self.encrypt(key.encode())).decode()
            except Exception: pass
        previous = self.settings, self.key, self.encrypted_key
        self.settings, self.key, self.encrypted_key = {k: value[k] for k in DEFAULTS}, key, sealed
        try: self.persist()
        except Exception:
            self.settings, self.key, self.encrypted_key = previous
            raise
        return self.public_settings()

    def new_session(self):
        s = dict(id=str(uuid.uuid4()), title="新对话", createdAt=int(time.time() * 1000), runs=[])
        self.sessions = [*self.sessions, s][-50:]
        self.persist()
        return s

    def prune_history(self):
        cutoff = time.time() * 1000 - HISTORY_DAYS * 86400_000
        all_runs = sorted((r for s in self.sessions for r in s["runs"]), key=lambda r: r.get("createdAt", 0), reverse=True)
        ids = {r.get("id") for r in all_runs if r.get("status") in ACTIVE}
        for r in all_runs:
            if len(ids) < MAX_RUNS and r.get("createdAt", 0) >= cutoff: ids.add(r.get("id"))
        for s in self.sessions:
            s["runs"] = [r for r in s["runs"] if r.get("id") in ids]
            for r in s["runs"]: r["events"] = [e for e in r["events"] if e.get("type") != "preview"][-300:]

    def persist(self):
        self.prune_history()
        def encode():
            return json.dumps(dict(version=2, settings=self.settings, encryptedKey=self.encrypted_key,
                                   sessions=self.sessions), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        data = encode()
        while len(data) > MAX_STATE_BYTES:
            rows = [(s, r) for s in self.sessions for r in s["runs"] if r.get("status") not in ACTIVE]
            if rows:
                s, r = min(rows, key=lambda x: x[1].get("createdAt", 0)); s["runs"].remove(r)
            else:
                r = next((r for s in self.sessions for r in s["runs"] if r["events"]), None)
                if r is None: raise ValueError("本地记录过大，请清理后重试")
                r["events"].pop(0)
            data = encode()
        tmp = self.file.with_suffix(".json.tmp")
        try:
            with tmp.open("wb") as f: f.write(data)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.file)
        finally: tmp.unlink(missing_ok=True)

    def owned_runs(self):
        rows = []
        if is_link(self.runs): return rows
        for p in self.runs.iterdir():
            try:
                uuid.UUID(p.name)
                if not p.is_dir() or is_link(p): continue
                marker = p / ".gui-agent-run"
                if marker.exists():
                    owned = not is_link(marker) and marker.read_text().strip() == RUN_MARKER
                else:
                    meta = p / "meta.json"
                    if is_link(meta) or is_link(p / "steps.jsonl"): continue
                    m = json.loads(meta.read_text(encoding="utf-8"))
                    owned = isinstance(m.get("task"), str) and isinstance(m.get("platform"), str) and (p / "steps.jsonl").is_file()
                if owned: rows.append((p, p.stat().st_mtime, bytes_in(p)))
            except (ValueError, OSError): continue
        return sorted(rows, key=lambda r: r[1], reverse=True)

    def prune_runs(self, active_id=None, *, days=HISTORY_DAYS, count=MAX_RUNS, max_bytes=MAX_RUN_BYTES):
        rows = self.owned_runs()
        used, kept = 0, 0
        for p, _, size in rows:
            if p.name == active_id: used += size; kept += 1
        for p, modified, size in rows:
            if p.name == active_id: continue
            if modified < time.time() - days * 86400 or kept >= count or used + size > max_bytes:
                try: remove_owned(p)
                except OSError: pass  # A locked report is retried at next startup.
            else: used += size; kept += 1

    def clean_transient(self, *, cache=True):
        failures = []
        for directory in ([self.temp, self.cache] if cache else [self.temp]):
            if is_link(directory): continue
            for p in directory.iterdir():
                try: remove_owned(p)
                except OSError: failures.append(p.name)
        return failures

    def clear_history(self):
        for p, _, _ in self.owned_runs(): remove_owned(p)
        self.sessions = []
        return self.new_session()

    def info(self):
        rows = self.owned_runs()
        return dict(dataPath=str(self.root), portable=self.portable, runCount=len(rows), runBytes=sum(r[2] for r in rows),
                    cacheBytes=bytes_in(self.cache) + bytes_in(self.temp), historyDays=HISTORY_DAYS,
                    maxRuns=MAX_RUNS, maxRunBytes=MAX_RUN_BYTES)

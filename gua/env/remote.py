"""RemoteEnv（v0.6）：通过 HTTP 操作一台沙箱电脑（gua/sandbox/daemon.py，本机 Xvfb 或 Docker / 云端）。

- observe：截图（PNG）+ 前台窗口 / 窗口列表 / 指针 + AT-SPI 树 → 统一 UIElement（与 Linux 后端同一套转换）。
- execute：真实输入（xdotool，会移动沙箱里的指针）或语义动作 invoke（AT-SPI action / EditableText，
  不移动指针）；语义动作按节点路径 + 原始角色 / 名字做身份核对，失配 → stale_target。
- run_tool：shell / file 直接在沙箱里执行（与宿主机隔离）；agent 侧 ToolRegistry 仍决定能不能执行。
- 人工接管：takeover() 后沙箱拒绝 agent 的一切动作与截图（返回 paused_for_human）；observe 会等待交还
  （wait_for_handback=True 时轮询，超时 → UserAbort）。交还后 epoch+1，之前的绑定全部失效，agent 重新观察。
- 快照 / 重置：snapshot(name) / reset(name)，评测里每个任务从同一状态开始（可复现）。
- live view：live_view_url（只看）/ takeover_url（可操作），由沙箱启动器提供（x11vnc + noVNC）。

HTTP 客户端显式禁用系统代理（沙箱一般在本机或内网，不能被 HTTP(S)_PROXY 劫持）。
"""
from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.request
from dataclasses import replace
from typing import Optional

from PIL import Image

from ..actions import Action
from ..errors import UserAbort
from .a11y import atspi_raws, finalize
from .base import Env, ExecResult, Observation


class RemoteError(RuntimeError):
    pass


class RemoteEnv(Env):
    platform = "remote"
    scroll_unit_px = 60
    semantic_actions = True
    targeted_input = True        # 指定字段输入：先点击聚焦，重新观察核对焦点节点路径，再输入

    def __init__(self, url: str, token: str, timeout: float = 30.0, max_elements: int = 150,
                 wait_for_handback: bool = True, takeover_timeout: float = 600.0, poll: float = 0.5):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.max_elements = max_elements
        self.wait_for_handback = wait_for_handback
        self.takeover_timeout = takeover_timeout
        self.poll = poll
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._snapshot_id = ""
        self._paths: dict[int, dict] = {}
        self.takeover_events: list[dict] = []
        self.input_epoch = 0

    # ------------------------------------------------------------------ HTTP
    def _req(self, method: str, path: str, body: Optional[dict] = None, raw: bool = False):
        data = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"X-Gua-Token": self.token, "Content-Type": "application/json"})
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                payload = r.read()
                return payload if raw else json.loads(payload.decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                obj = json.loads(e.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                obj = {"ok": False, "error": f"http {e.code}"}
            obj["_status"] = e.code
            if raw:
                raise RemoteError(obj.get("error", f"http {e.code}")) from None
            return obj

    def health(self) -> dict:
        return self._req("GET", "/health")

    # ------------------------------------------------------------------ 接管
    def takeover(self) -> dict:
        r = self._req("POST", "/takeover")
        self.takeover_events.append({"event": "takeover", "time": time.time()})
        return r

    def handback(self) -> dict:
        r = self._req("POST", "/handback")
        self.takeover_events.append({"event": "handback", "time": time.time()})
        self.input_epoch += 1
        return r

    def _wait_handback(self) -> None:
        if not self.wait_for_handback:
            raise RemoteError("paused_for_human")
        t0 = time.monotonic()
        self.takeover_events.append({"event": "agent_waiting", "time": time.time()})
        while time.monotonic() - t0 < self.takeover_timeout:
            if not self.health().get("takeover"):
                self.takeover_events.append({"event": "agent_resumed", "time": time.time(),
                                             "waited": round(time.monotonic() - t0, 2)})
                self.input_epoch += 1
                return
            time.sleep(self.poll)
        raise UserAbort("human takeover was not handed back in time")

    # ------------------------------------------------------------------ 观察
    def observe(self, with_elements: bool = True) -> Observation:
        for _ in range(3):
            try:
                png = self._req("GET", "/screenshot", raw=True)
            except RemoteError as e:
                if "paused_for_human" in str(e):
                    self._wait_handback()
                    continue
                raise
            meta = self._req("GET", f"/observe?elements={1 if with_elements else 0}")
            if meta.get("_status") == 423:
                self._wait_handback()
                continue
            break
        else:
            raise RemoteError("could not observe the sandbox")
        img = Image.open(io.BytesIO(png)).convert("RGB")
        elems, text = [], ""
        tree = meta.get("tree")
        if with_elements and isinstance(tree, dict) and "error" not in tree:
            elems, text = finalize(atspi_raws(tree), img.size, self.max_elements)
        self._snapshot_id = str(meta.get("snapshot_id", ""))
        self._paths = {e.id: dict(e.attrs) for e in elems}
        self._native = {e.id: e.native_role for e in elems}
        ptr = meta.get("pointer")
        focused = any(e.focused for e in elems)
        return Observation(screenshot=img, timestamp=time.time(), screen_size=img.size, dpi_scale=1.0,
                           active_window=meta.get("active_window", ""), active_process=meta.get("active_process", ""),
                           windows=list(meta.get("windows") or []), elements=elems, platform="remote", text=text,
                           cursor=tuple(ptr) if ptr else None, focus_state="known" if focused else "",
                           snapshot_id=self._snapshot_id)

    def bind_action(self, action: Action, obs: Observation) -> Action:
        return replace(action, binding={"snapshot_id": obs.snapshot_id})

    def element_identity(self, element):
        return element.attrs.get("atspi_path") or None

    def pointer_position(self):
        r = self._req("GET", "/pointer")
        p = r.get("pointer")
        return tuple(p) if p else None

    def foreground_token(self) -> str:
        return str(self._req("GET", "/pointer").get("foreground", ""))

    # ------------------------------------------------------------------ 执行
    def execute(self, a: Action) -> ExecResult:
        t0 = time.time()
        if not self.supports(a.type):
            return ExecResult(False, f"unsupported action {a.type} on remote sandbox", t0, time.time())
        snap = (a.binding or {}).get("snapshot_id", "")
        if a.type == "invoke":
            attrs = self._paths.get(a.element_id) if a.element_id is not None else None
            if not attrs or not attrs.get("atspi_path") or snap != self._snapshot_id:
                return ExecResult(False, "stale_target: semantic action needs the latest observed element", t0,
                                  time.time())
            body = {"path": attrs["atspi_path"], "name_raw": attrs.get("atspi_name", ""),
                    "native_role": self._native_role(a.element_id), "method": a.method, "text": a.text,
                    "snapshot_id": snap}
            r = self._req("POST", "/semantic", body)
        else:
            err = self._bounds_error(a, 10 ** 6, 10 ** 6)
            if err:
                return ExecResult(False, err, t0, time.time())
            body = {k: v for k, v in a.to_dict().items() if k != "reason"}
            body["snapshot_id"] = snap
            r = self._req("POST", "/input", body)
        if r.get("_status") == 423:
            return ExecResult(False, "paused_for_human: a human has control; observe again after hand-back", t0,
                              time.time(), route="remote")
        return ExecResult(bool(r.get("ok")), str(r.get("error") or ""), t0, time.time(),
                          route=str(r.get("route") or "remote"))

    def _native_role(self, eid: int) -> str:
        return getattr(self, "_native", {}).get(eid, "")

    def run_tool(self, a: Action) -> Optional[ExecResult]:
        t0 = time.time()
        if a.type == "shell":
            r = self._req("POST", "/shell", {"argv": list(a.command)})
        elif a.type == "file":
            r = self._req("POST", "/files", {"method": a.method, "path": a.path, "text": a.text})
        else:
            return None
        if r.get("_status") == 423:
            return ExecResult(False, "paused_for_human: a human has control", t0, time.time(), route=a.type)
        return ExecResult(bool(r.get("ok")), str(r.get("error") or ""), t0, time.time(),
                          output=str(r.get("output") or ""), route=f"remote_{r.get('route') or a.type}")

    # ------------------------------------------------------------------ 任务生命周期
    def launch(self, argv: list[str]) -> dict:
        return self._req("POST", "/launch", {"argv": list(argv)})

    def snapshot(self, name: str = "default") -> dict:
        return self._req("POST", "/snapshot", {"name": name})

    def reset(self, name: str = "default") -> None:  # type: ignore[override]
        self._req("POST", "/reset", {"name": name})

    def read_file(self, path: str) -> str:
        r = self._req("POST", "/files", {"method": "read", "path": path})
        return str(r.get("output") or "") if r.get("ok") else ""

    def live_view(self) -> dict:
        return self._req("GET", "/liveview")

    def focus_window(self, title_substring: str) -> bool:
        r = self._req("POST", "/input", {"type": "focus_window", "text": title_substring})
        return bool(r.get("ok"))

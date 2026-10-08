"""Desktop worker: JSON-lines over private stdin/stdout pipes, no listening port.

The desktop shell owns credentials, history and the stop shortcut. This process
owns every platform object on its main thread (including Playwright). A reader
thread only delivers controls and human answers; it never touches the computer.
"""
from __future__ import annotations

import io
import json
import os
import queue
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .actions import Action
from .config import build_agent, build_env, load_config, merge
from .env import detect_platform, platform_status
from .env.base import Env, ExecResult
from .errors import UserAbort
from .logger import TrajectoryLogger, _jsonable
from .sensitive import Scrubber, focus_target, is_password_el

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


@contextmanager
def model_credentials(api_key: str):
    """Only client construction reads this variable. Launched apps must not inherit it."""
    name = "GUI_AGENT_DESKTOP_API_KEY"
    original = os.environ.get(name)
    os.environ[name] = api_key or "EMPTY"
    try:
        yield
    finally:
        if original is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = original


def model_spec(settings: dict) -> dict:
    provider = settings.get("provider", "openai")
    if provider not in {"openai", "anthropic"}:
        raise ValueError("请选择 OpenAI 兼容接口或 Anthropic Messages 接口")
    base = str(settings.get("baseUrl", "")).strip().rstrip("/")
    parsed = urlsplit(base)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Base URL 必须是完整的 HTTP(S) API 地址，不能包含凭据或查询参数")
    model = str(settings.get("model", "")).strip()
    if not model or len(model) > 200:
        raise ValueError("请填写服务商提供的模型名称")
    # AnthropicLLM appends /v1/messages; accept the familiar /v1 API root too.
    if provider == "anthropic" and base.endswith("/v1"):
        base = base[:-3]
    return {"provider": provider, "model": model, "base_url": base,
            "api_key_env": "GUI_AGENT_DESKTOP_API_KEY", "image_max_side": 1280,
            "max_tokens": 2048}


def desktop_config(settings: dict, *, demo: bool = False, headless: bool = False) -> dict:
    cfg = load_config(ROOT / "configs/default.yaml")
    target = "browser" if demo else settings.get("target", "desktop")
    if target not in {"browser", "desktop"}:
        raise ValueError("无效的执行环境")
    platform = "web" if target == "browser" else detect_platform()
    if platform == "mock":
        raise ValueError("没有可用的桌面会话。Windows 需要登录桌面；Linux 需要 X11")
    steps = settings.get("maxSteps", 50)
    if type(steps) is not int or not 1 <= steps <= 200:
        raise ValueError("最大步骤数应为 1–200")
    safety = settings.get("safetyMode", "confirm")
    if safety not in {"confirm", "deny"}:
        raise ValueError("无效的确认模式")
    overrides = {"env": {"platform": platform, "web": {"headless": headless}},
                 "agent": {"max_steps": steps}, "safety": {"mode": safety},
                 "grounding": {"coord": "norm1000", "refine": True, "min_confidence": 0.75},
                 "actor": {"som": True}}
    if sys.platform == "win32":
        # Desktop releases use the installed system Edge, never download Chromium.
        overrides["env"]["web"]["channel"] = "msedge"
    if not demo:
        spec = model_spec(settings)
        overrides["models"] = {"planner": spec, "actor": None,
                               "grounder": {**spec, "max_tokens": 256},
                               "verifier": None, "reflector": None}
    return merge(cfg, overrides)


class Control:
    def __init__(self, emit: Callable):
        self.emit = emit
        self.condition = threading.Condition()
        self.paused = False
        self.stopped = False
        self.epoch = 0
        self.pending: dict[str, object] = {}

    def prepare(self):
        with self.condition:
            self.paused = self.stopped = False
            self.pending.clear()

    def command(self, msg: dict):
        with self.condition:
            kind = msg.get("command")
            if kind == "pause" and not self.paused:
                self.paused = True
                self.epoch += 1
                self.emit("state", state="pausing")
            elif kind == "resume":
                self.paused = False
            elif kind in {"stop", "shutdown"}:
                self.stopped = True
                self.paused = False
            elif kind == "respond" and msg.get("requestId") in self.pending:
                self.pending[msg["requestId"]] = msg
            self.condition.notify_all()

    def checkpoint(self):
        with self.condition:
            if self.stopped:
                raise UserAbort("stopped from desktop app")
            waited = self.paused
            if waited:
                self.emit("state", state="paused")
            while self.paused and not self.stopped:
                self.condition.wait()
            if self.stopped:
                raise UserAbort("stopped from desktop app")
            if waited:
                self.emit("state", state="running")
            return self.epoch

    def request(self, kind: str, question: str, action=None):
        request_id = uuid.uuid4().hex
        with self.condition:
            self.pending[request_id] = None
            self.emit("request", requestId=request_id, kind=kind, question=question,
                      action=action)
            while self.pending[request_id] is None and not self.stopped:
                self.condition.wait()
            if self.stopped:
                self.pending.pop(request_id, None)
                raise UserAbort("stopped while waiting for user")
            answer = self.pending.pop(request_id)
            self.emit("state", state="running" if not self.paused else "pausing")
            return answer.get("approved") is True if kind == "confirm" else answer.get("answer", "")


class StreamingLogger(TrajectoryLogger):
    def __init__(self, root: Path, run_id: str, emit: Callable, sanitize: Callable = lambda value: value,
                 save_images: bool = False):
        super().__init__(root, run_id, save_images=save_images)
        (self.dir / ".gui-agent-run").write_text("gui-agent-desktop-run-v1\n", encoding="utf-8")
        self.emit = emit
        self.sanitize = sanitize
        self.privacy_sent = False
        self.last_preview = 0.0
        self.last_target = ""

    def privacy(self):
        if self.scrubber.images_blocked and not self.privacy_sent:
            self.privacy_sent = True
            self.emit("privacy")

    def preview(self, obs):
        # Apply the same privacy boundary *before* any image enters the UI pipe.
        if (focus_target(obs)[1] in {"password", "unknown"}
                or any(is_password_el(e) for e in obs.elements)):
            self.scrubber.mark_sensitive()
        self.privacy()
        if self.scrubber.images_blocked or time.monotonic() - self.last_preview < 0.2:
            return
        import base64
        img = obs.screenshot.copy().convert("RGB")
        img.thumbnail((1000, 750))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        self.emit("preview", image="data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode(),
                  title=self.scrub(obs.active_window), url=self.scrub(obs.url))
        self.last_preview = time.monotonic()

    def step(self, **rec):
        if rec.get("kind") == "step" and self.last_target:
            rec["displayTarget"] = self.last_target
            self.last_target = ""
        rec = self.sanitize(rec)
        super().step(**rec)
        self.privacy()
        self.emit("log", record=self.scrub(rec))

    def meta(self, **meta):
        super().meta(**self.sanitize(meta))


class ControlledEnv(Env):
    def __init__(self, env: Env, control: Control, log: StreamingLogger):
        self.inner, self.control, self.log = env, control, log
        self.platform, self.scroll_unit_px = env.platform, env.scroll_unit_px
        self.targeted_input = env.targeted_input
        self.observed_epoch = -1
        self.last_observation = None

    def observe(self, with_elements=True):
        epoch = self.control.checkpoint()
        obs = self.inner.observe(with_elements=with_elements)
        if with_elements:
            self.observed_epoch = epoch
            self.last_observation = obs
            self.log.preview(obs)
        return obs

    def bind_action(self, action, obs):
        return self.inner.bind_action(action, obs)

    def element_identity(self, element):
        return self.inner.element_identity(element)

    @property
    def input_epoch(self):
        return self.observed_epoch

    def wait_until_stable(self, timeout=5.0, interval=0.4, **kwargs):
        # Preserve the backend's event-aware wait. If a pause occurred while it
        # was waiting, resume with fresh evidence rather than reusing that frame.
        while True:
            epoch = self.control.checkpoint()
            obs, stable = self.inner.wait_until_stable(timeout=timeout, interval=interval, **kwargs)
            if self.control.checkpoint() != epoch:
                continue
            self.observed_epoch, self.last_observation = epoch, obs
            self.log.preview(obs)
            return obs, stable

    def execute(self, action: Action):
        epoch = self.control.checkpoint()
        # A person can alter the computer during a pause. Discard the old action;
        # the normal verification/recovery loop observes and decides again.
        if epoch != self.observed_epoch:
            now = time.time()
            return ExecResult(False, "observation_invalidated_by_pause: observe again", now, now)
        obs = self.last_observation
        if obs is not None and action.is_pointer:
            element = obs.element(action.element_id) if action.element_id is not None else obs.element_at(*action.point) if action.point else None
            if element is not None:
                self.log.last_target = "受保护的输入框" if element.is_password else self.log.scrub(element.name)
        return self.inner.execute(action)

    def focus_window(self, title_substring):
        self.control.checkpoint()
        return self.inner.focus_window(title_substring)

    def close(self):
        self.inner.close()


def public_error(exc: Exception) -> str:
    code = getattr(exc, "status_code", getattr(exc, "code", None))
    if code in {401, 403}:
        return "模型服务拒绝访问，请检查 API Key 和模型权限。"
    if code == 404:
        return "模型或 API 地址不存在，请检查 Base URL 和模型名称。"
    if code == 429:
        return "模型服务限流或额度不足，请检查服务商账户。"
    if code == 400:
        return "模型服务拒绝请求，请确认模型支持图片输入及当前 API 协议。"
    if isinstance(exc, ValueError):
        return str(exc)
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return "执行引擎缺少依赖，请按桌面 App 启动说明安装对应平台依赖。"
    if "timeout" in type(exc).__name__.lower():
        return "模型请求超时，请检查网络或服务商状态。"
    # Never return vendor response bodies, headers, tracebacks or credentials.
    return f"执行引擎遇到 {type(exc).__name__}，请检查运行环境和模型配置。"


class Worker:
    def __init__(self, output):
        self.output = output
        self.lock = threading.Lock()
        self.commands = queue.Queue()
        self.run_id = ""
        self.credentials = Scrubber()
        self.control = Control(self.emit)
        self.env = None
        self.env_key = None
        self.privacy_blocked = False

    def emit(self, event_type: str, **payload):
        event = self.credentials.scrub_obj({"type": event_type, "runId": self.run_id,
                                           "time": time.time(), **payload})
        with self.lock:
            self.output.write(json.dumps(event, ensure_ascii=False, default=_jsonable) + "\n")
            self.output.flush()

    def read(self, source):
        for line in source:
            try:
                msg = json.loads(line)
                if not isinstance(msg, dict):
                    continue
                cmd = msg.get("command")
                if cmd in {"run", "test"}:
                    self.control.prepare()
                    self.commands.put(msg)
                elif cmd in {"pause", "resume", "stop", "respond", "shutdown"}:
                    if cmd != "shutdown" and msg.get("runId") != self.run_id:
                        continue
                    self.control.command(msg)
                    if cmd == "shutdown":
                        self.commands.put(None)
                        return
            except (ValueError, TypeError):
                continue
        self.control.command({"command": "shutdown"})
        self.commands.put(None)

    def test(self, settings: dict):
        from PIL import Image
        from .llm import make_llm
        spec = model_spec(settings)
        with model_credentials(settings.get("apiKey", "")):
            model = make_llm(spec, "connection_test")
        try:
            model.chat("You are testing an API connection.",
                       "This is a generated test image. Reply briefly with OK.",
                       [Image.new("RGB", (64, 64), "#28b899")])
        finally:
            client = getattr(model, "client", None)
            if client is not None:
                client.close()
        self.emit("tested", ok=True, message="模型连接和图片输入测试通过。")

    def run(self, msg: dict):
        settings = msg.get("settings") or {}
        demo = msg.get("demo") is True
        cfg = desktop_config(settings, demo=demo, headless=msg.get("headless") is True)
        task = msg.get("task", "")
        if not isinstance(task, str) or not task.strip() or len(task) > 20000:
            raise ValueError("请输入任务，长度不超过 20000 字符")
        self.control.checkpoint()
        self.emit("state", state="running")
        log = StreamingLogger(Path(msg["runsRoot"]), self.run_id, self.emit, self.credentials.scrub_obj,
                              save_images=settings.get("saveScreenshots") is True)
        agent = None
        try:
            start_url = settings.get("startUrl", "").strip() if not demo else ""
            if demo:
                start_url = str(ROOT / "tasks/web_assets/form.html")
            if cfg["env"]["platform"] == "web" and not start_url:
                start_url = "about:blank"
            env_key = (cfg["env"]["platform"], cfg["env"]["web"]["headless"], start_url)
            if self.env is None or self.env_key != env_key or demo:
                if self.env is not None:
                    self.env.close()
                self.env = None
                self.privacy_blocked = False
                if start_url:
                    from .env.web import to_url
                    cfg["env"]["web"]["start_url"] = to_url(start_url, ROOT)
                self.env = build_env(cfg)
                self.env_key = env_key
            if self.privacy_blocked:
                log.scrubber.mark_sensitive()
            llms = None
            if demo:
                from .scripted import ScriptedPolicy
                demo_task = json.loads((ROOT / "tasks/web/form_submit.json").read_text(encoding="utf-8"))
                demo_task["demo"]["subgoals"][0]["goal"] = "填写姓名和邮箱"
                demo_task["demo"]["subgoals"][1]["goal"] = "选择 Pro 套餐、订阅并提交"
                task = demo_task["instruction"]
                llms = ScriptedPolicy(demo_task["demo"]).llms()
            wrapped = ControlledEnv(self.env, self.control, log)
            with model_credentials(settings.get("apiKey", "")):
                agent = build_agent(cfg, wrapped, log, llms=llms,
                                    task_window=settings.get("taskWindow", ""),
                                    confirm_fn=lambda action, reason: self.control.request("confirm", reason, action.to_dict()),
                                    ask_fn=lambda question: self.control.request("ask", question))
            agent.before_step.append(lambda step, _: self.control.checkpoint())
            if not demo:
                history = str(msg.get("context", ""))[:10000]
                if history:
                    task = f"{task}\n\nPrevious tasks in this conversation (context only):\n{history}"
            self.control.checkpoint()
            result = agent.run(task)
            report = log.close()
            self.privacy_blocked = log.scrubber.images_blocked
            self.emit("result", result=asdict(result), report=str(report) if report else None)
        finally:
            self.privacy_blocked = log.scrubber.images_blocked
            log.close(report=False)
            if agent is not None:
                # Close HTTP pools while preserving the computer between tasks.
                seen = set()
                for comp in (agent.planner, agent.actor, agent.grounder, agent.verifier, agent.reflector):
                    llm = getattr(comp, "llm", None)
                    client = getattr(llm, "client", None)
                    if client is not None and id(client) not in seen:
                        seen.add(id(client))
                        client.close()

    def main(self, source):
        self.emit("ready", python=sys.version.split()[0], platform=sys.platform,
                  backends=platform_status())
        threading.Thread(target=self.read, args=(source,), daemon=True).start()
        try:
            while True:
                msg = self.commands.get()
                if msg is None:
                    break
                self.run_id = str(msg.get("runId", uuid.uuid4().hex))
                self.credentials = Scrubber()
                self.credentials.add(str((msg.get("settings") or {}).get("apiKey", "")), explicit=True)
                try:
                    if msg["command"] == "test":
                        self.test(msg.get("settings") or {})
                    else:
                        self.run(msg)
                except UserAbort:
                    self.emit("result", result={"status": "user_abort", "claimed_done": False,
                                                 "message": "用户已停止执行"})
                except Exception as exc:
                    self.emit("error", message=public_error(exc))
        finally:
            if self.env is not None:
                self.env.close()


def main():
    if sys.platform == "win32" and os.environ.get("GUA_APP_TEMP"):
        from .app_native import configure_com_cache
        configure_com_cache(Path(os.environ["GUA_APP_TEMP"]) / "comtypes")
    # PyInstaller's Windows bootloader can ignore PYTHONIOENCODING and keep the
    # system code page. The desktop pipe protocol is always UTF-8 in both ways.
    for name in ("stdin", "stdout", "stderr"):
        reconfigure = getattr(getattr(sys, name), "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace" if name == "stderr" else "strict")
    # All normal prints go to stderr. Only Worker.emit writes protocol messages.
    output = sys.stdout
    sys.stdout = sys.stderr
    Worker(output).main(sys.stdin)


if __name__ == "__main__":
    main()

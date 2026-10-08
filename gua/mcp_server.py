"""`gua mcp`：把 gua 的观察 / 执行 / 验证作为 MCP 工具提供给任意 agent（Claude Code、Codex、Cursor……）。

Cua Driver 以 `cua-driver mcp` 暴露 “截图 + 无障碍树 + 动作”，并建议调用方“每次点击后再截一次图确认”；
本项目把**确认这一步做进工具本身**：`act` 返回的不是 “OK”，而是规则验证结论（成功 / 无效果 / 被弹窗挡住 /
后置条件失败……）、证据、实际执行模态与是否后台；`verify` 产出带新鲜度检查的完成凭据。

传输：MCP stdio（每行一个 JSON-RPC 2.0 消息，stdout 只写协议消息，日志写 stderr）。不依赖 mcp SDK。
工具：observe / act / verify / run_task / takeover / handback / snapshot / reset / live_view。

安全：所有动作经过同一个 SafetyGuard + 混合执行器（与 `gua run` 完全相同的闸门与验证）；MCP 调用方无法在
终端里回答确认问题，所以需要确认的动作默认**拒绝**（--yes 才放行，仅用于一次性沙箱）；ask_user 不会读 stdin。
返回内容经过 Scrubber 清洗；隐私阻断后不再返回截图。
"""
from __future__ import annotations

import base64
import io
import json
import sys
import threading
import time
import traceback
from typing import Any, Optional

from . import __version__
from .actions import ActionParseError, parse_action
from .env.base import ExecResult
from .verify import Verdict

PROTOCOL = "2025-06-18"

TOOLS = [
    {"name": "observe", "description": "Capture the current screen: foreground window, URL, interactive elements "
                                       "(with ids and available semantic methods) and visible text, plus a screenshot.",
     "inputSchema": {"type": "object", "properties": {
         "include_image": {"type": "boolean", "default": True},
         "max_elements": {"type": "integer", "default": 80, "minimum": 1, "maximum": 300}}}},
    {"name": "act", "description": "Execute ONE gua action (JSON, same schema as gua's actor: click/type/hotkey/scroll/"
                                   "invoke/shell/file/api/...). Element ids refer to the latest observe. The action "
                                   "passes the safety gate, runs (semantic/background first when requested), and is "
                                   "verified by rules; the result reports verdict, evidence, modality and route.",
     "inputSchema": {"type": "object", "required": ["action"], "properties": {
         "action": {"type": "object", "description": "e.g. {\"type\":\"invoke\",\"element_id\":3,\"method\":\"toggle\","
                                                     "\"expect\":[{\"kind\":\"element_state\",\"name\":\"Subscribe\","
                                                     "\"checked\":true}]}"}}}},
    {"name": "verify", "description": "Check that something is TRUE NOW and freshly produced: expect_text must be "
                                      "visible and not stale relative to the baseline (set by 'mark' or the first "
                                      "observe); postconditions are evaluated against the last pre-action state. "
                                      "Returns a verified-done receipt or the reason it is not verified.",
     "inputSchema": {"type": "object", "properties": {
         "expect_text": {"type": "string"}, "postconditions": {"type": "array", "items": {"type": "object"}},
         "goal": {"type": "string"}, "mark": {"type": "boolean", "description": "reset the freshness baseline"}}}},
    {"name": "run_task", "description": "Run a whole task with gua's planner/actor/verifier loop (needs models in the "
                                        "config) or with a scripted 'demo' (deterministic replay). Returns status, "
                                        "receipts and modality statistics.",
     "inputSchema": {"type": "object", "required": ["task"], "properties": {
         "task": {"type": "string"}, "demo": {"type": "object"}, "max_steps": {"type": "integer", "minimum": 1}}}},
    {"name": "takeover", "description": "Remote sandbox only: hand control to a human (agent actions and screenshots "
                                        "pause). Returns the interactive live-view URL.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "handback", "description": "Remote sandbox only: return control to the agent; observe again afterwards.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "snapshot", "description": "Remote sandbox only: save the task state.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}}},
    {"name": "reset", "description": "Remote sandbox only: restore a saved task state.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}}},
    {"name": "live_view", "description": "Remote sandbox only: view-only and takeover URLs.",
     "inputSchema": {"type": "object", "properties": {}}},
]


class Session:
    """一个 MCP 连接对应一个环境 + 一个（无模型的）GUIAgent 外壳，复用 agent 的闸门 / 执行 / 验证路径。"""

    def __init__(self, cfg: dict, env=None, allow_risky: bool = False):
        from .config import build_agent, build_env
        self.cfg = cfg
        self.env = env if env is not None else build_env(cfg)
        cfg.setdefault("safety", {})["mode"] = "allow" if allow_risky else "deny"
        from .llm.base import ScriptedLLM
        stub = ScriptedLLM(fn=lambda s, t, i: '{"verdict":"uncertain","evidence":"no model in MCP session"}')
        self.agent = build_agent(cfg, self.env, None, llms={"planner": stub, "actor": stub, "grounder": None,
                                                           "verifier": None, "reflector": None},
                                 ask_fn=lambda q: None)
        self.agent.verifier.llm = None              # act / verify 只用规则（L0/L1）；不会悄悄调用模型
        if getattr(self.env, "platform", "") == "remote":
            self.env.wait_for_handback = False      # MCP 调用方自己决定何时交还；接管期间立即返回 paused_for_human
        self.last = None
        self.baseline = None
        self.lock = threading.Lock()
        self.events: list[dict] = []

    # ------------------------------------------------------------------ helpers
    def _image(self, obs) -> Optional[dict]:
        if getattr(self.agent.scrubber, "images_blocked", False):
            return None
        img = obs.screenshot.copy()
        img.thumbnail((1280, 1280))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return {"type": "image", "data": base64.b64encode(buf.getvalue()).decode("ascii"), "mimeType": "image/png"}

    def _describe(self, obs, max_elements: int) -> str:
        sc = self.agent._scrub
        hint = self.env.semantic_methods if getattr(self.env, "semantic_actions", False) else None
        lines = []
        for e in obs.elements[:max_elements]:
            b = e.brief()
            if hint is not None:
                ms = sorted(hint(e) - {"focus", "scroll_into_view"})
                if ms:
                    b += " {" + ",".join(ms) + "}"
            lines.append(b)
        return sc("\n".join([f"window: {obs.active_window!r}", f"url: {obs.url}" if obs.url else "",
                             f"platform: {obs.platform}", "elements:", *lines, "visible text:",
                             (obs.text or "")[:2000]]).strip())

    # ------------------------------------------------------------------ tools
    def observe(self, include_image: bool = True, max_elements: int = 80) -> dict:
        obs = self.agent._observe()
        self.last = obs
        if self.baseline is None:
            self.baseline = obs
        content = [{"type": "text", "text": self._describe(obs, int(max_elements))}]
        if include_image:
            img = self._image(obs)
            if img:
                content.append(img)
        return {"content": content, "structuredContent": {"snapshot_id": obs.snapshot_id,
                                                          "elements": len(obs.elements)}}

    def act(self, action: dict) -> dict:
        try:
            a = parse_action(action)
        except ActionParseError as e:
            return _err(f"invalid action: {e.feedback()}")
        if a.type in {"done", "fail"}:
            return _err("done/fail are not executable; use verify to prove completion")
        before = self.last or self.agent._observe()
        a, src = self.agent._resolve(a, before)
        if src == "grounding_failed":
            return _err("could not resolve the target on the latest observation; observe again")
        view_d, view_s = self.agent._view(a, before)
        res: ExecResult = self.agent._execute_gated(a, before, "mcp")
        if (res.error or "").startswith("paused_for_human"):
            out = {"executed": False, "verdict": "paused_for_human", "error": res.error, "route": res.route}
            return {"content": [{"type": "text", "text": json.dumps(out)}], "structuredContent": out, "isError": True}
        after, stable = self.agent._settle()
        check = self.agent.verifier.check_step(before, after, a, res, stable, "", task_window="",
                                               action_desc=view_s)
        self.agent._remember_unconfirmed(a, before, res, check)
        self.last = after
        sig = check.signals or {}
        out = {"executed": res.ok, "verdict": check.verdict.value, "level": check.level,
               "evidence": self.agent._scrub(check.evidence), "route": res.route,
               "modality": res.signals.get("modality", ""), "background": res.signals.get("background", False),
               "pointer_moved": res.signals.get("pointer_moved"), "fallback": res.signals.get("fallback_reason", ""),
               "postconditions": sig.get("postconditions", []), "action": view_d,
               "error": self.agent._scrub(res.error or "")}
        if res.output and a.type in {"shell", "file", "api"}:
            out["output"] = self.agent._scrub(res.output)[:4000]
        out = self.agent.scrubber.scrub_obj(out)
        return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
                "structuredContent": out, "isError": check.verdict not in {Verdict.SUCCESS, Verdict.IN_PROGRESS}}

    def verify(self, expect_text: str = "", postconditions: Optional[list] = None, goal: str = "",
               mark: bool = False) -> dict:
        from .verify.postconditions import evaluate, validate_postcondition
        from .verify.receipts import make_receipt
        obs, stable = self.agent._settled_for_check(None)
        if mark or self.baseline is None:
            self.baseline = obs
            if mark:
                self.last = obs
                return {"content": [{"type": "text", "text": "baseline marked"}],
                        "structuredContent": {"marked": True}}
        for pc in postconditions or []:
            err = validate_postcondition(pc)
            if err:
                return _err(f"invalid postcondition: {err}")
        if not expect_text and not postconditions:
            return _err("give expect_text and/or postconditions")
        results, level, evidence, verdicts = [], "L1", [], []
        if postconditions:
            rep = evaluate(postconditions, self.baseline, obs, use_a11y=self.agent.policy.a11y_rules)
            results = rep.to_list()
            evidence.append(rep.evidence())
            verdicts.append(rep.verdict)
        if expect_text:
            c = self.agent.verifier.check_goal(obs, goal or "verify", "", expect_text, stable=stable,
                                               baseline=self.baseline)
            evidence.append(c.evidence)
            level = c.level
            verdicts.append("success" if c.verdict == Verdict.SUCCESS else c.verdict.value)
        if "failed" in verdicts:
            verdict = "failed"
        elif all(v == "success" for v in verdicts):
            verdict = "success"
        else:
            verdict = next(v for v in verdicts if v != "success")
        unsettled = self.agent.verifier._not_settled(obs, stable, "mcp-L1")
        if unsettled is not None:
            verdict = "uncertain"
            evidence.append(unsettled.evidence)
        receipt = make_receipt("mcp_verify", goal or expect_text or "postconditions",
                               "verified_done" if verdict == "success" else verdict, level,
                               self.agent._scrub("; ".join(evidence)), obs, [],
                               {"postconditions": results, "stable": stable})
        receipt = self.agent.scrubber.scrub_obj(receipt)
        self.last = obs
        return {"content": [{"type": "text", "text": json.dumps(receipt, ensure_ascii=False, default=str)}],
                "structuredContent": receipt, "isError": receipt["verdict"] != "verified_done"}

    def run_task(self, task: str, demo: Optional[dict] = None, max_steps: Optional[int] = None,
                 notify=None) -> dict:
        from .config import build_agent
        from .logger import TrajectoryLogger
        llms = None
        if demo:
            from .scripted import ScriptedPolicy
            llms = ScriptedPolicy(demo).llms()
        else:
            spec = (self.cfg.get("models") or {}).get("planner") or {}
            import os
            key_ok = bool(spec.get("api_key")) or bool(os.environ.get(spec.get("api_key_env") or "", "")) or \
                str(spec.get("base_url") or "").startswith(("http://localhost", "http://127.0.0.1"))
            if not spec or not key_ok:
                return _err("no models configured (planner model or its API key missing); pass -c/-m with models "
                            "or give a scripted 'demo'")
        if max_steps:
            self.cfg.setdefault("agent", {})["max_steps"] = int(max_steps)
        log = TrajectoryLogger(self.cfg.get("runs_dir", "runs"), save_images=False)
        # Privacy, rejected intents and held keys belong to the connection,
        # including across run_task / act calls on the same computer.
        log.scrubber = self.agent.scrubber
        if notify:
            log.subscribe(notify)
        agent = build_agent(self.cfg, self.env, log, llms=llms, ask_fn=lambda q: None)
        agent.guard.denied = self.agent.guard.denied
        agent.guard.uncertain = self.agent.guard.uncertain
        agent.guard.held = self.agent.guard.held
        agent.guard.tools = self.agent.guard.tools
        agent.hybrid.tools = self.agent.hybrid.tools
        if hasattr(agent.actor, "extra_docs") and agent.guard.tools is not None:
            agent.actor.extra_docs = agent.guard.tools.prompt_docs()
        try:
            res = agent.run(task)
        finally:
            log.close(report=False)
            self.last = None
        out = {"status": res.status, "claimed_done": res.claimed_done, "steps": res.steps,
               "message": res.message, "receipts": res.receipts, "modality": res.modality,
               "trajectory": str(log.dir)}
        out = agent.scrubber.scrub_obj(out)
        return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, default=str)}],
                "structuredContent": out, "isError": res.status != "done"}

    def remote(self, op: str, name: str = "default") -> dict:
        if getattr(self.env, "platform", "") != "remote":
            return _err(f"{op} needs the remote sandbox platform")
        fn = {"takeover": self.env.takeover, "handback": self.env.handback, "live_view": self.env.live_view,
              "snapshot": lambda: self.env.snapshot(name), "reset": lambda: self.env.reset(name) or {"ok": True}}[op]
        r = fn()
        if op in {"handback", "reset"}:
            self.last = None
        return {"content": [{"type": "text", "text": json.dumps(r, ensure_ascii=False)}], "structuredContent": r}


def _err(msg: str) -> dict:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


class Server:
    def __init__(self, session: Session, out=None):
        self.s = session
        self.out = out or sys.stdout
        self.wlock = threading.Lock()

    def send(self, msg: dict) -> None:
        with self.wlock:
            self.out.write(json.dumps(msg, ensure_ascii=False, default=str) + "\n")
            self.out.flush()

    def notify(self, method: str, params: dict) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def handle(self, msg: dict) -> Optional[dict]:
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if method is None:
            return None
        if mid is None:                                   # notification
            return None
        try:
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                          "capabilities": {"tools": {"listChanged": False}, "logging": {}},
                          "serverInfo": {"name": "gua", "version": __version__},
                          "instructions": "Call observe first. act executes and VERIFIES one action; use verify "
                                          "with expect_text/postconditions to prove completion."}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self.call(params.get("name"), params.get("arguments") or {})
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        except Exception as e:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            return {"jsonrpc": "2.0", "id": mid, "result": _err(f"internal error: {type(e).__name__}")}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def call(self, name: str, args: dict) -> dict:
        try:
            return self._call(name, args)
        except Exception as e:  # noqa: BLE001
            if "paused_for_human" in str(e):
                return _err("paused_for_human: a human has control of the sandbox; call handback first")
            raise

    def _call(self, name: str, args: dict) -> dict:
        s = self.s
        with s.lock:
            if name == "observe":
                return s.observe(bool(args.get("include_image", True)), int(args.get("max_elements", 80)))
            if name == "act":
                if not isinstance(args.get("action"), dict):
                    return _err("act needs an 'action' object")
                return s.act(args["action"])
            if name == "verify":
                return s.verify(str(args.get("expect_text") or ""), args.get("postconditions"),
                                str(args.get("goal") or ""), bool(args.get("mark", False)))
            if name == "run_task":
                def fwd(ev):
                    self.notify("notifications/message", {"level": "info", "logger": "gua.trajectory", "data": ev})
                return s.run_task(str(args.get("task") or ""), args.get("demo"), args.get("max_steps"), fwd)
            if name in {"takeover", "handback", "snapshot", "reset", "live_view"}:
                return s.remote(name, str(args.get("name") or "default"))
        return _err(f"unknown tool {name!r}")

    def serve(self, inp=None) -> None:
        inp = inp or sys.stdin
        for line in inp:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            msgs = msg if isinstance(msg, list) else [msg]
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                r = self.handle(m)
                if r is not None:
                    self.send(r)


def main(cfg: dict, allow_risky: bool = False) -> None:
    # 协议消息独占 stdout：把意外的 print 重定向到 stderr
    real_out = sys.stdout
    sys.stdout = sys.stderr
    try:
        session = Session(cfg, allow_risky=allow_risky)
        Server(session, out=real_out).serve(sys.stdin)
    finally:
        sys.stdout = real_out
        try:
            session.env.close()  # type: ignore[possibly-undefined]
        except Exception:  # noqa: BLE001
            pass
    time.sleep(0)

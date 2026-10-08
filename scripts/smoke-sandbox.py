"""Real Docker/GTK/AT-SPI and VNC takeover outcomes; no LLM credentials.

Build the image first, then run: python scripts/smoke-sandbox.py --image gua-sandbox
The disposable container publishes only loopback ports and is always removed.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gua.actions import Action  # noqa: E402
from gua.env.remote import RemoteEnv, RemoteError  # noqa: E402


def docker(*args, env=None):
    return subprocess.check_output(["docker", *args], text=True, env=env).strip()


def wait_for(fn, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("Sandbox outcome did not arrive before the deadline")


class WebSocket:
    """Binary frames for the loopback noVNC proxy; no direct RFB exposure."""
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.buffer = bytearray()
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        request = (f"GET /websockify HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                   "Sec-WebSocket-Protocol: binary\r\n\r\n")
        self.sock.sendall(request.encode())
        header = bytearray()
        while not header.endswith(b"\r\n\r\n") and len(header) < 16384:
            header.extend(self.raw(1))
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        assert header.startswith(b"HTTP/1.1 101") or header.startswith(b"HTTP/1.0 101"), header
        assert expected in header
    def raw(self, size):
        data = bytearray()
        while len(data) < size:
            part = self.sock.recv(size - len(data))
            if not part:
                raise EOFError("noVNC disconnected")
            data.extend(part)
        return bytes(data)
    def sendall(self, payload, opcode=2):
        length = len(payload)
        header = bytes([0x80 | opcode, 0x80 | length]) if length < 126 else \
            bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", length)
        mask = secrets.token_bytes(4)
        self.sock.sendall(header + mask + bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload)))
    def recv(self, size):
        while not self.buffer:
            first, second = self.raw(2)
            opcode, length = first & 15, second & 127
            if length == 126: length = struct.unpack(">H", self.raw(2))[0]
            elif length == 127: length = struct.unpack(">Q", self.raw(8))[0]
            assert length <= 8_000_000 and not second & 128
            payload = self.raw(length)
            if opcode == 8: raise EOFError("noVNC closed")
            if opcode == 9: self.sendall(payload, 10)
            elif opcode in {0, 2}: self.buffer.extend(payload)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result
    def close(self):
        self.sock.close()


class VNC:
    """Minimal RFB client; bypass UI query flags to check server enforcement."""
    def __init__(self, port):
        self.sock = WebSocket(port)
        assert self.read(12).startswith(b"RFB 003."), "No RFB server"
        self.sock.sendall(b"RFB 003.008\n")
        security = self.read(self.read(1)[0])
        assert 1 in security, "Loopback-only fixture requires RFB None auth"
        self.sock.sendall(b"\x01")
        assert self.read(4) == b"\0\0\0\0"
        self.sock.sendall(b"\x01")
        header = self.read(24)
        self.read(struct.unpack(">I", header[-4:])[0])
    def read(self, size):
        parts = bytearray()
        while len(parts) < size:
            chunk = self.sock.recv(size - len(parts))
            if not chunk:
                raise EOFError("VNC disconnected")
            parts.extend(chunk)
        return bytes(parts)
    def move(self, x, y):
        self.sock.sendall(struct.pack(">BBHH", 5, 0, x, y))
        time.sleep(0.2)
    def close(self):
        self.sock.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="gua-sandbox")
    args = parser.parse_args()
    token = secrets.token_urlsafe(24)
    control = secrets.token_urlsafe(24)
    child_env = dict(os.environ, GUA_SANDBOX_TOKEN=token, GUA_SANDBOX_CONTROL_TOKEN=control)
    cid = docker("run", "--detach", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                 "--pids-limit=512", "--memory=2g", "-e", "GUA_SANDBOX_TOKEN", "-e", "GUA_SANDBOX_CONTROL_TOKEN",
                 "-p", "127.0.0.1::8765", "-p", "127.0.0.1::6080",
                 args.image, env=child_env)
    vnc = None
    try:
        def port(internal):
            return int(docker("port", cid, str(internal) + "/tcp").rsplit(":", 1)[1])
        http_port, view_port = port(8765), port(6080)
        env = RemoteEnv(f"http://127.0.0.1:{http_port}", token, wait_for_handback=False)
        def healthy():
            try:
                return env.health().get("atspi")
            except (OSError, RemoteError):
                return False
        wait_for(healthy)
        assert env.launch(["gua-form"])["ok"]
        wait_for(lambda: any(e.name == "Save" for e in env.observe().elements))
        assert env.snapshot("clean")["ok"]
        routes = []
        pointer, foreground = env.pointer_position(), env.foreground_token()
        for name, method, text in [("Name", "set_value", "Alice 中文"), ("Subscribe", "toggle", None),
                                   ("Save", "invoke", None)]:
            obs = env.observe()
            element = next(e for e in obs.elements if e.name == name)
            action = env.bind_action(Action("invoke", element_id=element.id, method=method, text=text), obs)
            result = env.execute(action)
            assert result.ok and result.route == "atspi:" + method, result
            routes.append(result.route)
        actual = wait_for(lambda: env.read_file("result.json"))
        assert json.loads(actual) == {"name": "Alice 中文", "subscribe": True, "plan": "Free"}
        assert env.pointer_position() == pointer and env.foreground_token() == foreground
        assert not env.run_tool(Action("shell", command=["id"])).ok
        assert not env.run_tool(Action("file", method="write", path="../escape", text="no")).ok
        obs = env.observe()
        save = next(e for e in obs.elements if e.name == "Save")
        old = env.bind_action(Action("invoke", element_id=save.id, method="invoke"), obs)
        vnc = VNC(view_port)
        vnc.move(110, 120)
        assert env.pointer_position() == pointer, "VNC query flags bypassed view-only mode"
        assert env.takeover()["ok"]
        for path, payload in [("/launch", {"argv": ["gua-form"]}), ("/snapshot", {"name": "bad"}),
                              ("/reset", {"name": "clean"}), ("/files", {"method": "read", "path": "result.json"})]:
            assert env._req("POST", path, payload).get("_status") == 423, path
        with_image = env._req("GET", "/observe?elements=1&image=1")
        assert with_image.get("_status") == 423 and "image" not in with_image
        vnc.move(220, 230)
        wait_for(lambda: env.pointer_position() == (220, 230))
        try:
            env.handback()
            raise AssertionError("agent token handed control back to itself")
        except RemoteError:
            pass
        assert RemoteEnv(env.url, token, control_token=control).handback()["ok"]
        vnc.move(330, 340)
        assert env.pointer_position() == (220, 230), "Human control continued after hand-back"
        assert "stale_target" in env.execute(old).error
        env.reset("clean")
        wait_for(lambda: any(e.name == "Save" for e in env.observe().elements))
        assert env.read_file("result.json") == ""
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{view_port}/vnc.html", timeout=5) as response:
            assert response.status == 200
        print(json.dumps({"container": "non-root; cap-drop ALL; no-new-privileges", "routes": routes,
                          "outcome": json.loads(actual), "pointer_moves_background": 0,
                          "takeover": "actions/images blocked; RFB control enabled only during takeover",
                          "reset": "clean app and workdir restored", "llm_calls": 0}, ensure_ascii=False))
    except Exception:
        sys.stderr.write(docker("logs", cid)[-5000:] + "\n")
        raise
    finally:
        if vnc:
            vnc.close()
        docker("rm", "--force", cid)


if __name__ == "__main__":
    main()

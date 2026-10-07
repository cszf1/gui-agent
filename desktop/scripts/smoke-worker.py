"""Exercise the frozen worker and bundled Chromium without any paid model API."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

RESOURCES = Path(__file__).resolve().parents[1] / "resources"


def main():
    executable = RESOURCES / "gua-worker" / ("gua-worker.exe" if sys.platform == "win32" else "gua-worker")
    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(RESOURCES / "browsers"), "PYTHONIOENCODING": "utf-8"}
    with tempfile.TemporaryDirectory(prefix="gui-agent-smoke-") as directory:
        worker = subprocess.Popen([str(executable)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env)
        events = queue.Queue()
        def read():
            for line in worker.stdout:
                events.put(json.loads(line))
        threading.Thread(target=read, daemon=True).start()
        try:
            ready = events.get(timeout=30)
            assert ready["type"] == "ready", ready
            run_id = str(uuid.uuid4())
            worker.stdin.write(json.dumps({"command": "run", "runId": run_id, "demo": True,
                                           "task": "offline form demo", "runsRoot": directory,
                                           "headless": True, "settings": {}}) + "\n")
            worker.stdin.flush()
            previews = 0
            while True:
                event = events.get(timeout=45)
                if event["type"] == "preview":
                    previews += 1
                assert event["type"] != "error", event
                if event["type"] == "result":
                    assert event["result"]["status"] == "done", event
                    assert event["result"]["claimed_done"] is True
                    assert previews > 0
                    assert Path(event["report"]).is_file()
                    print("Frozen worker + bundled Chromium: offline task completed and verified.")
                    break
        finally:
            if worker.poll() is None:
                worker.stdin.write('{"command":"shutdown"}\n')
                worker.stdin.flush()
                try:
                    worker.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.wait()


if __name__ == "__main__":
    main()

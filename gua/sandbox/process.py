"""Bounded argv execution shared by local tools and the standalone daemon."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading


def run_bounded(argv, *, cwd, env, timeout=20.0, max_output=8000, limits=False):
    budget = max(0, int(max_output))
    command = list(argv)
    if limits and os.name == "posix" and not getattr(sys, "frozen", False):
        # No preexec_fn: parallel evaluations may launch from Python threads.
        command = [sys.executable, __file__, "--limited-exec", str(timeout), *command]
    process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=os.name == "posix")
    buffers = [bytearray(), bytearray()]
    totals = [0, 0]
    def drain(stream, index):
        with stream:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                totals[index] += len(chunk)
                buffers[index].extend(chunk[:max(0, budget * 4 - len(buffers[index]))])
    readers = [threading.Thread(target=drain, args=(stream, i), daemon=True)
               for i, stream in enumerate((process.stdout, process.stderr))]
    for reader in readers:
        reader.start()
    expired = False
    try:
        process.wait(timeout=max(0.01, timeout))
    except subprocess.TimeoutExpired:
        expired = True
    finally:
        if os.name == "posix":
            # A timeout must kill descendants too. Kill remaining members on
            # normal completion as well, before they can keep pipe readers alive.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif expired:  # pragma: no cover - Windows CI exercises ordinary argv execution
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=1)
    if expired:
        raise subprocess.TimeoutExpired(argv, timeout)
    values = []
    for buf, total in zip(buffers, totals):
        value = bytes(buf).decode("utf-8", "replace")[:budget]
        if total > len(buf) or len(bytes(buf).decode("utf-8", "replace")) > budget:
            value += "\n...[truncated]"
        values.append(value)
    return subprocess.CompletedProcess(argv, process.returncode, *values)


if __name__ == "__main__":
    if len(sys.argv) < 4 or sys.argv[1] != "--limited-exec":
        raise SystemExit("internal argv executor")
    import resource
    for key, value in ((resource.RLIMIT_CPU, int(float(sys.argv[2])) + 1),
                       (resource.RLIMIT_FSIZE, 50 * 1024 * 1024),
                       (resource.RLIMIT_AS, 2 * 1024 * 1024 * 1024)):
        resource.setrlimit(key, (value, value))
    os.execvpe(sys.argv[3], sys.argv[3:], os.environ)

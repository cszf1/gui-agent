"""Installer cleanup of this application's own processes and directories only."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .app_storage import DATA_MARKER, is_link, remove_owned
from .windows_app import default_data, existing_command, program_root


def cleanup(uninstall=False):
    root = program_root()
    marker = root / ".gui-agent-program"
    if is_link(root) or is_link(marker) or not marker.is_file() or marker.read_text().strip() != "gui-agent-program-v1":
        raise ValueError("program ownership marker is missing")
    data, _ = default_data(root)
    # Stop the exact backend PID recorded by this data directory, never all Python/Edge processes.
    instance = data / "cache/instance.json"
    process = None
    if instance.is_file() and not is_link(instance):
        import psutil
        try:
            record = json.loads(instance.read_text())
            candidate = psutil.Process(record["pid"])
            if (candidate.create_time() == record.get("created") and "gua.windows_app" in candidate.cmdline()
                    and Path(candidate.exe()).parent == root / "runtime"):
                process = candidate
        except (ValueError, KeyError, OSError, psutil.Error): pass
    try: existing_command(data, "shutdown")
    except (OSError, ValueError, KeyError): pass
    if process:
        try: process.wait(timeout=20)
        except psutil.TimeoutExpired: raise RuntimeError("Please close GUI Agent before uninstalling") from None
    # Waiting for the backend, rather than the instance file, also waits for
    # worker/Edge shutdown and the final transient cleanup to finish.
    if uninstall and data.exists():
        if is_link(data): remove_owned(data)
        else:
            data_marker = data / ".gui-agent-data"
            if not data_marker.is_file() or is_link(data_marker) or data_marker.read_text().strip() != DATA_MARKER:
                raise ValueError("data ownership marker is missing")
            remove_owned(data)
    # NSIS runs after this process exits. Remove junctions first so its recursive
    # removal cannot cross into documents outside the installation directory.
    def remove_links(directory):
        for p in directory.iterdir():
            if is_link(p): remove_owned(p)
            elif p.is_dir(): remove_links(p)
    remove_links(root)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--uninstall", action="store_true")
    args = p.parse_args()
    cleanup(args.uninstall)


if __name__ == "__main__": main()

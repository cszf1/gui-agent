"""Local timing only; records fixed phase names, never inputs or screenshots."""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class Performance:
    phases: dict = field(default_factory=dict)
    routes: dict = field(default_factory=dict)

    @contextmanager
    def measure(self, phase):
        started = time.perf_counter()
        try:
            yield
        finally:
            item = self.phases.setdefault(phase, {"seconds": 0.0, "count": 0})
            item["seconds"] += time.perf_counter() - started
            item["count"] += 1

    def execution(self, route):
        key = route or "platform_input"
        self.routes[key] = self.routes.get(key, 0) + 1

    def summary(self):
        return {"phases": {k: {"seconds": round(v["seconds"], 4), "count": v["count"]}
                           for k, v in self.phases.items()}, "execution_routes": dict(self.routes)}

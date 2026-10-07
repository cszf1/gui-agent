"""域名白名单判定（SafetyGuard 与 WebEnv 共用，审查条目 9）。

按 **主机名**（不含端口）匹配：允许 d 本身及其子域名；file / about / data / blob 等非网络 scheme 始终允许；
白名单为空表示不限制。
"""
from __future__ import annotations

from typing import Iterable
from urllib.parse import urlparse

LOCAL_SCHEMES = {"file", "about", "data", "blob", "chrome-error", "javascript"}


def host_of(url: str) -> str:
    u = urlparse(url if "://" in url or url.startswith(("about:", "data:", "blob:", "javascript:")) else "https://" + url)
    return (u.hostname or "").lower().rstrip(".")


def domain_allowed(url: str, allowed: Iterable[str]) -> bool:
    allowed = [d.lower().strip().lstrip(".") for d in (allowed or []) if d and d.strip()]
    if not allowed:
        return True
    raw = (url or "").strip()
    u = urlparse(raw if "://" in raw or raw.startswith(("about:", "data:", "blob:", "javascript:")) else "https://" + raw)
    if u.scheme in LOCAL_SCHEMES:
        return u.scheme != "javascript"      # javascript: URL 不是导航目标，一律不放行
    h = (u.hostname or "").lower().rstrip(".")
    if not h:
        return False
    return any(h == d or h.endswith("." + d) for d in allowed)

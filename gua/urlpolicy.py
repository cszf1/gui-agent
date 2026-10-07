"""URL 策略：严格规范化 + 域名白名单（SafetyGuard 与 WebEnv 共用）。

第三轮审查条目 1：白名单检查、浏览器层路由拦截、真正发出的请求必须使用**同一个规范化 URL**。
不再用 ``replace("\\\\", "/")`` 之类的近似手段假装与 WHATWG 一致——含歧义（反斜杠、控制 / 空白字符、
userinfo、百分号 authority、数字 / 十六进制 IP 别名）的网络 URL 一律 **拒绝**，而不是"先替换再检查"。

- 网络 scheme 只认 http / https；主机必须是合法 ASCII 域名（含子域）、正规点分十进制 IPv4，或带方括号的 [IPv6]。
- 非网络 scheme（file / about / data / blob / chrome-error）保持原有语义：始终允许，不受白名单限制。
  ``javascript:`` 永远不是合法导航目标。
- 白名单按主机名匹配：``d`` 本身及其子域；白名单为空表示不限制。

解析失败时 ``domain_allowed`` 返回 False（fail-closed），``host_of`` 返回 ""。
"""
from __future__ import annotations

import ipaddress
import re
from typing import Iterable, Optional
from urllib.parse import SplitResult, urlsplit, urlunsplit

# 非网络 scheme：始终允许（file 本地任务 / about:blank 等既有语义）
LOCAL_SCHEMES = {"file", "about", "data", "blob", "chrome-error"}
NETWORK_SCHEMES = {"http", "https"}

# 控制字符 / 空白 / DEL / 反斜杠：一旦出现即拒绝（绝不替换后继续）
_BAD_CHARS = re.compile("[\x00-\x20\x7f\\\\]")
# 单个 DNS label（ASCII，不以 - 开头结尾，1..63 字符）
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# 数字 / 十六进制整数写法（数字 IP 别名的一部分）
_NUMERICISH = re.compile(r"^(?:0[xX][0-9a-fA-F]+|[0-9]+)$")
_MAX_HOST_LEN = 253


class UrlRejected(ValueError):
    """URL 含歧义或无法安全解释（审查条目 1）。"""


def _split(url: str) -> SplitResult:
    """切分 URL（无 scheme 时按 https:// 处理）。空白 / 反斜杠直接拒绝，不做替换。"""
    if not isinstance(url, str) or not url:
        raise UrlRejected("empty url")
    if _BAD_CHARS.search(url):
        raise UrlRejected("url contains control / whitespace / backslash characters")
    try:
        s = urlsplit(url)
        if s.scheme:
            return s
        return urlsplit("https://" + url)
    except ValueError as e:                      # 非法方括号 IPv6 等
        raise UrlRejected(f"invalid url: {e}") from None


def _host_port(s: SplitResult) -> tuple[str, Optional[int]]:
    """校验并返回 (小写 host, port)；歧义 / 非法时抛 UrlRejected。"""
    netloc = s.netloc
    if not netloc:
        raise UrlRejected("network url without host")
    if "@" in netloc:
        raise UrlRejected("userinfo in url is not allowed")
    if "%" in netloc:
        raise UrlRejected("percent-encoded authority is ambiguous")
    try:
        host = s.hostname
        port = s.port
    except ValueError as e:                      # 非法方括号 IPv6 / 非法端口
        raise UrlRejected(f"invalid host or port: {e}") from None
    if not host:
        raise UrlRejected("network url without host")
    host = host.lower().rstrip(".")
    if not host:
        raise UrlRejected("empty host")
    _validate_host(host, netloc)
    return host, port


def _validate_host(host: str, netloc: str) -> None:
    if "[" in netloc or "]" in netloc:          # [IPv6]（hostname 已去掉方括号）
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise UrlRejected(f"invalid IPv6 host {host!r}") from None
        return
    if not host.isascii():
        raise UrlRejected("non-ASCII host (use punycode)")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if ip.version == 6:                     # 不带方括号的 IPv6 与端口有歧义
            raise UrlRejected("IPv6 host must be bracketed")
        return                                  # 正规点分十进制 IPv4
    labels = host.split(".")
    if all(_NUMERICISH.match(lab) for lab in labels):
        raise UrlRejected(f"numeric / hex host alias {host!r} is ambiguous")
    if len(host) > _MAX_HOST_LEN or len(labels) > 127:
        raise UrlRejected("host too long")
    for lab in labels:
        if not _LABEL.fullmatch(lab):
            raise UrlRejected(f"invalid host label {lab!r}")


def normalize(url: str, *, allow_local: bool = True) -> str:
    """返回可安全用于比较与发请求的规范化 URL；非法 / 歧义则抛 UrlRejected。

    规范化：scheme 与 host 小写；网络 URL 强制带路径 "/"。allow_local=False 时只接受 http/https
    （用于 Location 重定向：绝不接受 javascript: 等可执行 scheme）。
    """
    s = _split(url)
    scheme = s.scheme.lower()
    if scheme in NETWORK_SCHEMES:
        host, port = _host_port(s)
        netloc = f"[{host}]" if ":" in host else host
        if port is not None:
            netloc = f"{netloc}:{port}"
        return urlunsplit((scheme, netloc, s.path or "/", s.query, s.fragment))
    if allow_local and scheme in LOCAL_SCHEMES:
        return urlunsplit((scheme, s.netloc, s.path, s.query, s.fragment))
    raise UrlRejected(f"scheme {scheme!r} is not an allowed navigation scheme")


def host_of(url: str) -> str:
    """主机名（不含端口，小写）；非网络 scheme 或非法 URL 返回 ""。"""
    try:
        s = _split(url)
        if s.scheme.lower() in NETWORK_SCHEMES:
            return _host_port(s)[0]
    except UrlRejected:
        pass
    return ""


def domain_allowed(url: str, allowed: Iterable[str]) -> bool:
    """白名单判定。无法安全解析的 URL → False（fail-closed）。白名单为空 → 不限制。"""
    allowed = [d.lower().strip().lstrip(".") for d in (allowed or []) if d and d.strip()]
    if not allowed:
        return True
    try:
        s = _split(url)
    except UrlRejected:
        return False
    scheme = s.scheme.lower()
    if scheme in LOCAL_SCHEMES:
        return True
    if scheme not in NETWORK_SCHEMES:
        return False                             # javascript: 等一律不放行
    try:
        h = _host_port(s)[0]
    except UrlRejected:
        return False
    return any(h == d or h.endswith("." + d) for d in allowed)

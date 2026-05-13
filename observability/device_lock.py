"""
设备锁：未授权客户端所有 HTTP 请求返回固定 PNG（无法操作看板/API）。

说明：HTTP 协议无法获取浏览器端网卡 MAC，因此以来源 IP 白名单为主；
可选 SHARK_ALLOWED_MAC：部分 API 请求须带 Header（同源 bootstrap 注入）。

Docker / 本机访问时，对端 IP 可能为 172.17.0.1、::ffff:127.0.0.1 等；设备锁开启后
默认只信任 SHARK_ALLOWED_IPS / SHARK_ALLOWED_CIDRS 中显式配置的来源。
确需局域网共享时再显式设置 SHARK_DEVICE_LOCK_ALLOW_PRIVATE=1。
"""

from __future__ import annotations

import ipaddress
import logging
import os
from pathlib import Path
from typing import FrozenSet, List, Optional, Set, Tuple, Union

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_log = logging.getLogger(__name__)

_DENY_BODY: bytes = b""
_LOCK_ENABLED: bool = False
_ALLOWED_IPS: FrozenSet[str] = frozenset()
_ALLOW_PRIVATE: bool = True
_TRUST_XFF: bool = False
_ALLOWED_CIDRS: Tuple[Union[ipaddress.IPv4Network, ipaddress.IPv6Network], ...] = ()


def _truthy(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


def _falsy(v: str) -> bool:
    return v.strip().lower() in ("0", "false", "no", "off", "")


def normalize_mac(raw: str) -> str:
    t = raw.strip().lower().replace("-", ":")
    parts = [p.zfill(2) for p in t.split(":") if p.strip()]
    return ":".join(parts)


def init_device_lock(repo_root: Path) -> None:
    """在启动时加载配置与拒绝用图片字节。"""
    global _DENY_BODY, _LOCK_ENABLED, _ALLOWED_IPS, _ALLOW_PRIVATE, _TRUST_XFF, _ALLOWED_MAC_NORM
    global _ALLOWED_CIDRS

    _LOCK_ENABLED = _truthy(os.environ.get("SHARK_DEVICE_LOCK", ""))
    _TRUST_XFF = _truthy(os.environ.get("SHARK_TRUST_X_FORWARDED_FOR", ""))
    raw_ap = os.environ.get("SHARK_DEVICE_LOCK_ALLOW_PRIVATE")
    if raw_ap is None or str(raw_ap).strip() == "":
        _ALLOW_PRIVATE = False
    else:
        _ALLOW_PRIVATE = not _falsy(str(raw_ap))

    ips = os.environ.get("SHARK_ALLOWED_IPS", "127.0.0.1,::1").strip()
    allowed: Set[str] = {x.strip() for x in ips.split(",") if x.strip()}
    if _LOCK_ENABLED and not allowed:
        allowed = {"127.0.0.1", "::1"}
    _ALLOWED_IPS = frozenset(allowed)

    nets: List[Union[ipaddress.IPv4Network, ipaddress.IPv6Network]] = []
    for part in os.environ.get("SHARK_ALLOWED_CIDRS", "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            _log.error("[device_lock] 忽略无效 SHARK_ALLOWED_CIDRS 段: %r", part)
    _ALLOWED_CIDRS = tuple(nets)

    mac = os.environ.get("SHARK_ALLOWED_MAC", "").strip()
    _ALLOWED_MAC_NORM = normalize_mac(mac) if mac else None
    if _LOCK_ENABLED and _ALLOWED_MAC_NORM:
        _parts = [x for x in _ALLOWED_MAC_NORM.split(":") if x]
        if len(_parts) != 6 or any(len(x) != 2 for x in _parts):
            _log.error(
                "[device_lock] SHARK_ALLOWED_MAC 应为 6 组十六进制（例 aa:bb:cc:dd:ee:ff），"
                "当前 raw=%r normalized=%r",
                mac,
                _ALLOWED_MAC_NORM,
            )

    _DENY_BODY = b""
    candidates = []
    custom = os.environ.get("SHARK_DEVICE_DENY_IMAGE", "").strip()
    if custom:
        candidates.append(Path(custom))
    candidates.append(repo_root / "static" / "device_denied.png")

    for p in candidates:
        try:
            if p.is_file():
                _DENY_BODY = p.read_bytes()
                break
        except OSError:
            continue

    if _LOCK_ENABLED:
        _log.warning(
            "[device_lock] 已启用 allow_private=%s trust_xff=%s allowed_ips=%s cidrs=%s mac_norm=%s deny_png=%dB",
            _ALLOW_PRIVATE,
            _TRUST_XFF,
            sorted(_ALLOWED_IPS),
            [str(x) for x in _ALLOWED_CIDRS],
            _ALLOWED_MAC_NORM or "off",
            len(_DENY_BODY),
        )


def _client_ip(request: Request) -> str:
    loopback_host = _request_loopback_host(request)
    if loopback_host:
        return loopback_host

    client = request.client
    peer = ""
    if client and client.host:
        peer = client.host
    else:
        scope = request.scope.get("client")
        if scope and len(scope) >= 1 and scope[0]:
            peer = str(scope[0])

    if _host_is_loopback(peer):
        return peer
    if _TRUST_XFF and _proxy_peer_trusted(peer):
        xff = request.headers.get("x-forwarded-for") or request.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
    return peer


def _normalize_host_ip(host: str) -> str:
    if host.startswith("::ffff:"):
        return host[7:]
    return host


def _host_is_loopback(host: str) -> bool:
    if not host:
        return False
    try:
        return ipaddress.ip_address(_normalize_host_ip(host)).is_loopback
    except ValueError:
        return False


def _host_header_loopback(host_header: str) -> Optional[str]:
    if not host_header:
        return None
    host = host_header.strip()
    if host.startswith("["):
        end = host.find("]")
        candidate = host[1:end] if end > 0 else host
    else:
        candidate = host.rsplit(":", 1)[0] if ":" in host else host
    if candidate.lower() == "localhost":
        return "127.0.0.1"
    return candidate if _host_is_loopback(candidate) else None


def _request_loopback_host(request: Request) -> Optional[str]:
    return _host_header_loopback(request.headers.get("host") or request.headers.get("Host") or "")


def _proxy_peer_trusted(host: str) -> bool:
    """只信任显式白名单代理写入的 XFF，避免直连客户端伪造 X-Forwarded-For。"""
    if not host:
        return False
    if host in _ALLOWED_IPS:
        return True
    h = _normalize_host_ip(host)
    if h != host and h in _ALLOWED_IPS:
        return True
    try:
        addr = ipaddress.ip_address(h)
    except ValueError:
        return False
    if str(addr) in _ALLOWED_IPS:
        return True
    return any(addr in net for net in _ALLOWED_CIDRS)


def _ip_allowed(host: str) -> bool:
    if not host:
        return False
    if host in _ALLOWED_IPS:
        return True
    h = _normalize_host_ip(host)
    if h != host and h in _ALLOWED_IPS:
        return True
    try:
        addr = ipaddress.ip_address(h)
    except ValueError:
        return False
    if str(addr) in _ALLOWED_IPS:
        return True
    for net in _ALLOWED_CIDRS:
        if addr in net:
            return True
    if _ALLOW_PRIVATE:
        if addr.is_loopback or addr.is_private or addr.is_link_local:
            return True
    return False


def _mac_from_request(request: Request) -> Optional[str]:
    h = request.headers.get("x-shark-client-mac") or request.headers.get("X-Shark-Client-Mac")
    if not h or not str(h).strip():
        return None
    return normalize_mac(str(h))


def _http_path_exempt_from_mac(path: str) -> bool:
    """首页与静态资源请求在浏览器里无法带自定义 Header；仅校验 IP；JS 执行后再对 API 带 MAC。"""
    if path in ("", "/"):
        return True
    if path == "/api/bootstrap.js" or path == "/api/health":
        return True
    # 只读接口：旧版内联脚本或外部工具可能只带 Bearer 不带 MAC；IP 已放行时不再强制 MAC
    if path == "/api/live/status" or path == "/api/evo/pending":
        return True
    if path == "/api/plans" or path == "/plans":
        return True
    if path.startswith("/assets/") or path.startswith("/public/") or path.startswith("/video/"):
        return True
    # 本地 vite 直连后端时模块入口
    if path.startswith("/src/"):
        return True
    return False


def http_request_check(request: Request) -> Tuple[bool, str]:
    if not _LOCK_ENABLED:
        return True, ""
    ip = _client_ip(request)
    if not _ip_allowed(ip):
        return False, (
            f"ip={ip!r} 未放行（allow_private={_ALLOW_PRIVATE}）。"
            f"若在 Docker/Cloudflare Tunnel 下日志里出现 172.64.x.x 等，请把该 IP 写入 SHARK_ALLOWED_IPS "
            f"或使用 SHARK_ALLOWED_CIDRS（见 .env.example）"
        )
    if _ALLOWED_MAC_NORM and not _http_path_exempt_from_mac(request.url.path):
        got = _mac_from_request(request)
        if got != _ALLOWED_MAC_NORM:
            return False, f"mac mismatch path={request.url.path!r} got={got!r} need={_ALLOWED_MAC_NORM!r}"
    return True, ""


def http_request_allowed(request: Request) -> bool:
    ok, _ = http_request_check(request)
    return ok


def deny_image_response() -> Response:
    return Response(
        content=_DENY_BODY if _DENY_BODY else b"",
        status_code=403,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


def bootstrap_client_mac_js_value() -> str:
    if not _LOCK_ENABLED or not _ALLOWED_MAC_NORM:
        return ""
    return _ALLOWED_MAC_NORM


def websocket_connection_allowed(
    client_host: str,
    headers: List[Tuple[bytes, bytes]],
    device_mac_query: Optional[str],
) -> Tuple[bool, str]:
    if not _LOCK_ENABLED:
        return True, ""
    host = client_host or ""
    hdr = {k.lower(): v for k, v in headers}
    hh = hdr.get(b"host")
    if hh:
        loopback_host = _host_header_loopback(hh.decode("latin-1", errors="replace"))
        if loopback_host:
            return True, ""
    if _host_is_loopback(host):
        return True, ""
    if _TRUST_XFF and _proxy_peer_trusted(host):
        xff = hdr.get(b"x-forwarded-for")
        if xff:
            host = xff.decode("latin-1", errors="replace").split(",")[0].strip()
    if not _ip_allowed(host):
        return False, f"ws ip={host!r} denied"
    if _ALLOWED_MAC_NORM:
        got: Optional[str] = None
        hdr = {k.lower(): v for k, v in headers}
        mh = hdr.get(b"x-shark-client-mac")
        if mh:
            got = normalize_mac(mh.decode("latin-1", errors="replace"))
        if got != _ALLOWED_MAC_NORM and device_mac_query and device_mac_query.strip():
            got = normalize_mac(device_mac_query)
        if got != _ALLOWED_MAC_NORM:
            return False, f"ws mac got={got!r} need={_ALLOWED_MAC_NORM!r}"
    return True, ""


class DeviceLockMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        ok, reason = http_request_check(request)
        if not ok:
            _log.warning("[device_lock] 拒绝 %s %s", request.url.path, reason)
            return deny_image_response()
        return await call_next(request)

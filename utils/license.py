"""
License 鉴权中间件（Redis 验证版）。

许可证: 随机生成的 64 位 hex token，存储在 Redis `shark:license:active`。
每次 POST/PUT/DELETE 请求，后端查询 Redis 验证 token 是否存在且未过期。

环境变量:
  SHARK_LICENSE_SECRET — 可选，用于 gen_license 脚本签名
  SHARK_LICENSE_ENABLED — 是否启用鉴权（默认 1）

生成: python scripts/gen_license.py --user shark --expiry 2026-12-31
      会自动将 license 写入 Redis

前端: 登录弹窗输入 license → 后端验证 → localStorage 保存
      每次 POST/PUT/DELETE 带 Header: X-Shark-License: <token>
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from pathlib import Path
from typing import Optional, Tuple

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_log = logging.getLogger(__name__)

_LOCK_ENABLED: bool = False
_CAT_BODY: bytes = b""
_REDIS_URL: str = "redis://redis:6379/0"


def _truthy(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


def init_license_middleware(repo_root: Path) -> None:
    """启动时加载猫图和配置。"""
    global _CAT_BODY, _LOCK_ENABLED, _REDIS_URL

    _LOCK_ENABLED = _truthy(os.environ.get("SHARK_LICENSE_ENABLED", "1"))
    _REDIS_URL = os.environ.get("SHARK_REDIS_URL", "redis://redis:6379/0")

    # 加载猫图
    candidates = []
    custom = os.environ.get("SHARK_LICENSE_DENY_IMAGE", "").strip()
    if custom:
        candidates.append(Path(custom))
    candidates.append(repo_root / "static" / "device_denied.png")

    for p in candidates:
        try:
            if p.is_file():
                _CAT_BODY = p.read_bytes()
                break
        except OSError:
            continue

    if _LOCK_ENABLED:
        _log.warning("[license] 已启用 cat_png=%dB redis=%s", len(_CAT_BODY), _REDIS_URL)


def generate_license_token() -> str:
    """生成一个随机 license token（64字符 hex）。"""
    return secrets.token_hex(32)


def _get_redis():
    """获取同步 Redis 连接。"""
    import redis as _redis
    return _redis.from_url(_REDIS_URL, decode_responses=True)


def store_license(token: str, user: str, expiry: str) -> bool:
    """将许可证存入 Redis。"""
    try:
        r = _get_redis()
        import json as _json
        payload = _json.dumps({"user": user, "exp": expiry, "created_at": int(time.time())})
        r.set("shark:license:active", token)
        r.set("shark:license:meta", payload)
        return True
    except Exception as e:
        _log.error("store license failed: %s", e)
        return False


def _verify_license_redis(token: str) -> Tuple[bool, str]:
    """在 Redis 中验证 license token。"""
    if not token or not token.strip():
        return False, "empty token"

    token = token.strip()

    try:
        r = _get_redis()
        stored = r.get("shark:license:active")
        if not stored:
            return False, "Redis 中无许可证"
        if not secrets.compare_digest(token, stored):
            return False, "许可证无效"

        # 检查过期
        meta_str = r.get("shark:license:meta")
        if meta_str:
            import json as _json
            meta = _json.loads(meta_str)
            exp_str = meta.get("exp", "")
            if exp_str:
                from datetime import datetime
                try:
                    exp_date = datetime.strptime(exp_str, "%Y-%m-%d")
                    if datetime.now() > exp_date:
                        return False, f"已过期 ({exp_str})"
                except ValueError:
                    pass

        return True, ""
    except Exception as e:
        _log.warning("license redis verify failed: %s", e)
        return False, "Redis 验证失败"


def cat_response() -> Response:
    """返回猫图 403。"""
    return Response(
        content=_CAT_BODY if _CAT_BODY else b"",
        status_code=403,
        media_type="image/png",
        headers={"Cache-Control": "no-store", "X-Shark-Auth": "denied"},
    )


def license_from_request(request: Request) -> Optional[str]:
    """从请求中提取许可证。"""
    h = request.headers.get("x-shark-license") or request.headers.get("X-Shark-License")
    if h:
        return str(h)
    q = request.query_params.get("license")
    if q:
        return str(q)
    return None


class LicenseMiddleware(BaseHTTPMiddleware):
    """许可证中间件：POST/PUT/DELETE 需要有效 license，否则返回猫图。"""

    async def dispatch(self, request: Request, call_next):
        if not _LOCK_ENABLED:
            return await call_next(request)

        method = str(request.method or "GET").upper()
        if method in ("GET", "HEAD", "OPTIONS"):
            return await call_next(request)

        # 登录接口和 license 检查接口豁免
        path = request.url.path
        if path in ("/api/license/login", "/api/license/check"):
            return await call_next(request)

        token = license_from_request(request)
        ok, reason = _verify_license_redis(token or "")
        if not ok:
            ip = ""
            if request.client and request.client.host:
                ip = request.client.host
            _log.warning("[license] 拒绝 %s ip=%s reason=%s", request.url.path, ip, reason)
            return cat_response()

        return await call_next(request)


# 兼容旧版导出
_verify_license = _verify_license_redis

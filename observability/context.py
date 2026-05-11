"""
HTTP / WebSocket 关联 ID（correlation / request id）与根日志配置。

- 请求头 `X-Request-ID` 若存在则透传（截断 128），否则服务端生成 UUID。
- 日志字段 `rid=...` 通过 ContextVar 注入 LogRecord，无请求上下文时为 `-`。
"""

from __future__ import annotations

import contextvars
import logging
import os
import sys
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_CTX: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id",
    default="-",
)


class RequestIdLogFilter(logging.Filter):
    """为每条日志补上 `request_id` 属性供 Formatter 使用。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = REQUEST_ID_CTX.get()
        return True


def configure_logging() -> None:
    """配置根 logger：单行、含 rid。进程入口调用一次即可。"""
    level_name = os.environ.get("SHARK_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s [rid=%(request_id)s] %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    handler.addFilter(RequestIdLogFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(handler)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """为每个 HTTP 请求注入 `REQUEST_ID_CTX`，并回写响应头。"""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        raw = request.headers.get("x-request-id") or request.headers.get("X-Request-ID")
        raw = raw.strip() if raw else ""
        rid = (raw if raw else str(uuid.uuid4()))[:128]
        token = REQUEST_ID_CTX.set(rid)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            REQUEST_ID_CTX.reset(token)

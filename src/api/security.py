"""
HTTP API hardening: secret redaction, optional bearer token, path display helpers.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

from fastapi import Header, HTTPException

# 永续常用格式：BASE/QUOTE（如 BTC/USDT），防异常长字符串与注入下游。
_SYMBOL_PAIR_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}/[A-Za-z0-9._-]{1,32}$")
_CANDLE_INTERVAL_RE = re.compile(r"^[0-9]+[mhdwMHDW]$")

# Keys whose string values must never appear in JSON API responses (nested dicts).
_REDACT_EXACT: frozenset[str] = frozenset(
    {
        "api_key",
        "api_secret",
        "llm_api_key",
    }
)


def redact_sensitive_config(obj: Any) -> Any:
    """Recursively redact known secret fields for public / browser-facing GET responses."""
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if lk in _REDACT_EXACT or lk.endswith("_secret"):
                out[k] = _redaction_placeholder(v)
            else:
                out[k] = redact_sensitive_config(v)
        return out
    if isinstance(obj, list):
        return [redact_sensitive_config(x) for x in obj]
    return obj


def _redaction_placeholder(v: Any) -> str:
    if v is None or v == "":
        return ""
    return "***"


def sanitize_dir_for_api(path: str) -> str:
    """Avoid leaking home directory / absolute paths in JSON (information disclosure)."""
    if not path or not str(path).strip():
        return ""
    base = os.path.basename(str(path).rstrip("/"))
    return base or ""


def get_api_bind_host() -> str:
    """Listen address: default loopback to avoid exposing the control plane on LAN."""
    return (os.environ.get("SHARK_API_HOST") or "127.0.0.1").strip() or "127.0.0.1"


def _expected_bearer_token() -> str:
    return (os.environ.get("SHARK_API_TOKEN") or "").strip()


def get_expected_api_token() -> str:
    """与 ``SHARK_API_TOKEN`` 一致；供 WebSocket query 等使用。"""
    return _expected_bearer_token()


def validate_perp_symbol(symbol: str) -> str:
    s = (symbol or "").strip()
    if not _SYMBOL_PAIR_RE.match(s):
        raise HTTPException(status_code=400, detail="invalid symbol")
    return s


def validate_candle_interval(interval: str) -> str:
    s = (interval or "").strip()
    if not _CANDLE_INTERVAL_RE.match(s):
        raise HTTPException(status_code=400, detail="invalid interval")
    return s


async def require_api_token_if_configured(authorization: Optional[str] = Header(None)) -> None:
    """
    When SHARK_API_TOKEN is set, require ``Authorization: Bearer <token>`` for protected routes.
    When unset, no auth (rely on bind address + firewall).
    """
    expected = _expected_bearer_token()
    if not expected:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    got = authorization[7:].strip()
    if got != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

"""API 安全辅助：脱敏与路径展示。"""
import pytest

from src.api.security import redact_sensitive_config, sanitize_dir_for_api, validate_perp_symbol
from fastapi import HTTPException


def test_redact_masks_exchange_and_llm_keys():
    raw = {
        "exchange": {"api_key": "k", "api_secret": "s", "sandbox_mode": False},
        "darwin": {"llm_api_key": "lk", "batch_size": 3},
        "strategy": {"symbols": ["BTC/USDT"]},
    }
    out = redact_sensitive_config(raw)
    assert out["exchange"]["api_key"] == "***"
    assert out["exchange"]["api_secret"] == "***"
    assert out["darwin"]["llm_api_key"] == "***"
    assert out["darwin"]["batch_size"] == 3
    assert out["strategy"]["symbols"] == ["BTC/USDT"]


def test_sanitize_dir_for_api_no_absolute_leak():
    assert sanitize_dir_for_api("/Users/x/darwin_autopsy") == "darwin_autopsy"
    assert sanitize_dir_for_api("") == ""


def test_validate_perp_symbol_ok():
    assert validate_perp_symbol("BTC/USDT") == "BTC/USDT"


def test_validate_perp_symbol_rejects_injection():
    with pytest.raises(HTTPException) as ei:
        validate_perp_symbol("../../etc/passwd")
    assert ei.value.status_code == 400

"""
微观游击刺客：单标的互斥（与 risk_engine + 纸面会话一致）。
"""

from __future__ import annotations

from typing import Optional


def assassin_entry_blocked(symbol: str, now: Optional[float] = None) -> bool:
    """已有持仓则禁止新开刺客仓。"""
    from src.core.risk_engine import risk_engine

    return risk_engine.entry_mutex_reason(symbol, False) is not None


def arm_assassin_cooldown(symbol: str, seconds: float) -> None:
    """兼容旧调用名：post_flat 冷静期已移除，此函数为空操作。"""
    del symbol, seconds


def clear_assassin_cooldown_for_tests(symbol: str) -> None:
    """兼容单测：已无冷静期状态可清。"""
    del symbol

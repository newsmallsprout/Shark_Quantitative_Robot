from __future__ import annotations

from enum import Enum


class Side(str, Enum):
    """永续 / 合约方向（与 `main.py` StrategyRunner 一致）。"""

    LONG = "long"
    SHORT = "short"


class OrderStatus(str, Enum):
    """
    订单生命周期（纸面/实盘统一抽象；纸面成交可瞬时 FILLED）。

    terminal: FILLED, CANCELLED, REJECTED
    """

    CREATED = "created"
    SUBMITTED = "submitted"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .enums import OrderStatus, Side

OrderId = str  # 将来可改为 NewType("OrderId", str)


@dataclass(frozen=True)
class TradeOrder:
    """
    单笔交易指令快照（不可变值对象）。

    不承载 HTTP 字段；与 `engine.paper_engine.Order` 并存，逐步收敛时以本类型为准。
    """

    order_id: OrderId
    symbol: str
    side: Side
    status: OrderStatus
    size: float
    entry_price: Optional[float] = None
    leverage: int = 1
    idempotency_key: Optional[str] = None

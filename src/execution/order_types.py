"""
订单状态机与意图类型 — 与 docs/Quant_AI_Architecture_Upgrade_Guide.md 第二节对齐。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from dataclasses import asdict
from enum import Enum
from typing import Any, Dict, Optional, TYPE_CHECKING, Union
import uuid

if TYPE_CHECKING:
    from src.core.events import SignalEvent


class OrderState(str, Enum):
    PENDING_CREATE = "PENDING_CREATE"
    SUBMITTED = "SUBMITTED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELING = "CANCELING"
    CANCELED = "CANCELED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass
class OrderIntent:
    """
    策略只提交意图；执行细节（TTL、撤单）由 OrderManager 负责。
    """

    symbol: str
    side: str
    order_type: str
    amount: float
    price: Optional[float] = None
    leverage: int = 1
    notional_size: float = 0.0
    """动态 sizing 后的目标名义价值（USDT）；0 表示未计算/走信号原值。"""
    margin_amount: float = 0.0
    """动态 sizing 后预计占用保证金（USDT）。"""
    margin_mode: str = "isolated"
    reduce_only: bool = False
    post_only: bool = False
    berserker: bool = False
    entry_context: Dict[str, Any] = field(default_factory=dict)

    is_high_conviction: bool = False
    """高置信度贪婪单（特许市价 + 追踪止损让利润奔跑）。"""
    trailing_stop_activation_pct: float = 0.0
    """权益 ROE（浮盈/保证金）≥ 该阈值后激活按价回撤止损；与纸面 positions 一致。"""
    trailing_stop_callback_pct: float = 0.0
    """相对最有利极价的价格回撤比例（0.005=0.5%），触发市价平仓。"""

    intent_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ttl_ms: int = 0
    """限价单存活时间；0 表示不启用 TTL 撤单。"""
    max_slippage_bps: Optional[float] = None
    """超过则触发防御性撤单（相对成交价/挂价）。"""
    max_deviation_atr_mult: Optional[float] = None
    """价格偏离参考价超过 ATR×系数 时撤单；需 OrderManager 侧能取 ATR。"""

    @classmethod
    def from_signal(cls, signal: "Union[SignalEvent, Any]", *, ttl_ms: int = 0) -> "OrderIntent":
        ect = dict(getattr(signal, "entry_context", None) or {})
        return cls(
            symbol=getattr(signal, "symbol", ""),
            side=getattr(signal, "side", "buy"),
            order_type=str(getattr(signal, "order_type", "market")),
            amount=float(getattr(signal, "amount", 0) or 0),
            price=getattr(signal, "price", None),
            # Do not hide a default leverage here. Leverage must be decided by Playbook/Sizing.
            leverage=int(getattr(signal, "leverage", 1)),
            notional_size=float(getattr(signal, "notional_size", 0.0) or 0.0),
            margin_amount=float(getattr(signal, "margin_amount", 0.0) or 0.0),
            margin_mode=str(getattr(signal, "margin_mode", "isolated") or "isolated"),
            reduce_only=bool(getattr(signal, "reduce_only", False)),
            post_only=bool(getattr(signal, "post_only", False)),
            berserker=bool(getattr(signal, "berserker", False)),
            entry_context=ect,
            ttl_ms=int(ttl_ms or 0),
            is_high_conviction=bool(getattr(signal, "is_high_conviction", False)),
            trailing_stop_activation_pct=float(
                getattr(signal, "trailing_stop_activation_pct", 0.0) or 0.0
            ),
            trailing_stop_callback_pct=float(
                getattr(signal, "trailing_stop_callback_pct", 0.0) or 0.0
            ),
        )


@dataclass
class PairLegIntent:
    symbol: str
    side: str
    order_type: str
    amount: float
    price: Optional[float] = None
    leverage: int = 1
    margin_mode: str = "isolated"
    reduce_only: bool = False
    post_only: bool = False
    berserker: bool = False
    entry_context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PairOrderIntent:
    pair_id: str
    strategy_name: str
    alt_leg: PairLegIntent
    anchor_leg: PairLegIntent
    panic_close_on_partial: bool = True
    maker_grace_ms: int = 120

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PairOrderIntent":
        raw = dict(data or {})
        return cls(
            pair_id=str(raw.get("pair_id") or ""),
            strategy_name=str(raw.get("strategy_name") or ""),
            alt_leg=PairLegIntent(**dict(raw.get("alt_leg") or {})),
            anchor_leg=PairLegIntent(**dict(raw.get("anchor_leg") or {})),
            panic_close_on_partial=bool(raw.get("panic_close_on_partial", True)),
            maker_grace_ms=int(raw.get("maker_grace_ms", 120) or 120),
        )


@dataclass
class OrderRecord:
    """本地与交易所对账用的订单视图。"""

    intent_id: str
    client_order_id: str
    symbol: str
    side: str
    state: OrderState = OrderState.PENDING_CREATE
    exchange_order_id: Optional[str] = None
    price: Optional[float] = None
    amount: float = 0.0
    filled: float = 0.0
    created_ts: float = 0.0
    last_sync_ts: float = 0.0
    raw: Dict[str, Any] = field(default_factory=dict)

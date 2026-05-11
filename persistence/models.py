"""
SQLAlchemy 2.x 模型：Order（开仓）、Trade（平仓）、BalanceLog（资金变动）。

与 main.StrategyRunner 纸面交易语义对齐；资金相关字段使用 Numeric 保持精度。
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class OrderStatus(str, enum.Enum):
    open = "open"
    closed = "closed"
    cancelled = "cancelled"


class Base(DeclarativeBase):
    pass


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=OrderStatus.open.value, index=True)

    entry_price: Mapped[float] = mapped_column(Numeric(24, 10), nullable=False)
    size: Mapped[float] = mapped_column(Numeric(24, 10), nullable=False)
    margin_usd: Mapped[float] = mapped_column(Numeric(24, 10), nullable=False)
    leverage: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    fee_open_usd: Mapped[float] = mapped_column(Numeric(24, 10), default=0)

    opened_at: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    closed_at: Mapped[Optional[float]] = mapped_column(Numeric(20, 6), nullable=True)

    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    trades: Mapped[List["Trade"]] = relationship("Trade", back_populates="order")


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)

    entry_price: Mapped[float] = mapped_column(Numeric(24, 10), nullable=False)
    exit_price: Mapped[float] = mapped_column(Numeric(24, 10), nullable=False)
    size: Mapped[float] = mapped_column(Numeric(24, 10), nullable=False)
    leverage: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    margin_usd: Mapped[float] = mapped_column(Numeric(24, 10), nullable=False)

    gross_pnl_usd: Mapped[float] = mapped_column(Numeric(24, 10), nullable=False)
    fee_open_usd: Mapped[float] = mapped_column(Numeric(24, 10), default=0)
    fee_close_usd: Mapped[float] = mapped_column(Numeric(24, 10), default=0)
    realized_pnl_usd: Mapped[float] = mapped_column(Numeric(24, 10), nullable=False)
    pnl_pct: Mapped[float] = mapped_column(Numeric(14, 6), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    opened_at: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    closed_at: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)

    extra: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    order: Mapped["Order"] = relationship("Order", back_populates="trades")


class BalanceLog(Base):
    __tablename__ = "balance_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )

    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    symbol: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    delta_free_cash: Mapped[float] = mapped_column(Numeric(24, 10), nullable=False)
    free_cash_after: Mapped[float] = mapped_column(Numeric(24, 10), nullable=False)
    total_balance_after: Mapped[float] = mapped_column(Numeric(24, 10), nullable=False)
    equity_after: Mapped[float] = mapped_column(Numeric(24, 10), nullable=False)
    margin_locked_after: Mapped[float] = mapped_column(Numeric(24, 10), nullable=False)
    unrealized_after: Mapped[float] = mapped_column(Numeric(24, 10), default=0)

    order_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True
    )
    trade_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trades.id", ondelete="SET NULL"), nullable=True, index=True
    )

    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)


class DialogueLine(Base):
    """台词弹药库：按场景 category 存一句一线，供随机抽取。"""

    __tablename__ = "dialogue_lines"
    __table_args__ = (
        UniqueConstraint("category", "line", name="uq_dialogue_lines_category_line"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    line: Mapped[str] = mapped_column(String(64), nullable=False)

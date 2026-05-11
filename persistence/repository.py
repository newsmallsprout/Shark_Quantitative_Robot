"""Postgres 仓储：开仓 / 平仓 / 资金流水写入。"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Mapping, Optional

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from persistence.models import BalanceLog, Order, OrderStatus, Trade


class AccountRepository:
    """资金相关写入路径使用同一 session 保证事务一致。"""

    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._sf = session_factory

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sf() as s:
            yield s

    async def insert_open_order(
        self,
        session: AsyncSession,
        *,
        order_id: uuid.UUID,
        symbol: str,
        side: str,
        entry_price: float,
        size: float,
        margin_usd: float,
        leverage: float,
        fee_open_usd: float,
        opened_ts: float,
        meta: Optional[Mapping[str, Any]] = None,
    ) -> Order:
        row = Order(
            id=order_id,
            symbol=symbol,
            side=side,
            status=OrderStatus.open.value,
            entry_price=entry_price,
            size=size,
            margin_usd=margin_usd,
            leverage=leverage,
            fee_open_usd=fee_open_usd,
            opened_at=opened_ts,
            meta=dict(meta) if meta is not None else None,
        )
        session.add(row)
        await session.flush()
        return row

    async def insert_balance_log(
        self,
        session: AsyncSession,
        *,
        event_type: str,
        delta_free_cash: float,
        free_cash_after: float,
        total_balance_after: float,
        equity_after: float,
        margin_locked_after: float,
        unrealized_after: float,
        symbol: Optional[str] = None,
        note: Optional[str] = None,
        order_id: Optional[uuid.UUID] = None,
        trade_id: Optional[uuid.UUID] = None,
        meta: Optional[Mapping[str, Any]] = None,
    ) -> BalanceLog:
        row = BalanceLog(
            event_type=event_type,
            symbol=symbol,
            note=note,
            delta_free_cash=delta_free_cash,
            free_cash_after=free_cash_after,
            total_balance_after=total_balance_after,
            equity_after=equity_after,
            margin_locked_after=margin_locked_after,
            unrealized_after=unrealized_after,
            order_id=order_id,
            trade_id=trade_id,
            meta=dict(meta) if meta is not None else None,
        )
        session.add(row)
        await session.flush()
        return row

    async def close_order(
        self,
        session: AsyncSession,
        *,
        order_id: uuid.UUID,
        closed_ts: float,
    ) -> None:
        await session.execute(
            update(Order)
            .where(Order.id == order_id)
            .values(status=OrderStatus.closed.value, closed_at=closed_ts)
        )

    async def insert_close_trade(
        self,
        session: AsyncSession,
        *,
        trade_id: uuid.UUID,
        order_id: uuid.UUID,
        symbol: str,
        side: str,
        entry_price: float,
        exit_price: float,
        size: float,
        leverage: float,
        margin_usd: float,
        gross_pnl_usd: float,
        fee_open_usd: float,
        fee_close_usd: float,
        realized_pnl_usd: float,
        pnl_pct: float,
        reason: str,
        opened_ts: float,
        closed_ts: float,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> Trade:
        row = Trade(
            id=trade_id,
            order_id=order_id,
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            exit_price=exit_price,
            size=size,
            leverage=leverage,
            margin_usd=margin_usd,
            gross_pnl_usd=gross_pnl_usd,
            fee_open_usd=fee_open_usd,
            fee_close_usd=fee_close_usd,
            realized_pnl_usd=realized_pnl_usd,
            pnl_pct=pnl_pct,
            reason=reason,
            opened_at=opened_ts,
            closed_at=closed_ts,
            extra=dict(extra) if extra is not None else None,
        )
        session.add(row)
        await session.flush()
        return row

    async def persist_open(
        self,
        *,
        order_id: uuid.UUID,
        symbol: str,
        side: str,
        entry_price: float,
        size: float,
        margin_usd: float,
        leverage: float,
        fee_open_usd: float,
        opened_ts: float,
        snap: Mapping[str, float],
        meta: Optional[Mapping[str, Any]] = None,
    ) -> None:
        delta = -(float(margin_usd) + float(fee_open_usd))
        async with self._sf() as session:
            async with session.begin():
                await self.insert_open_order(
                    session,
                    order_id=order_id,
                    symbol=symbol,
                    side=side,
                    entry_price=entry_price,
                    size=size,
                    margin_usd=margin_usd,
                    leverage=leverage,
                    fee_open_usd=fee_open_usd,
                    opened_ts=opened_ts,
                    meta=meta,
                )
                await self.insert_balance_log(
                    session,
                    event_type="open",
                    delta_free_cash=delta,
                    free_cash_after=float(snap["free_cash"]),
                    total_balance_after=float(snap["total_balance"]),
                    equity_after=float(snap["equity"]),
                    margin_locked_after=float(snap["margin_locked"]),
                    unrealized_after=float(snap["unrealized"]),
                    symbol=symbol,
                    note="open_position",
                    order_id=order_id,
                    meta={"margin": margin_usd, "fee_open": fee_open_usd},
                )

    async def persist_close(
        self,
        *,
        trade_id: uuid.UUID,
        order_id: uuid.UUID,
        symbol: str,
        side: str,
        entry_price: float,
        exit_price: float,
        size: float,
        leverage: float,
        margin_usd: float,
        gross_pnl_usd: float,
        fee_open_usd: float,
        fee_close_usd: float,
        realized_pnl_usd: float,
        pnl_pct: float,
        reason: str,
        opened_ts: float,
        closed_ts: float,
        prev_free_cash: float,
        snap: Mapping[str, float],
    ) -> None:
        delta = float(snap["free_cash"]) - float(prev_free_cash)
        async with self._sf() as session:
            async with session.begin():
                await self.insert_close_trade(
                    session,
                    trade_id=trade_id,
                    order_id=order_id,
                    symbol=symbol,
                    side=side,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    size=size,
                    leverage=leverage,
                    margin_usd=margin_usd,
                    gross_pnl_usd=gross_pnl_usd,
                    fee_open_usd=fee_open_usd,
                    fee_close_usd=fee_close_usd,
                    realized_pnl_usd=realized_pnl_usd,
                    pnl_pct=pnl_pct,
                    reason=reason,
                    opened_ts=opened_ts,
                    closed_ts=closed_ts,
                    extra=None,
                )
                await self.close_order(session, order_id=order_id, closed_ts=closed_ts)
                await self.insert_balance_log(
                    session,
                    event_type="close",
                    delta_free_cash=delta,
                    free_cash_after=float(snap["free_cash"]),
                    total_balance_after=float(snap["total_balance"]),
                    equity_after=float(snap["equity"]),
                    margin_locked_after=float(snap["margin_locked"]),
                    unrealized_after=float(snap["unrealized"]),
                    symbol=symbol,
                    note=reason[:512],
                    order_id=order_id,
                    trade_id=trade_id,
                    meta={"realized": realized_pnl_usd},
                )

    async def persist_balance_adjustment(
        self,
        *,
        event_type: str,
        delta_free_cash: float,
        snap: Mapping[str, float],
        symbol: Optional[str] = None,
        note: Optional[str] = None,
        order_id: Optional[uuid.UUID] = None,
        meta: Optional[Mapping[str, Any]] = None,
    ) -> None:
        async with self._sf() as session:
            async with session.begin():
                await self.insert_balance_log(
                    session,
                    event_type=event_type,
                    delta_free_cash=delta_free_cash,
                    free_cash_after=float(snap["free_cash"]),
                    total_balance_after=float(snap["total_balance"]),
                    equity_after=float(snap["equity"]),
                    margin_locked_after=float(snap["margin_locked"]),
                    unrealized_after=float(snap["unrealized"]),
                    symbol=symbol,
                    note=note,
                    order_id=order_id,
                    trade_id=None,
                    meta=meta,
                )

"""协程调度、Redis 状态缓存与异步持久化入口。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Mapping, Optional

import redis.asyncio as redis

from persistence.repository import AccountRepository

_log = logging.getLogger(__name__)

REDIS_STATE_KEY = "shark:state:summary"


def _json_safe(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float):
        return obj
    if isinstance(obj, (str, int, bool)) or obj is None:
        return obj
    return str(obj)


class PersistenceBridge:
    """
    内存 _state 之外：Postgres 持久化（订单/成交/资金流水）+ Redis 汇总缓存。
    在策略循环内用 create_task 异步写入，失败只记日志不抛回交易逻辑。
    """

    def __init__(
        self,
        repository: Optional[AccountRepository] = None,
        redis_client: Optional[redis.Redis] = None,
        *,
        redis_state_ttl_sec: int = 300,
        redis_state_min_interval: float = 0.4,
    ) -> None:
        self.repository = repository
        self.redis = redis_client
        self._redis_state_ttl = redis_state_ttl_sec
        self._redis_min_interval = redis_state_min_interval
        self._last_redis_push = 0.0
        self._redis_task: Optional[asyncio.Task] = None

    def enabled_db(self) -> bool:
        return self.repository is not None

    def enabled_redis(self) -> bool:
        return self.redis is not None

    def schedule_coro(self, coro_fn: Callable[[], Awaitable[None]]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _log.warning("persistence: no running loop; skipped async task")
            return

        async def _wrap() -> None:
            try:
                await coro_fn()
            except Exception:
                _log.exception("persistence async task failed")

        loop.create_task(_wrap())

    def on_position_open(
        self,
        runner: Any,
        prices: Mapping[str, float],
        *,
        order_id: uuid.UUID,
        sym: str,
        side: str,
        entry_price: float,
        size: float,
        margin: float,
        lev: float,
        fee: float,
        opened_ts: float,
    ) -> None:
        if not self.repository:
            return
        snap = runner._fund_snapshot(prices)

        async def _go() -> None:
            await self.repository.persist_open(
                order_id=order_id,
                symbol=sym,
                side=side,
                entry_price=entry_price,
                size=size,
                margin_usd=margin,
                leverage=lev,
                fee_open_usd=fee,
                opened_ts=opened_ts,
                snap=snap,
                meta={"signal": "paper"},
            )

        self.schedule_coro(_go)

    def on_position_close(
        self,
        runner: Any,
        prices: Optional[Mapping[str, float]],
        *,
        order_id: uuid.UUID,
        trade_id: uuid.UUID,
        sym: str,
        side: str,
        entry_price: float,
        exit_price: float,
        size: float,
        leverage: float,
        margin: float,
        gross_pnl: float,
        fee_open: float,
        fee_close: float,
        realized: float,
        pnl_pct: float,
        reason: str,
        opened_ts: float,
        closed_ts: float,
        free_cash_before_release: float,
    ) -> None:
        if not self.repository:
            return

        if prices is not None:
            snap = runner._fund_snapshot(prices)
        else:
            locked = sum(p["margin"] for p in runner.positions.values())
            uc = runner._initial_capital
            total_balance = uc + runner.gross_realized - runner.total_fees
            unrealized = runner.equity - runner.balance - locked
            snap = {
                "equity": runner.equity,
                "free_cash": runner.balance,
                "total_balance": total_balance,
                "margin_locked": locked,
                "unrealized": unrealized,
            }

        async def _go() -> None:
            await self.repository.persist_close(
                trade_id=trade_id,
                order_id=order_id,
                symbol=sym,
                side=side,
                entry_price=entry_price,
                exit_price=exit_price,
                size=size,
                leverage=leverage,
                margin_usd=margin,
                gross_pnl_usd=gross_pnl,
                fee_open_usd=fee_open,
                fee_close_usd=fee_close,
                realized_pnl_usd=realized,
                pnl_pct=pnl_pct,
                reason=reason,
                opened_ts=opened_ts,
                closed_ts=closed_ts,
                prev_free_cash=free_cash_before_release,
                snap=snap,
            )

        self.schedule_coro(_go)

    def on_balance_adjustment(
        self,
        runner: Any,
        prices: Mapping[str, float],
        *,
        event_type: str,
        delta_free_cash: float,
        sym: Optional[str],
        note: Optional[str],
        order_id: Optional[uuid.UUID] = None,
        meta: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if not self.repository:
            return
        snap = runner._fund_snapshot(prices)

        async def _go() -> None:
            await self.repository.persist_balance_adjustment(
                event_type=event_type,
                delta_free_cash=delta_free_cash,
                snap=snap,
                symbol=sym,
                note=note,
                order_id=order_id,
                meta=meta,
            )

        self.schedule_coro(_go)

    def schedule_state_redis(self, state_subset: Mapping[str, Any]) -> None:
        if not self.redis:
            return
        now = time.monotonic()
        if now - self._last_redis_push < self._redis_min_interval:
            return
        self._last_redis_push = now

        async def _push() -> None:
            try:
                payload = _json_safe(dict(state_subset))
                await self.redis.set(
                    REDIS_STATE_KEY,
                    json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
                    ex=self._redis_state_ttl,
                )
            except Exception:
                _log.exception("redis state push failed")

        self.schedule_coro(_push)


async def create_redis(url: str) -> Optional[redis.Redis]:
    if not url.strip():
        return None
    try:
        return redis.from_url(url, decode_responses=True)
    except Exception:
        _log.exception("redis connect failed")
        return None


async def close_redis(client: Optional[redis.Redis]) -> None:
    if client is not None:
        await client.close()

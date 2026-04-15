"""
OrderManager：统一管理下单、状态跃迁、TTL 撤单、定期对账。

- submit_intent：PENDING_CREATE → 网关 create_order → 按返回更新状态（纸面影子单为 OPEN / 部分成等）。
- TTL：限价 intent.ttl_ms>0 时，挂单存活超时后调用网关 cancel_order。
- reconcile：fetch_open_orders 与本地 OrderRecord 对齐（filled / 是否已完结）。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Dict, Optional

from src.core.config_manager import config_manager
from src.core.paper_engine import paper_engine
from src.execution.order_types import OrderIntent, OrderRecord, OrderState
from src.strategy.tuner import feed_realized_net_from_exchange_result
from src.utils.logger import log


class OrderManager:
    def __init__(
        self,
        exchange: Any,
        *,
        reconcile_interval_sec: float = 5.0,
    ):
        self._exchange = exchange
        self._reconcile_interval_sec = float(reconcile_interval_sec)
        self._orders: Dict[str, OrderRecord] = {}
        self._by_client_id: Dict[str, str] = {}
        self._intents: Dict[str, OrderIntent] = {}
        self._reconcile_task: Optional[asyncio.Task] = None
        self._running = False
        self._ttl_tasks: Dict[str, asyncio.Task] = {}

    @property
    def orders(self) -> Dict[str, OrderRecord]:
        return dict(self._orders)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._reconcile_task = asyncio.create_task(self._reconcile_loop())

    async def stop(self) -> None:
        self._running = False
        for t in list(self._ttl_tasks.values()):
            t.cancel()
        self._ttl_tasks.clear()
        if self._reconcile_task:
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
            except asyncio.CancelledError:
                pass
            self._reconcile_task = None

    def _cancel_ttl(self, intent_id: str) -> None:
        t = self._ttl_tasks.pop(intent_id, None)
        if t and not t.done():
            t.cancel()

    @staticmethod
    def _repriced_limit_price(symbol: str, side: str, fallback_price: float, offset_bps: float) -> float:
        bb, ba = paper_engine._best_bid_ask(symbol)
        off = max(0.0, float(offset_bps or 0.0)) / 1e4
        if str(side).lower() == "buy":
            if bb > 0:
                return max(0.0, bb)
            return max(0.0, float(fallback_price) * (1.0 - max(off, 0.0001)))
        if ba > 0:
            return max(0.0, ba)
        return max(0.0, float(fallback_price) * (1.0 + max(off, 0.0001)))

    def _should_requote(self, intent: OrderIntent) -> bool:
        ect = dict(intent.entry_context or {})
        return (
            str(intent.order_type).lower() == "limit"
            and not bool(intent.reduce_only)
            and bool(ect.get("core_limit_requote_enabled"))
            and bool(intent.post_only)
        )

    async def _maybe_requote_after_ttl(self, intent: OrderIntent, rec: OrderRecord) -> None:
        if not self._should_requote(intent):
            return
        ect = dict(intent.entry_context or {})
        cur_n = int(ect.get("core_limit_requote_count") or 0)
        max_n = int(ect.get("core_limit_requote_max") or 0)
        if cur_n >= max_n:
            return

        remaining_amount = float(intent.amount) - float(rec.filled or 0.0)
        if remaining_amount <= 1e-12:
            return

        offset_bps = float(
            ect.get("core_entry_limit_offset_bps")
            or getattr(config_manager.get_config().strategy.params, "core_entry_limit_offset_bps", 1.0)
            or 1.0
        )
        last_px = float(
            paper_engine.latest_prices.get(intent.symbol, 0.0)
            or intent.price
            or ect.get("entry_limit_price")
            or 0.0
        )
        if last_px <= 0:
            return
        new_price = self._repriced_limit_price(intent.symbol, intent.side, last_px, offset_bps)
        if new_price <= 0:
            return

        new_ect = {
            **ect,
            "entry_limit_price": float(new_price),
            "core_limit_requote_count": cur_n + 1,
            "core_limit_requoted_from": intent.intent_id,
            "client_oid": f"rq-{uuid.uuid4().hex[:18]}",
            "text": f"rq-{uuid.uuid4().hex[:18]}",
        }
        new_intent = OrderIntent(
            symbol=intent.symbol,
            side=intent.side,
            order_type="limit",
            amount=float(remaining_amount),
            price=float(new_price),
            leverage=int(intent.leverage),
            notional_size=float(intent.notional_size) * (remaining_amount / max(float(intent.amount), 1e-12)),
            margin_amount=float(intent.margin_amount) * (remaining_amount / max(float(intent.amount), 1e-12)),
            margin_mode=str(intent.margin_mode),
            reduce_only=bool(intent.reduce_only),
            post_only=True,
            berserker=bool(intent.berserker),
            entry_context=new_ect,
            is_high_conviction=bool(intent.is_high_conviction),
            trailing_stop_activation_pct=float(intent.trailing_stop_activation_pct or 0.0),
            trailing_stop_callback_pct=float(intent.trailing_stop_callback_pct or 0.0),
            ttl_ms=int(intent.ttl_ms or 0),
        )
        log.info(
            f"[OrderManager] Requote {intent.symbol} {intent.side} "
            f"attempt={cur_n + 1}/{max_n} px={new_price:.6f} remaining={remaining_amount:.6f}"
        )
        await self.submit_intent(new_intent)

    def _apply_submit_result(self, rec: OrderRecord, result: Any) -> None:
        if not isinstance(result, dict):
            if result is None:
                rec.state = OrderState.FAILED
            return
        st = str(result.get("status") or "").lower()
        ex_id = result.get("id")
        if ex_id is not None:
            rec.exchange_order_id = str(ex_id)
        if st in ("open", "resting"):
            rec.state = OrderState.OPEN
        elif st == "partially_filled":
            rec.state = OrderState.PARTIALLY_FILLED
            try:
                rec.filled = float(result.get("filled", 0) or 0)
            except (TypeError, ValueError):
                rec.filled = 0.0
        elif st in ("filled", "closed"):
            rec.state = OrderState.FILLED
            try:
                rec.filled = float(result.get("filled", rec.amount) or rec.amount)
            except (TypeError, ValueError):
                rec.filled = rec.amount
        elif st in ("canceled", "cancelled"):
            rec.state = OrderState.CANCELED
        elif st in ("rejected",):
            rec.state = OrderState.REJECTED
            rec.raw = {k: result[k] for k in ("reason", "label") if k in result}
        else:
            # 兼容旧纸面：未标 slice 的限价成交仍可能返回 "open" 表示成交回报语义混用 — 默认视为已提交
            rec.state = OrderState.OPEN

    def _schedule_ttl_if_needed(self, intent: OrderIntent, rec: OrderRecord) -> None:
        self._cancel_ttl(intent.intent_id)
        if intent.ttl_ms <= 0:
            return
        if rec.state not in (
            OrderState.OPEN,
            OrderState.PARTIALLY_FILLED,
            OrderState.SUBMITTED,
        ):
            return
        oid = rec.exchange_order_id or intent.intent_id
        sym = intent.symbol

        async def _run() -> None:
            try:
                await asyncio.sleep(float(intent.ttl_ms) / 1000.0)
            except asyncio.CancelledError:
                return
            cur = self._orders.get(intent.intent_id)
            if not cur or cur.state in (
                OrderState.FILLED,
                OrderState.CANCELED,
                OrderState.REJECTED,
                OrderState.FAILED,
            ):
                return
            cancel = getattr(self._exchange, "cancel_order", None)
            if not callable(cancel):
                log.warning("OrderManager TTL: exchange has no cancel_order")
                return
            try:
                await cancel(oid, symbol=sym)
                cur.state = OrderState.CANCELED
                cur.last_sync_ts = time.time()
                await self._maybe_requote_after_ttl(intent, cur)
            except Exception as e:
                log.warning(f"OrderManager TTL cancel failed: {e}")

        self._ttl_tasks[intent.intent_id] = asyncio.create_task(_run())

    async def submit_intent(self, intent: OrderIntent) -> Optional[Any]:
        """
        PENDING_CREATE → 调用网关 create_order → SUBMITTED / OPEN / FILLED / …
        """
        now = time.time()
        rec = OrderRecord(
            intent_id=intent.intent_id,
            client_order_id=intent.intent_id,
            symbol=intent.symbol,
            side=intent.side,
            state=OrderState.PENDING_CREATE,
            price=intent.price,
            amount=float(intent.amount),
            created_ts=now,
            last_sync_ts=now,
        )
        self._orders[intent.intent_id] = rec
        self._by_client_id[intent.intent_id] = intent.intent_id
        self._intents[intent.intent_id] = intent

        if not hasattr(self._exchange, "create_order"):
            rec.state = OrderState.FAILED
            log.error("OrderManager: exchange has no create_order")
            return None

        try:
            rec.state = OrderState.SUBMITTED
            px = intent.price if intent.order_type == "limit" else None
            ect = dict(intent.entry_context or {})
            if intent.is_high_conviction:
                ect["high_conviction_trailing"] = True
                ect["trailing_stop_activation_pct"] = float(
                    intent.trailing_stop_activation_pct or 0.0
                )
                ect["trailing_stop_callback_pct"] = float(
                    intent.trailing_stop_callback_pct or 0.0
                )
            if float(getattr(intent, "notional_size", 0.0) or 0.0) > 0:
                ect["intent_notional_usdt"] = float(intent.notional_size)
                ect["intent_margin_usdt"] = float(intent.margin_amount)
                ect["intent_dynamic_leverage"] = int(intent.leverage)
            result = await self._exchange.create_order(
                symbol=intent.symbol,
                side=intent.side,
                amount=intent.amount,
                price=px,
                reduce_only=intent.reduce_only,
                leverage=int(intent.leverage),
                margin_mode=str(intent.margin_mode),
                berserker=bool(intent.berserker),
                post_only=bool(intent.post_only),
                entry_context=ect if ect else None,
                exit_reason=ect.get("exit_reason"),
                order_text=ect.get("client_oid"),
            )
            rec.last_sync_ts = time.time()
            self._apply_submit_result(rec, result)
            feed_realized_net_from_exchange_result(result)
            if rec.state in (OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED):
                self._cancel_ttl(intent.intent_id)
            else:
                self._schedule_ttl_if_needed(intent, rec)
            return result
        except Exception as e:
            rec.state = OrderState.FAILED
            rec.raw = {"error": str(e)[:500]}
            log.error(f"OrderManager submit_intent failed: {e}")
            return None

    async def _reconcile_loop(self) -> None:
        while self._running:
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning(f"OrderManager reconcile: {e}")
            await asyncio.sleep(self._reconcile_interval_sec)

    async def reconcile_once(self) -> None:
        """
        拉取交易所开放订单并与本地合并；纸面影子单由 paper_engine 聚合。
        """
        fetch = getattr(self._exchange, "fetch_open_orders", None)
        if not callable(fetch):
            return
        try:
            remote = await fetch()
            if not isinstance(remote, list):
                return
            seen: Dict[str, Dict[str, Any]] = {}
            for o in remote:
                if not isinstance(o, dict):
                    continue
                oid = str(o.get("id") or "")
                if oid:
                    seen[oid] = o
            for intent_id, rec in list(self._orders.items()):
                rid = str(rec.exchange_order_id or "")
                if not rid or rec.state in (
                    OrderState.FILLED,
                    OrderState.CANCELED,
                    OrderState.REJECTED,
                    OrderState.FAILED,
                ):
                    continue
                if rid in seen:
                    row = seen[rid]
                    try:
                        rec.filled = float(row.get("filled") or 0.0)
                    except (TypeError, ValueError):
                        pass
                    st = str(row.get("status") or "").lower()
                    if st == "partially_filled":
                        rec.state = OrderState.PARTIALLY_FILLED
                    elif st == "open":
                        rec.state = OrderState.OPEN
                    rec.last_sync_ts = time.time()
                else:
                    # 远端已无此 open：可能已完全成交或被撤
                    if rec.state in (OrderState.OPEN, OrderState.PARTIALLY_FILLED, OrderState.SUBMITTED):
                        rec.state = OrderState.FILLED
                        rec.filled = rec.amount
                        rec.last_sync_ts = time.time()
                        self._cancel_ttl(intent_id)
        except Exception as e:
            log.debug(f"OrderManager reconcile skip: {e}")

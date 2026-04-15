"""
纸面持仓四维出场：ATR 初始止损 + Chandelier 追踪 + 保本上移 + OBI 抢先平仓 + 时间止损。
在 Gate WS 行情/盘口更新后调用；依赖 REST K 线计算开仓 ATR。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

from src.core.config_manager import config_manager
from src.core.paper_engine import paper_engine
from src.utils.atr import compute_atr_from_candles
from src.utils.logger import log

# 软出场：若毛利仅覆盖不了手续费，禁止平仓（避免「追踪止盈」名义下锁亏）
_SOFT_EXIT_REASONS = frozenset(
    {
        "exit_chandelier_trail",
        "exit_alpha_decay_time",
        "exit_obi_preempt",
    }
)


def calc_obi(bids: list, asks: list, top_n: int = 5) -> float:
    bid_vol = sum(float(x[1]) for x in (bids or [])[:top_n])
    ask_vol = sum(float(x[1]) for x in (asks or [])[:top_n])
    t = bid_vol + ask_vol
    if t <= 0:
        return 0.0
    return (bid_vol - ask_vol) / t


class PositionExitMonitor:
    def __init__(self) -> None:
        self._locks: Dict[str, asyncio.Lock] = {}

    def _lock(self, symbol: str) -> asyncio.Lock:
        if symbol not in self._locks:
            self._locks[symbol] = asyncio.Lock()
        return self._locks[symbol]

    async def _ensure_atr(self, exchange: Any, symbol: str, pos: Dict[str, Any]) -> float:
        cfg = config_manager.get_config().exit_management
        ex = pos.get("exit") or {}
        if ex.get("atr") is not None and float(ex["atr"]) > 0:
            return float(ex["atr"])

        need = cfg.atr_period + 5
        candles: list = []
        if hasattr(exchange, "fetch_candlesticks"):
            candles = await exchange.fetch_candlesticks(
                symbol, interval=cfg.candle_interval, limit=max(need, 32)
            )
        candles = [c for c in candles if c.get("time") and c.get("high") is not None]
        candles.sort(key=lambda x: int(x["time"]))

        atr = compute_atr_from_candles(candles, cfg.atr_period) if candles else 0.0
        if atr <= 0:
            from src.core.risk_engine import risk_engine

            last = float(
                paper_engine.latest_prices.get(symbol, pos.get("entry_price", 0)) or 0
            )
            pct = float(risk_engine.symbol_atr_pct.get(symbol, 0.02) or 0.02)
            atr = max(last * pct, last * 0.001)

        entry = float(pos["entry_price"])
        m = float(cfg.atr_sl_multiplier)
        side = pos["side"]
        if side == "long":
            initial_sl = entry - m * atr
        else:
            initial_sl = entry + m * atr

        ex = pos.setdefault("exit", {})
        ex["atr"] = atr
        ex["initial_sl"] = initial_sl
        ex["active_sl"] = initial_sl
        ex["extreme"] = entry
        log.info(f"[ExitMgr] {symbol} ATR={atr:.6f} initial_sl={initial_sl:.6f} side={side}")
        return atr

    def _active_sl_long(
        self, symbol: str, pos: Dict[str, Any], last: float, cfg: Any
    ) -> float:
        ex = pos["exit"]
        atr = float(ex["atr"])
        entry = float(pos["entry_price"])
        sl0 = float(ex["initial_sl"])
        extreme = max(float(ex["extreme"]), last)
        ex["extreme"] = extreme
        trail = extreme - float(cfg.atr_trailing_multiplier) * atr
        cand = max(sl0, trail)
        buf = entry * (float(cfg.breakeven_fee_buffer_bps) / 10000.0)
        if getattr(cfg, "breakeven_roundtrip_taker", True):
            tk, _ = paper_engine._fee_rates_for_symbol(symbol)
            buf += entry * 2.0 * float(tk)
        if last - entry >= float(cfg.breakeven_r_multiple) * atr:
            ex["breakeven_done"] = True
            cand = max(cand, entry + buf)
        ex["trailing_armed"] = extreme > entry + 1e-12
        return cand

    def _active_sl_short(
        self, symbol: str, pos: Dict[str, Any], last: float, cfg: Any
    ) -> float:
        ex = pos["exit"]
        atr = float(ex["atr"])
        entry = float(pos["entry_price"])
        sl0 = float(ex["initial_sl"])
        extreme = min(float(ex["extreme"]), last)
        ex["extreme"] = extreme
        trail = extreme + float(cfg.atr_trailing_multiplier) * atr
        cand = min(sl0, trail)
        buf = entry * (float(cfg.breakeven_fee_buffer_bps) / 10000.0)
        if getattr(cfg, "breakeven_roundtrip_taker", True):
            tk, _ = paper_engine._fee_rates_for_symbol(symbol)
            buf += entry * 2.0 * float(tk)
        if entry - last >= float(cfg.breakeven_r_multiple) * atr:
            ex["breakeven_done"] = True
            cand = min(cand, entry - buf)
        ex["trailing_armed"] = extreme < entry - 1e-12
        return cand

    def _stop_hit_reason(self, pos: Dict[str, Any]) -> str:
        ex = pos["exit"]
        if ex.get("breakeven_done") or ex.get("trailing_armed"):
            return "exit_chandelier_trail"
        return "exit_atr_initial"

    async def _close_position(
        self,
        symbol: str,
        reason: str,
        pos: Dict[str, Any],
        *,
        limit_exit_price: Optional[float] = None,
        exit_as_maker: bool = False,
    ) -> None:
        if pos.get("_force_flat_pending"):
            return
        ex = pos.get("exit")
        if ex and ex.get("closing"):
            return
        sz = float(pos.get("size", 0) or 0)
        if sz <= 0:
            return

        cfg_exit = config_manager.get_config().exit_management
        if (
            cfg_exit.soft_exit_net_floor_enabled
            and reason in _SOFT_EXIT_REASONS
        ):
            last = float(paper_engine.latest_prices.get(symbol, 0) or 0)
            if last > 0:
                est = paper_engine.estimate_flat_net_pnl(symbol, pos, last)
                cs = paper_engine._position_contract_size(pos, symbol)
                ep = float(pos["entry_price"])
                contracts = float(pos["size"])
                notional = paper_engine._notional_from(contracts, ep, cs)
                thr = notional * (float(cfg_exit.soft_exit_min_net_bps) / 1e4)
                if est < thr:
                    log.debug(
                        f"[ExitMgr] soft exit blocked {symbol} {reason}: "
                        f"est_net={est:.4f} < floor={thr:.4f} ({cfg_exit.soft_exit_min_net_bps:g}bps on notional)"
                    )
                    return

        if ex:
            ex["closing"] = True
        elif (pos.get("entry_context") or {}).get("slingshot_managed"):
            pos["_force_flat_pending"] = True
        elif (pos.get("entry_context") or {}).get("assassin_managed"):
            pos["_force_flat_pending"] = True
        else:
            return
        side = "sell" if pos["side"] == "long" else "buy"
        log.warning(f"[ExitMgr] {symbol} force flat reason={reason} size={sz}")
        px = (
            float(limit_exit_price)
            if limit_exit_price is not None and float(limit_exit_price) > 0
            else None
        )
        ect: Dict[str, Any] = {}
        if exit_as_maker and px is not None:
            ect["maker_filled"] = True
        paper_engine.execute_order(
            symbol,
            side,
            sz,
            px,
            reduce_only=True,
            leverage=int(pos.get("leverage", 10)),
            margin_mode=str(pos.get("margin_mode", "cross")),
            exit_reason=reason,
            entry_context=ect or None,
        )

    async def on_ticker(self, exchange: Any, symbol: str, last: float) -> None:
        if last <= 0:
            return
        if not getattr(exchange, "use_paper_trading", False):
            return

        pos = paper_engine.positions.get(symbol)
        if not pos or float(pos.get("size", 0) or 0) <= 0:
            return

        if (pos.get("entry_context") or {}).get("slingshot_managed"):
            sc = config_manager.get_config().slingshot
            async with self._lock(symbol):
                pos = paper_engine.positions.get(symbol)
                if not pos or float(pos.get("size", 0) or 0) <= 0:
                    return
                if not (pos.get("entry_context") or {}).get("slingshot_managed"):
                    return
                if pos.get("_force_flat_pending"):
                    return
                age = time.time() - float(pos.get("opened_at", 0) or 0)
                if age < float(sc.time_stop_sec):
                    return
                entry = float(pos["entry_price"])
                min_b = float(sc.time_stop_min_bounce_bps) / 1e4
                bounced = False
                if pos["side"] == "long":
                    bounced = last >= entry * (1.0 + min_b)
                else:
                    bounced = last <= entry * (1.0 - min_b)
                if bounced:
                    return
                paper_engine.cancel_open_makers(symbol)
                await self._close_position(symbol, "slingshot_time_stop", pos)
            return

        if (pos.get("entry_context") or {}).get("assassin_managed"):
            return

        ect_im = pos.get("entry_context") or {}
        if ect_im.get("infinite_matrix_ultra") and bool(
            getattr(config_manager.get_config().infinite_matrix, "enabled", False)
        ):
            from src.core.infinite_matrix_runner import infinite_matrix_runner

            await infinite_matrix_runner.on_ticker(exchange, symbol, last)
            return

        if (pos.get("entry_context") or {}).get("leadlag_managed"):
            if (pos.get("entry_context") or {}).get("leadlag_bracket_protocol"):
                return
            ll = config_manager.get_config().binance_leadlag
            sl_bps = float(getattr(ll, "sl_bps", 100.0))
            async with self._lock(symbol):
                pos = paper_engine.positions.get(symbol)
                if not pos or float(pos.get("size", 0) or 0) <= 0:
                    return
                if not (pos.get("entry_context") or {}).get("leadlag_managed"):
                    return
                entry = float(pos["entry_price"])
                frac = sl_bps / 1e4
                hit = False
                if pos["side"] == "long" and last <= entry * (1.0 - frac):
                    hit = True
                elif pos["side"] == "short" and last >= entry * (1.0 + frac):
                    hit = True
                if hit:
                    paper_engine.cancel_open_makers(symbol)
                    await self._close_position(symbol, "leadlag_hard_sl", pos)
            return

        cfg = config_manager.get_config().exit_management
        if not cfg.enabled:
            return

        if (pos.get("entry_context") or {}).get("l1_bracket_protocol"):
            return
        if (pos.get("entry_context") or {}).get("fixed_tp_sl_protocol"):
            return
        ex = pos.get("exit")
        if not ex:
            return

        async with self._lock(symbol):
            pos = paper_engine.positions.get(symbol)
            if not pos or float(pos.get("size", 0) or 0) <= 0:
                return
            if (pos.get("entry_context") or {}).get("l1_bracket_protocol"):
                return
            if (pos.get("entry_context") or {}).get("fixed_tp_sl_protocol"):
                return
            ex = pos.get("exit")
            if not ex or ex.get("closing"):
                return

            await self._ensure_atr(exchange, symbol, pos)
            if float(ex.get("atr", 0) or 0) <= 0:
                return

            age = time.time() - float(pos.get("opened_at", 0) or 0)
            if age >= float(cfg.time_stop_sec):
                band = float(cfg.time_stop_atr_fraction) * float(ex["atr"])
                if abs(last - float(pos["entry_price"])) <= band:
                    await self._close_position(symbol, "exit_alpha_decay_time", pos)
                    return

            side = pos["side"]
            if side == "long":
                active = self._active_sl_long(symbol, pos, last, cfg)
            else:
                active = self._active_sl_short(symbol, pos, last, cfg)
            ex["active_sl"] = active

            eps = max(1e-8, last * 1e-6)
            reason_hit = self._stop_hit_reason(pos)
            use_chandelier_lim = (
                reason_hit == "exit_chandelier_trail"
                and getattr(cfg, "chandelier_exit_limit_maker", True)
            )
            lim_px = float(active) if use_chandelier_lim else None
            if side == "long" and last <= active + eps:
                await self._close_position(
                    symbol,
                    reason_hit,
                    pos,
                    limit_exit_price=lim_px,
                    exit_as_maker=bool(lim_px is not None),
                )
            elif side == "short" and last >= active - eps:
                await self._close_position(
                    symbol,
                    reason_hit,
                    pos,
                    limit_exit_price=lim_px,
                    exit_as_maker=bool(lim_px is not None),
                )

    async def on_orderbook(self, exchange: Any, symbol: str, obi: float) -> None:
        cfg = config_manager.get_config().exit_management
        if not cfg.enabled:
            return
        if not getattr(exchange, "use_paper_trading", False):
            return

        pos = paper_engine.positions.get(symbol)
        if not pos or float(pos.get("size", 0) or 0) <= 0:
            return
        if (pos.get("entry_context") or {}).get("l1_bracket_protocol"):
            return
        if (pos.get("entry_context") or {}).get("fixed_tp_sl_protocol"):
            return
        if (pos.get("entry_context") or {}).get("slingshot_managed"):
            return
        if (pos.get("entry_context") or {}).get("assassin_managed"):
            return
        if (pos.get("entry_context") or {}).get("leadlag_bracket_protocol"):
            return
        ex = pos.get("exit")
        if not ex or ex.get("closing"):
            return

        thr = float(cfg.obi_preemptive_threshold)
        hold = float(cfg.obi_preemptive_hold_sec)
        now = time.time()

        async with self._lock(symbol):
            pos = paper_engine.positions.get(symbol)
            if not pos or float(pos.get("size", 0) or 0) <= 0:
                return
            if (pos.get("entry_context") or {}).get("l1_bracket_protocol"):
                return
            if (pos.get("entry_context") or {}).get("fixed_tp_sl_protocol"):
                return
            if (pos.get("entry_context") or {}).get("slingshot_managed"):
                return
            if (pos.get("entry_context") or {}).get("assassin_managed"):
                return
            if (pos.get("entry_context") or {}).get("leadlag_managed"):
                return
            ex = pos.get("exit")
            if not ex or ex.get("closing"):
                return

            if pos["side"] == "long":
                if obi <= -thr:
                    if ex["obi_adverse_since"] is None:
                        ex["obi_adverse_since"] = now
                    elif now - float(ex["obi_adverse_since"]) >= hold:
                        await self._close_position(symbol, "exit_obi_preempt", pos)
                else:
                    ex["obi_adverse_since"] = None
            else:
                if obi >= thr:
                    if ex["obi_adverse_since"] is None:
                        ex["obi_adverse_since"] = now
                    elif now - float(ex["obi_adverse_since"]) >= hold:
                        await self._close_position(symbol, "exit_obi_preempt", pos)
                else:
                    ex["obi_adverse_since"] = None


position_exit_monitor = PositionExitMonitor()

__all__ = ["position_exit_monitor", "calc_obi"]

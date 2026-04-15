"""
L1 狙击：WS trades 驱动 CVD + 盘口 OBI + 1m ATR；Taker 开仓由 Signal 队列执行，
纸面撮合下止盈挂单由 paper_engine 在成交后立刻入队。
"""

from __future__ import annotations

import time
from typing import Any, Dict

from src.config import SystemState
from src.core.config_manager import config_manager
from src.core.events import OrderBookEvent, SignalEvent, TickEvent
from src.core.l1_fast_loop import (
    _eff,
    _eff_bool_halt,
    _eff_position_scale,
    atr_1m_bps,
    cvd_metrics,
    obi_opposes_long,
    obi_opposes_short,
)
from src.core.paper_engine import paper_engine
from src.strategy.base import BaseStrategy


class L1SniperStrategy(BaseStrategy):
    def __init__(self) -> None:
        super().__init__("L1Sniper")
        self._last_obi: Dict[str, float] = {}
        self._last_entry_ts: Dict[str, float] = {}
        self._last_cvd_flat_ts: Dict[str, float] = {}

    async def on_tick(self, event: TickEvent) -> None:
        return

    async def on_orderbook(self, event: OrderBookEvent) -> None:
        self._last_obi[event.symbol] = float(event.obi)

    def _l1_allowed(self, state_machine: Any) -> bool:
        cfg = config_manager.get_config()
        lc = cfg.l1_fast_loop
        if "l1_sniper" not in cfg.strategy.active_strategies or not lc.enabled:
            return False
        st = state_machine.state
        if st == SystemState.LICENSE_LOCKED:
            return False
        if st == SystemState.COOL_DOWN:
            return False
        if lc.require_attack_mode and st not in (SystemState.ATTACK, SystemState.BERSERKER):
            return False
        return True

    async def on_ws_trades(self, symbol: str, state_machine: Any) -> None:
        if not self._l1_allowed(state_machine):
            return

        cfg = config_manager.get_config()
        lc = cfg.l1_fast_loop
        if symbol not in cfg.strategy.symbols:
            return
        if _eff_bool_halt():
            return

        now = time.time()
        pos = paper_engine.positions.get(symbol)
        size = float(pos.get("size", 0) or 0) if pos else 0.0
        ctx = (pos or {}).get("entry_context") or {}
        l1_pos = bool(ctx.get("l1_managed")) and size > 0

        c10, _c60, baseline = cvd_metrics(symbol, now)
        stop_mult = _eff("cvd_stop_mult", lc.cvd_stop_mult)
        thr_stop = baseline * stop_mult

        if l1_pos:
            side = str(pos.get("side", "long"))
            if side == "long" and c10 < -thr_stop:
                if now - self._last_cvd_flat_ts.get(symbol, 0) < 0.35:
                    return
                self._last_cvd_flat_ts[symbol] = now
                paper_engine.cancel_open_makers(symbol)
                last = float(paper_engine.latest_prices.get(symbol, 0) or 0)
                self.emit_signal(
                    SignalEvent(
                        strategy_name=self.name,
                        symbol=symbol,
                        side="sell",
                        order_type="market",
                        price=last,
                        amount=size,
                        leverage=int(pos.get("leverage", lc.leverage)),
                        reduce_only=True,
                        margin_mode=str(pos.get("margin_mode", "cross")),
                        entry_context={
                            "l1_managed": True,
                            "l1_cvd_stop": True,
                            "exit_reason": "l1_cvd_stop",
                        },
                    )
                )
                await self.log(f"{symbol} CVD stop long c10={c10:.4f} baseline={baseline:.4f}")
            elif side == "short" and c10 > thr_stop:
                if now - self._last_cvd_flat_ts.get(symbol, 0) < 0.35:
                    return
                self._last_cvd_flat_ts[symbol] = now
                paper_engine.cancel_open_makers(symbol)
                last = float(paper_engine.latest_prices.get(symbol, 0) or 0)
                self.emit_signal(
                    SignalEvent(
                        strategy_name=self.name,
                        symbol=symbol,
                        side="buy",
                        order_type="market",
                        price=last,
                        amount=size,
                        leverage=int(pos.get("leverage", lc.leverage)),
                        reduce_only=True,
                        margin_mode=str(pos.get("margin_mode", "cross")),
                        entry_context={
                            "l1_managed": True,
                            "l1_cvd_stop": True,
                            "exit_reason": "l1_cvd_stop",
                        },
                    )
                )
                await self.log(f"{symbol} CVD stop short c10={c10:.4f} baseline={baseline:.4f}")
            return

        if symbol not in self._last_obi:
            return

        atr_bps = atr_1m_bps(symbol)
        min_atr = _eff("min_atr_bps", lc.min_atr_bps)
        if atr_bps < min_atr:
            return

        burst_mult = _eff("cvd_burst_mult", lc.cvd_burst_mult)
        thr_burst = baseline * burst_mult
        obi = float(self._last_obi.get(symbol, 0.0))

        cool = float(lc.signal_cooldown_sec)
        if now - self._last_entry_ts.get(symbol, 0) < cool:
            return

        last = float(paper_engine.latest_prices.get(symbol, 0) or 0)
        if last <= 0:
            return

        scale = _eff_position_scale()
        notional = float(lc.trade_notional_usd) * max(scale, 0.0)
        qty = notional / last
        if qty <= 0:
            return

        lev = int(lc.leverage)

        if c10 > thr_burst and not obi_opposes_long(obi, lc.max_obi_opposition_long):
            self._last_entry_ts[symbol] = now
            l1_micro = {
                "ts": now,
                "side_signal": "long",
                "cvd_10s": c10,
                "cvd_60s": _c60,
                "cvd_baseline": baseline,
                "burst_thr": thr_burst,
                "atr_1m_bps": atr_bps,
                "obi_top5": obi,
            }
            self.emit_signal(
                SignalEvent(
                    strategy_name=self.name,
                    symbol=symbol,
                    side="buy",
                    order_type="market",
                    price=last,
                    amount=qty,
                    leverage=lev,
                    reduce_only=False,
                    post_only=False,
                    margin_mode="cross",
                    entry_context={
                        "l1_managed": True,
                        "l1_tp_bps": float(lc.tp_bps),
                        "l1_atr_bps": float(atr_bps),
                        "l1_signal_micro": l1_micro,
                    },
                )
            )
            await self.log(
                f"{symbol} L1 long burst c10={c10:.4f} thr={thr_burst:.4f} atr_bps={atr_bps:.1f} obi={obi:.3f}"
            )
            return

        min_opp_short = -float(lc.max_obi_opposition_long)
        if c10 < -thr_burst and not obi_opposes_short(obi, min_opp_short):
            self._last_entry_ts[symbol] = now
            l1_micro = {
                "ts": now,
                "side_signal": "short",
                "cvd_10s": c10,
                "cvd_60s": _c60,
                "cvd_baseline": baseline,
                "burst_thr": thr_burst,
                "atr_1m_bps": atr_bps,
                "obi_top5": obi,
            }
            self.emit_signal(
                SignalEvent(
                    strategy_name=self.name,
                    symbol=symbol,
                    side="sell",
                    order_type="market",
                    price=last,
                    amount=qty,
                    leverage=lev,
                    reduce_only=False,
                    post_only=False,
                    margin_mode="cross",
                    entry_context={
                        "l1_managed": True,
                        "l1_tp_bps": float(lc.tp_bps),
                        "l1_atr_bps": float(atr_bps),
                        "l1_signal_micro": l1_micro,
                    },
                )
            )
            await self.log(
                f"{symbol} L1 short burst c10={c10:.4f} thr={thr_burst:.4f} atr_bps={atr_bps:.1f} obi={obi:.3f}"
            )

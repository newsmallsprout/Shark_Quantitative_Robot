"""
微观游击刺客：60s 滚动成交 VWAP + 吃单耗竭 → Taker 反手；可选成本感知（Hurdle、净空间、硬净利止盈）。
单标的互斥与冷静期见 assassin_gate + process_signals 二次闸。
"""

from __future__ import annotations

import time
from typing import Any, Dict

from src.config import SystemState
from src.core.assassin_cost import (
    assassin_hurdle_rate,
    long_entry_net_floor_ok,
    short_entry_net_floor_ok,
)
from src.core.assassin_gate import assassin_entry_blocked
from src.core.config_manager import config_manager
from src.core.events import SignalEvent, TickEvent
from src.core.l1_fast_loop import (
    _eff_bool_halt,
    _eff_position_scale,
    atr_1m_bps,
    rolling_trade_vwap,
    taker_buy_exhausted,
    taker_sell_exhausted,
)
from src.core.paper_engine import paper_engine
from src.strategy.base import BaseStrategy


class MicroAssassinStrategy(BaseStrategy):
    def __init__(self) -> None:
        super().__init__("MicroAssassin")
        self._last_signal_ts: Dict[str, float] = {}

    async def on_tick(self, event: TickEvent) -> None:
        return

    def _allowed(self, state_machine: Any) -> bool:
        cfg = config_manager.get_config()
        ac = cfg.assassin_micro
        if "micro_assassin" not in cfg.strategy.active_strategies or not ac.enabled:
            return False
        st = state_machine.state
        if st == SystemState.LICENSE_LOCKED:
            return False
        if st == SystemState.COOL_DOWN:
            return False
        if ac.require_attack_mode and st not in (SystemState.ATTACK, SystemState.BERSERKER):
            return False
        return True

    async def on_ws_trades(self, symbol: str, state_machine: Any) -> None:
        if not self._allowed(state_machine):
            return
        if _eff_bool_halt():
            return

        cfg = config_manager.get_config()
        if symbol not in cfg.strategy.symbols:
            return

        ac = cfg.assassin_micro
        now = time.time()
        if now - self._last_signal_ts.get(symbol, 0) < float(ac.signal_cooldown_sec):
            return

        if assassin_entry_blocked(symbol):
            return

        last = float(paper_engine.latest_prices.get(symbol, 0) or 0)
        if last <= 0:
            return

        vwap = rolling_trade_vwap(symbol, window=float(ac.vwap_window_sec), now=now)
        if vwap is None or vwap <= 0:
            return

        thr_bps = float(ac.deviation_bps)
        m = float(ac.deviation_atr_mult)
        if m > 0:
            atrb = atr_1m_bps(symbol)
            thr_bps = max(thr_bps, m * atrb)
        thr = thr_bps / 1e4
        dev = (last - vwap) / vwap

        burst = float(ac.exhaustion_burst_sec)
        base_w = float(ac.exhaustion_baseline_sec)
        ratio = float(ac.exhaustion_ratio)
        min_base = float(ac.min_baseline_taker_vol)

        scale = _eff_position_scale()
        notional = float(ac.trade_notional_usd) * max(scale, 0.0)
        qty = notional / last
        if qty <= 0:
            return

        lev = int(ac.leverage)
        frac = float(ac.tp_path_fraction)
        cost_aware = bool(getattr(ac, "use_cost_aware", True))
        hurdle = assassin_hurdle_rate(symbol, paper_engine)
        tn = float(getattr(ac, "target_net_frac", 5e-4))
        nsm = float(getattr(ac, "net_space_hurdle_mult", 2.5))

        def _long_ok() -> bool:
            if cost_aware:
                ok, _why = long_entry_net_floor_ok(
                    last, vwap, hurdle, nsm, thr, tn
                )
                return ok
            return dev <= -thr

        def _short_ok() -> bool:
            if cost_aware:
                ok, _why = short_entry_net_floor_ok(
                    last, vwap, hurdle, nsm, thr, tn
                )
                return ok
            return dev >= thr

        def _ect(side: str) -> Dict[str, Any]:
            base: Dict[str, Any] = {
                "assassin_managed": True,
                "assassin_target_vwap": float(vwap),
                "assassin_tp_path_fraction": frac,
                "assassin_sl_bps": float(ac.sl_bps),
            }
            if cost_aware:
                base["assassin_cost_aware"] = True
                base["assassin_hurdle_frac"] = float(hurdle)
                base["assassin_target_net_frac"] = float(tn)
            return base

        # 价格显著低于 VWAP + 主动卖盘枯竭 → 反手做多
        if _long_ok() and taker_sell_exhausted(symbol, burst, base_w, ratio, min_base, now):
            self._last_signal_ts[symbol] = now
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
                    entry_context=_ect("buy"),
                )
            )
            extra = f"hurdle={hurdle*10000:.2f}bps tn={tn*10000:.2f}bps" if cost_aware else f"dev={dev*10000:.1f}bps"
            await self.log(
                f"{symbol} assassin LONG {extra} vwap={vwap:.4f} last={last:.4f}"
            )
            return

        # 价格显著高于 VWAP + 主动买盘枯竭 → 反手做空
        if _short_ok() and taker_buy_exhausted(symbol, burst, base_w, ratio, min_base, now):
            self._last_signal_ts[symbol] = now
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
                    entry_context=_ect("sell"),
                )
            )
            extra = f"hurdle={hurdle*10000:.2f}bps tn={tn*10000:.2f}bps" if cost_aware else f"dev={dev*10000:.1f}bps"
            await self.log(
                f"{symbol} assassin SHORT {extra} vwap={vwap:.4f} last={last:.4f}"
            )

"""
引力弹弓：极端均值回归。布林 kσ 外轨 + 短 RSI + CVD 瀑布 同时满足时，
仅 Post-Only 限价在针尖下方/上方挂单；纸面成交后由 paper_engine 挂微反弹 OCO 括号。
"""

from __future__ import annotations

import time
from typing import Any, Optional, Tuple

from src.config import SystemState
from src.core.config_manager import config_manager
from src.core.events import SignalEvent, TickEvent
from src.core.globals import bot_context
from src.core.l1_fast_loop import (
    _eff_bool_halt,
    _eff_position_scale,
    cvd_metrics,
    ohlc_1m_closes,
)
from src.core.paper_engine import paper_engine
from src.core.slingshot_indicators import bollinger_bands, rsi_sma
from src.strategy.base import BaseStrategy


class SlingshotStrategy(BaseStrategy):
    def __init__(self) -> None:
        super().__init__("Slingshot")
        self._last_signal_ts: dict[str, float] = {}

    def _allowed(self, state_machine: Any) -> bool:
        cfg = config_manager.get_config()
        sc = cfg.slingshot
        if "slingshot" not in cfg.strategy.active_strategies or not sc.enabled:
            return False
        st = state_machine.state
        if st == SystemState.LICENSE_LOCKED:
            return False
        if st == SystemState.COOL_DOWN:
            return False
        if sc.require_attack_mode and st not in (SystemState.ATTACK, SystemState.BERSERKER):
            return False
        return True

    def _has_slingshot_entry_resting(self, symbol: str) -> bool:
        for o in paper_engine._maker_resting.get(symbol, []):
            if (o.get("entry_context") or {}).get("slingshot_maker_entry"):
                return True
        return False

    def _extreme_long(self, symbol: str, last: float, sc: Any) -> Tuple[bool, Optional[dict]]:
        closes = ohlc_1m_closes(symbol, max_bars=64)
        p = int(sc.bb_period)
        if len(closes) < p:
            return False, None
        bb = bollinger_bands(closes, p, float(sc.bb_std_mult))
        if bb is None:
            return False, None
        _mu, _up, lo = bb
        if last > lo:
            return False, None
        rsi_p = int(sc.rsi_period)
        rsi_closes = closes + [last]
        rsi_val = rsi_sma(rsi_closes, rsi_p)
        if rsi_val is None or rsi_val > float(sc.rsi_oversold):
            return False, None
        c10, _c60, baseline = cvd_metrics(symbol, time.time())
        thr = float(sc.cvd_waterfall_mult) * baseline
        if c10 >= -thr:
            return False, None
        return True, {
            "bb_lower": lo,
            "bb_mid": bb[0],
            "rsi": rsi_val,
            "cvd_10s": c10,
            "cvd_thr": thr,
        }

    def _extreme_short(self, symbol: str, last: float, sc: Any) -> Tuple[bool, Optional[dict]]:
        closes = ohlc_1m_closes(symbol, max_bars=64)
        p = int(sc.bb_period)
        if len(closes) < p:
            return False, None
        bb = bollinger_bands(closes, p, float(sc.bb_std_mult))
        if bb is None:
            return False, None
        _mu, up, _lo = bb
        if last < up:
            return False, None
        rsi_p = int(sc.rsi_period)
        rsi_closes = closes + [last]
        rsi_val = rsi_sma(rsi_closes, rsi_p)
        if rsi_val is None or rsi_val < float(sc.rsi_overbought):
            return False, None
        c10, _c60, baseline = cvd_metrics(symbol, time.time())
        thr = float(sc.cvd_waterfall_mult) * baseline
        if c10 <= thr:
            return False, None
        return True, {
            "bb_upper": up,
            "bb_mid": bb[0],
            "rsi": rsi_val,
            "cvd_10s": c10,
            "cvd_thr": thr,
        }

    async def on_tick(self, event: TickEvent) -> None:
        sm = bot_context.get_state_machine()
        if sm is None:
            return
        if not self._allowed(sm):
            return
        if _eff_bool_halt():
            return

        symbol = event.symbol
        cfg = config_manager.get_config()
        if symbol not in cfg.strategy.symbols:
            return

        sc = cfg.slingshot
        now = time.time()
        if now - self._last_signal_ts.get(symbol, 0) < float(sc.signal_cooldown_sec):
            return

        last = float(paper_engine.latest_prices.get(symbol, 0) or 0)
        if last <= 0:
            return

        pos = paper_engine.positions.get(symbol)
        if pos and float(pos.get("size", 0) or 0) > 0:
            return

        if self._has_slingshot_entry_resting(symbol):
            ok_l, _ = self._extreme_long(symbol, last, sc)
            ok_s, _ = self._extreme_short(symbol, last, sc)
            if not ok_l and not ok_s:
                n = paper_engine.cancel_slingshot_entry_orders(symbol)
                if n:
                    await self.log(f"{symbol} slingshot cancel entry (stretch released) n={n}")
            return

        scale = _eff_position_scale()
        notional = float(sc.trade_notional_usd) * max(scale, 0.0)
        qty = notional / last
        if qty <= 0:
            return

        lev = int(sc.leverage)
        depth = float(sc.entry_depth_bps) / 1e4

        ok_long, snap_l = self._extreme_long(symbol, last, sc)
        if ok_long:
            limit_px = last * (1.0 - depth)
            tp_px = limit_px * (1.0 + float(sc.tp_bps) / 1e4)
            if getattr(sc, "net_edge_gate_enabled", True):
                edge = paper_engine.round_trip_edge_usdt(
                    symbol,
                    qty,
                    limit_px,
                    tp_px,
                    position_side="long",
                    entry_is_taker=bool(getattr(sc, "friction_assume_taker_entry", False)),
                    exit_is_maker=bool(getattr(sc, "friction_exit_is_maker", True)),
                    include_spread_penalty=True,
                )
                if edge["expected_net_usdt"] <= float(getattr(sc, "min_expected_net_usdt", 0.0)):
                    await self.log(
                        f"{symbol} slingshot NET_EDGE_BLOCK long net={edge['expected_net_usdt']:.4f} "
                        f"(gross={edge['gross_usdt']:.4f} fees={edge['fee_open_usdt']:.4f}+{edge['fee_close_usdt']:.4f} "
                        f"spread≈{edge['spread_penalty_usdt']:.4f})"
                    )
                    return
            self._last_signal_ts[symbol] = now
            self.emit_signal(
                SignalEvent(
                    strategy_name=self.name,
                    symbol=symbol,
                    side="buy",
                    order_type="limit",
                    price=limit_px,
                    amount=qty,
                    leverage=lev,
                    reduce_only=False,
                    post_only=True,
                    margin_mode="cross",
                    entry_context={
                        "resting_quote": True,
                        "slingshot_maker_entry": True,
                        "slingshot_snap": snap_l,
                    },
                )
            )
            await self.log(
                f"{symbol} slingshot POST buy @ {limit_px:.6f} (last={last:.6f} depth_bps={sc.entry_depth_bps})"
            )
            return

        ok_short, snap_s = self._extreme_short(symbol, last, sc)
        if ok_short:
            limit_px = last * (1.0 + depth)
            tp_px = limit_px * (1.0 - float(sc.tp_bps) / 1e4)
            if getattr(sc, "net_edge_gate_enabled", True):
                edge = paper_engine.round_trip_edge_usdt(
                    symbol,
                    qty,
                    limit_px,
                    tp_px,
                    position_side="short",
                    entry_is_taker=bool(getattr(sc, "friction_assume_taker_entry", False)),
                    exit_is_maker=bool(getattr(sc, "friction_exit_is_maker", True)),
                    include_spread_penalty=True,
                )
                if edge["expected_net_usdt"] <= float(getattr(sc, "min_expected_net_usdt", 0.0)):
                    await self.log(
                        f"{symbol} slingshot NET_EDGE_BLOCK short net={edge['expected_net_usdt']:.4f} "
                        f"(gross={edge['gross_usdt']:.4f} fees={edge['fee_open_usdt']:.4f}+{edge['fee_close_usdt']:.4f} "
                        f"spread≈{edge['spread_penalty_usdt']:.4f})"
                    )
                    return
            self._last_signal_ts[symbol] = now
            self.emit_signal(
                SignalEvent(
                    strategy_name=self.name,
                    symbol=symbol,
                    side="sell",
                    order_type="limit",
                    price=limit_px,
                    amount=qty,
                    leverage=lev,
                    reduce_only=False,
                    post_only=True,
                    margin_mode="cross",
                    entry_context={
                        "resting_quote": True,
                        "slingshot_maker_entry": True,
                        "slingshot_snap": snap_s,
                    },
                )
            )
            await self.log(
                f"{symbol} slingshot POST sell @ {limit_px:.6f} (last={last:.6f} depth_bps={sc.entry_depth_bps})"
            )

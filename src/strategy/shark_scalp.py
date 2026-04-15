"""
狂鲨剥头皮：近窗 BBO 买盘量 ≫ 卖盘 + 连续主动买成交 → 市价开多；
固定名义 + fiat_tp_sl 反推 +20U / -10U（净）触价括号（纸面 OCO）。
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from src.config import SystemState
from src.core.config_manager import config_manager
from src.core.equity_sizing import margin_cap_fraction, shark_net_tp_sl_usdt
from src.core.events import OrderBookEvent, SignalEvent
from src.core.fiat_tp_sl import compute_tp_sl_prices_net_usdt, estimate_fees_usdt
from src.core.l1_fast_loop import cvd_metrics, parse_gate_trade
from src.core.paper_engine import paper_engine
from src.core.risk_engine import risk_engine
from src.core.globals import bot_context
from src.strategy.base import BaseStrategy


class SharkScalpStrategy(BaseStrategy):
    def __init__(self) -> None:
        super().__init__("SharkScalp")
        self._best_bid_sz: Dict[str, float] = {}
        self._best_ask_sz: Dict[str, float] = {}
        self._last_obi: Dict[str, float] = {}
        # 时间序成交流（仅保留 is_buy 标记与 ts）
        self._trade_flags: Dict[str, Deque[Tuple[float, bool]]] = {}
        self._last_fire: Dict[str, float] = {}

    def _deque(self, sym: str) -> Deque[Tuple[float, bool]]:
        if sym not in self._trade_flags:
            self._trade_flags[sym] = deque(maxlen=256)
        return self._trade_flags[sym]

    def _allowed(self, sm: Any) -> bool:
        cfg = config_manager.get_config()
        sc = cfg.shark_scalp
        if not sc.enabled or "shark_scalp" not in cfg.strategy.active_strategies:
            return False
        st = sm.state
        if st == SystemState.LICENSE_LOCKED or st == SystemState.COOL_DOWN:
            return False
        if sc.require_attack_mode and st not in (SystemState.ATTACK, SystemState.BERSERKER):
            return False
        return True

    async def on_tick(self, event: Any) -> None:
        return

    async def on_orderbook(self, event: OrderBookEvent) -> None:
        sm = bot_context.get_state_machine()
        if not self._allowed(sm):
            return
        bids = event.bids or []
        asks = event.asks or []
        if not bids or not asks:
            return
        sym = event.symbol
        try:
            self._best_bid_sz[sym] = float(bids[0][1])
            self._best_ask_sz[sym] = float(asks[0][1])
        except (TypeError, ValueError, IndexError):
            return
        self._last_obi[sym] = float(event.obi)

    def ingest_trades_and_maybe_fire(self, symbol: str, rows: List[Dict[str, Any]], sm: Any) -> None:
        if not self._allowed(sm):
            return
        cfg = config_manager.get_config()
        if symbol not in cfg.strategy.symbols:
            return
        sc = cfg.shark_scalp
        if not sc.long_only:
            return  # 首版仅做多微冲

        now = time.time()
        dq = self._deque(symbol)
        for tr in rows or []:
            if not isinstance(tr, dict):
                continue
            ts, signed = parse_gate_trade(tr)
            is_buy = signed > 0
            dq.append((ts, is_buy))

        win = float(sc.book_window_sec)
        while dq and now - dq[0][0] > win:
            dq.popleft()

        min_n = max(2, int(sc.min_consecutive_taker_buys))
        if len(dq) < min_n:
            return
        tail = list(dq)[-min_n:]
        if any(not x[1] for x in tail):
            return
        if now - tail[0][0] > win:
            return

        bb = float(self._best_bid_sz.get(symbol, 0) or 0)
        ba = float(self._best_ask_sz.get(symbol, 0) or 0)
        if ba < float(sc.min_best_ask_contracts):
            return
        ratio = float(sc.bid_ask_size_ratio_min)
        if bb < ratio * ba:
            return

        pos = paper_engine.positions.get(symbol)
        if pos and float(pos.get("size", 0) or 0) > 1e-12:
            return

        cd = float(sc.signal_cooldown_sec)
        if now - self._last_fire.get(symbol, 0.0) < cd:
            return

        last = float(paper_engine.latest_prices.get(symbol, 0) or 0)
        if last <= 0:
            return

        paper_engine._calculate_pnl()
        eq = float(risk_engine.current_balance or 0.0)
        if eq <= 1e-12:
            eq = max(float(paper_engine.balance), 1e-9)
        risk_cfg = cfg.risk
        if getattr(risk_cfg, "use_equity_tier_margin", True):
            tier_cap = margin_cap_fraction(eq)
            eff_shot_frac = min(float(sc.max_equity_fraction_per_shot), tier_cap)
        else:
            eff_shot_frac = max(float(sc.max_equity_fraction_per_shot), 1e-9)
        max_notional = eq * eff_shot_frac * max(float(sc.leverage), 1.0)
        notional = min(max(float(sc.fixed_notional_usdt), 1.0), max_notional)

        cs = paper_engine.contract_size_for_symbol(symbol)
        contracts = paper_engine.contracts_for_target_usdt_notional(symbol, last, notional)
        if contracts <= 0:
            return

        if sc.scale_tp_sl_to_equity:
            tgt_net, rsk_net = shark_net_tp_sl_usdt(
                eq,
                float(sc.tp_net_equity_fraction),
                float(sc.sl_net_equity_fraction),
            )
            prm = cfg.strategy.params
            thr_atr = float(getattr(prm, "core_high_atr_threshold", 0.01) or 0)
            ap = float(risk_engine.symbol_atr_pct.get(symbol, 0.0) or 0.0)
            if thr_atr > 0 and ap >= thr_atr:
                tgt_net *= float(getattr(sc, "high_atr_net_tp_mult", 1.0))
                rsk_net *= float(getattr(sc, "high_atr_net_sl_mult", 1.0))
        else:
            tgt_net, rsk_net = float(sc.target_net_usdt), float(sc.risk_net_usdt)

        taker, maker = paper_engine._fee_rates_for_symbol(symbol)
        n0 = contracts * cs * last
        # 一阶手续费；用 entry 名义估 TP/SL 腿，略保守
        fo, fctp, fcsl = estimate_fees_usdt(
            n0,
            n0,
            n0,
            taker_rate=taker,
            maker_rate=maker,
            tp_as_maker=True,
            sl_as_taker=True,
        )
        tp_px, sl_px = compute_tp_sl_prices_net_usdt(
            "long",
            last,
            contracts,
            cs,
            target_net_usdt=tgt_net,
            risk_net_usdt=rsk_net,
            fee_open_usdt=fo,
            fee_close_tp_usdt=fctp,
            fee_close_sl_usdt=fcsl,
        )
        if not (tp_px > last > sl_px > 0):
            return

        micro = self._build_micro_snapshot(symbol, sc, tail, bb, ba, now)

        self._last_fire[symbol] = now
        self.emit_signal(
            SignalEvent(
                strategy_name=self.name,
                symbol=symbol,
                side="buy",
                order_type="market",
                price=last,
                amount=float(contracts),
                leverage=int(sc.leverage),
                margin_mode="cross",
                entry_context={
                    "strategy": self.name,
                    "shark_scalp": True,
                    "position_silo": "SHARK_SCALP",
                    "take_profit_limit_price": float(tp_px),
                    "stop_loss_limit_price": float(sl_px),
                    "shark_target_net_usdt": float(tgt_net),
                    "shark_risk_net_usdt": float(rsk_net),
                    "shark_fixed_notional_usdt": notional,
                    "shark_micro_snapshot": micro,
                },
            )
        )

    def _build_micro_snapshot(
        self,
        symbol: str,
        sc: Any,
        tail: List[Tuple[float, bool]],
        bb: float,
        ba: float,
        now: float,
    ) -> Dict[str, Any]:
        c10, _c60, base = cvd_metrics(symbol, now)
        return {
            "t": now,
            "bb_sz": bb,
            "ba_sz": ba,
            "bb_ba_ratio": (bb / ba) if ba > 0 else None,
            "obi_top5": self._last_obi.get(symbol),
            "consecutive_taker_buys": len(tail),
            "cvd_10s": c10,
            "cvd_baseline": base,
            "window_sec": float(sc.book_window_sec),
        }

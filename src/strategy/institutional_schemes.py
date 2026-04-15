"""
机构级三套并行方案（需在 strategy.active_strategies 中显式启用）：
1) MicroMaker — 震荡市双边 Post-Only 挂单（纸面 resting_quote 队列）
2) LiquidationSnipe — 插针 + 买盘断层后限价埋伏多
3) FundingSqueeze — 极端负费率 + OBI 埋伏多
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

from src.strategy.base import BaseStrategy
from src.core.events import TickEvent, SignalEvent, OrderBookEvent
from src.core.config_manager import config_manager
from src.core.globals import bot_context
from src.core.paper_engine import paper_engine
from src.core.risk_engine import risk_engine
from src.ai.regime import regime_classifier, MarketRegime
from src.utils.logger import log


def _bid_depth_ratio(bids: list, top: int = 3, deep_lo: int = 3, deep_hi: int = 12) -> float:
    """深档相对前三档总量比例；越小表示下方买盘越薄（断层感）。"""
    if not bids:
        return 1.0
    t = sum(float(bids[i][1]) for i in range(min(top, len(bids))))
    if t < 1e-12:
        return 1.0
    d = sum(
        float(bids[i][1])
        for i in range(min(deep_lo, len(bids)), min(deep_hi, len(bids)))
    )
    return d / t


class MicroMakerStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("MicroMaker")
        self._last_quote_ts: Dict[str, float] = {}

    async def on_tick(self, event: TickEvent):
        return

    async def on_orderbook(self, event: OrderBookEvent):
        cfg = config_manager.get_config().institutional_schemes.micro_maker
        if not cfg.enabled:
            return
        sym = event.symbol
        bids, asks = event.bids, event.asks
        if not bids or not asks:
            return
        bb, ba = float(bids[0][0]), float(asks[0][0])
        if bb <= 0 or ba <= 0 or ba <= bb:
            return
        mid = (bb + ba) / 2.0
        spread_bps = (ba - bb) / mid * 10000.0
        if spread_bps < float(cfg.min_spread_bps):
            return

        if cfg.require_regime_oscillating and regime_classifier.analyze(sym) != MarketRegime.OSCILLATING:
            return

        now = time.time()
        if now - self._last_quote_ts.get(sym, 0.0) < float(cfg.throttle_sec):
            return
        self._last_quote_ts[sym] = now

        paper_engine.cancel_open_makers(sym)

        notional = float(cfg.quote_notional_usd)
        lev = int(cfg.leverage)
        q_buy = notional / bb
        q_sell = notional / ba
        if q_buy <= 0 or q_sell <= 0:
            return

        ctx_base = {
            "scheme": "micro_maker",
            "resting_quote": True,
            "spread_bps": spread_bps,
        }
        self.emit_signal(
            SignalEvent(
                strategy_name=self.name,
                symbol=sym,
                side="buy",
                order_type="limit",
                price=bb,
                amount=q_buy,
                leverage=lev,
                post_only=True,
                margin_mode="cross",
                entry_context=dict(ctx_base),
            )
        )
        self.emit_signal(
            SignalEvent(
                strategy_name=self.name,
                symbol=sym,
                side="sell",
                order_type="limit",
                price=ba,
                amount=q_sell,
                leverage=lev,
                post_only=True,
                margin_mode="cross",
                entry_context=dict(ctx_base),
            )
        )
        log.info(f"[{self.name}] Quote bid@{bb:.6f} ask@{ba:.6f} {sym} spread={spread_bps:.1f}bps")


class LiquidationSnipeStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("LiquidationSnipe")
        self._obi: Dict[str, float] = {}
        self._bids_cache: Dict[str, list] = {}
        self._ohlcv_cache: Dict[str, tuple] = {}
        self._last_fire: Dict[str, float] = {}

    async def on_orderbook(self, event: OrderBookEvent):
        self._obi[event.symbol] = float(event.obi)
        self._bids_cache[event.symbol] = event.bids

    async def on_tick(self, event: TickEvent):
        cfg = config_manager.get_config().institutional_schemes.liquidation_snipe
        if not cfg.enabled:
            return
        sym = event.symbol
        pos = paper_engine.positions.get(sym) or {}
        if float(pos.get("size", 0) or 0) > 0:
            return

        now = time.time()
        if now - self._last_fire.get(sym, 0.0) < float(cfg.cooldown_sec):
            return

        ts, candles = self._ohlcv_cache.get(sym, (0.0, []))
        ex = bot_context.get_exchange()
        if ex and hasattr(ex, "fetch_candlesticks") and (now - ts) >= float(cfg.fetch_throttle_sec):
            try:
                candles = await ex.fetch_candlesticks(
                    sym,
                    interval=cfg.ohlcv_interval,
                    limit=int(cfg.ohlcv_limit),
                )
                self._ohlcv_cache[sym] = (now, candles or [])
            except Exception:
                pass
            ts, candles = self._ohlcv_cache.get(sym, (0.0, []))

        if len(candles) < 4:
            return
        candles.sort(key=lambda x: int(x.get("time", 0) or 0))
        c = candles[-2]
        o = float(c.get("open", 0) or 0)
        h = float(c.get("high", 0) or 0)
        l = float(c.get("low", 0) or 0)
        cl = float(c.get("close", 0) or 0)
        if min(o, h, l, cl) <= 0:
            return
        rng = h - l
        if rng <= 0:
            return
        rng_pct = rng / cl
        if rng_pct < float(cfg.min_range_pct):
            return
        body = abs(cl - o)
        if body / rng > float(cfg.max_body_to_range_ratio):
            return
        if (cl - l) / rng < 0.52:
            return

        bids = self._bids_cache.get(sym) or []
        vac = _bid_depth_ratio(bids)
        if vac > float(cfg.bid_depth_vacuum_ratio):
            return

        off = float(cfg.limit_price_offset_bps) / 10000.0
        lim = l * (1.0 + off)
        last = float(event.ticker.get("last", 0) or 0)
        if last <= 0:
            return
        notional = max(risk_engine.current_balance * 0.02, 30.0) * 3.0
        qty = notional / lim
        if qty <= 0:
            return
        lev = min(int(config_manager.get_config().risk.max_leverage), 10)

        self._last_fire[sym] = now
        self.emit_signal(
            SignalEvent(
                strategy_name=self.name,
                symbol=sym,
                side="buy",
                order_type="limit",
                price=lim,
                amount=qty,
                leverage=lev,
                post_only=True,
                margin_mode="cross",
                entry_context={
                    "scheme": "liquidation_snipe",
                    "resting_quote": True,
                    "obi": self._obi.get(sym, 0.0),
                    "vacuum_ratio": vac,
                    "bar_range_pct": rng_pct,
                },
            )
        )
        log.warning(f"[{self.name}] SNIPE bid@{lim:.6f} {sym} range={rng_pct*100:.2f}% vac={vac:.3f}")


class FundingSqueezeStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("FundingSqueeze")
        self._obi: Dict[str, float] = {}
        self._last_fire: Dict[str, float] = {}

    async def on_orderbook(self, event: OrderBookEvent):
        self._obi[event.symbol] = float(event.obi)

    async def on_tick(self, event: TickEvent):
        cfg = config_manager.get_config().institutional_schemes.funding_squeeze
        if not cfg.enabled:
            return
        sym = event.symbol
        pos = paper_engine.positions.get(sym) or {}
        if float(pos.get("size", 0) or 0) > 0:
            return

        now = time.time()
        if now - self._last_fire.get(sym, 0.0) < float(cfg.cooldown_sec):
            return

        ex = bot_context.get_exchange()
        spec: Dict[str, Any] = {}
        if ex is not None:
            spec = getattr(ex, "contract_specs_cache", {}).get(sym, {}) or {}
        funding = float(spec.get("funding_rate", 0) or 0)
        if funding > float(cfg.funding_rate_below):
            return
        if self._obi.get(sym, 0.0) < float(cfg.min_obi):
            return

        last = float(event.ticker.get("last", 0) or 0)
        if last <= 0:
            return
        off = float(cfg.limit_offset_bps) / 10000.0
        ob = paper_engine.orderbooks_cache.get(sym) or {}
        bids = ob.get("bids") or []
        bid0 = float(bids[0][0]) if bids else last * (1.0 - off)
        lim = bid0 * (1.0 - off * 0.5)
        notional = max(risk_engine.current_balance * 0.025, 40.0) * 2.5
        qty = notional / max(lim, 1e-12)
        if qty <= 0:
            return
        lev = min(int(config_manager.get_config().risk.max_leverage), 12)

        self._last_fire[sym] = now
        self.emit_signal(
            SignalEvent(
                strategy_name=self.name,
                symbol=sym,
                side="buy",
                order_type="limit",
                price=lim,
                amount=qty,
                leverage=lev,
                post_only=True,
                margin_mode="cross",
                entry_context={
                    "scheme": "funding_squeeze",
                    "resting_quote": True,
                    "funding_rate": funding,
                    "obi": self._obi.get(sym, 0.0),
                },
            )
        )
        log.warning(
            f"[{self.name}] FUNDING bid@{lim:.6f} {sym} fr={funding:.6f} obi={self._obi.get(sym, 0.0):.3f}"
        )

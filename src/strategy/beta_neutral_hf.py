from __future__ import annotations

import math
import time
import uuid
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from src.core.config_manager import config_manager
from src.core.events import SignalEvent, TickEvent
from src.core.paper_engine import MAKER_FEE_RATE, TAKER_FEE_RATE, paper_engine
from src.ai.analyzer import ai_context
from src.ai.regime import MarketRegime, regime_classifier
from src.execution.order_types import PairLegIntent, PairOrderIntent
from src.strategy.base import BaseStrategy
from src.utils.logger import log

# Micro ATR floor: effective relative move ≥ 0.0005 (≈5 bps in _micro_atr_bps units).
_BNHF_ATR_FLOOR_BPS = float(0.0005 * 1e4)
_BNHF_MARGIN_MODE = "isolated"


def _last_price(ticker: Dict[str, Any]) -> float:
    for key in ("last", "mark_price", "price", "close", "last_price"):
        try:
            v = float(ticker.get(key, 0.0) or 0.0)
            if v > 0:
                return v
        except Exception:
            pass
    return 0.0


def _mean(xs: List[float]) -> float:
    return sum(xs) / max(len(xs), 1)


def _variance(xs: List[float], mu: Optional[float] = None) -> float:
    if not xs:
        return 0.0
    m = _mean(xs) if mu is None else mu
    return sum((x - m) * (x - m) for x in xs) / max(len(xs), 1)


def _covariance(xs: List[float], ys: List[float], mx: Optional[float] = None, my: Optional[float] = None) -> float:
    n = min(len(xs), len(ys))
    if n <= 0:
        return 0.0
    x2 = xs[-n:]
    y2 = ys[-n:]
    ax = _mean(x2) if mx is None else mx
    ay = _mean(y2) if my is None else my
    return sum((x - ax) * (y - ay) for x, y in zip(x2, y2)) / max(n, 1)


class BetaNeutralHFScalpStrategy(BaseStrategy):
    def __init__(self) -> None:
        super().__init__("BetaNeutralHF")
        self._prices: Dict[str, Deque[float]] = {}
        self._pair_samples: Dict[str, Deque[tuple[float, float]]] = {}
        self._active_pairs: Dict[str, Dict[str, Any]] = {}
        self._last_trade_ts: Dict[str, float] = {}
        self._rearm_ready: Dict[str, bool] = {}
        self._candidate_snapshots: List[Dict[str, Any]] = []
        self._recent_closed: List[Dict[str, Any]] = []
        self._last_anchor_rebalance_ts: float = 0.0
        self._last_diag_log_ts: float = 0.0
        self._anchor_rebalance_inflight: bool = False
        self._anchor_notional_delta: float = 0.0
        self._anchor_deadband_threshold: float = 0.0
        self._anchor_rebalance_suppressed: bool = False
        self._last_expected_tp_vs_cost: float = 0.0
        self._last_anchor_suppressed_debug_ts: float = 0.0
        self._last_anchor_rebalance_log_ts: float = 0.0
        self._last_candidate_by_alt: Dict[str, Dict[str, Any]] = {}

    def log_sync(self, message: str) -> None:
        try:
            import asyncio

            loop = asyncio.get_running_loop()
            loop.create_task(self.log(message))
        except Exception:
            pass

    def _cfg(self) -> Any:
        return config_manager.get_config().beta_neutral_hf

    @staticmethod
    def _canonical_market_symbol(symbol: str) -> str:
        s = str(symbol or "").strip().upper()
        if ":" in s:
            s = s.split(":", 1)[0]
        return s

    def _hedge_symbol(self) -> str:
        return self._canonical_market_symbol(str(self._cfg().anchor_symbol or ""))

    def _is_hedge_leg_symbol(self, symbol: str) -> bool:
        """对冲基准（如 BTC/USDT）绝不能作为 Alpha 主腿扫描或开仓。"""
        return self._canonical_market_symbol(symbol) == self._hedge_symbol()

    def _alpha_symbols(self) -> List[str]:
        cfg = self._cfg()
        hx = self._hedge_symbol()
        out: List[str] = []
        for s in list(cfg.symbols or []):
            if self._canonical_market_symbol(s) == hx:
                continue
            out.append(str(s))
        return out

    def _tracked_symbols(self) -> List[str]:
        cfg = self._cfg()
        anchor = str(cfg.anchor_symbol or "").strip()
        alts = self._alpha_symbols()
        return [anchor, *alts] if anchor else alts

    def _pair_total_margin_budget_usdt(self, cfg: Any) -> float:
        """
        双腿合计可占用初始保证金上限：
        min(Available / max_active_pairs, Equity × max_pair_equity_fraction)
        """
        try:
            paper_engine._calculate_pnl()
        except Exception:
            pass
        avail = max(float(getattr(paper_engine, "available_balance", 0.0) or 0.0), 0.0)
        eq = max(float(getattr(paper_engine, "balance", 0.0) or 0.0), 0.0)
        n = max(1, int(cfg.max_active_pairs))
        by_slots = avail / float(n)
        cap_frac = float(getattr(cfg, "max_pair_equity_fraction", 0.05) or 0.05)
        cap_frac = min(max(cap_frac, 1e-6), 1.0)
        by_equity = eq * cap_frac
        return max(0.0, min(by_slots, by_equity))

    def _compute_pair_alt_notional_usdt(self, cfg: Any, lev: float, beta_abs: float) -> float:
        """
        名义：alt_notional = alt_margin_cfg × lev，且双腿合计初始保证金 ≤ _pair_total_margin_budget。
        近似：margin_alt + margin_anchor ≈ alt_notional/lev + (alt_notional×β)/lev = alt_notional×(1+β)/lev。
        """
        lev = max(float(lev), 1.0)
        beta_abs = max(abs(float(beta_abs)), 0.0)
        budget = self._pair_total_margin_budget_usdt(cfg)
        if budget <= 1e-12:
            return 0.0
        denom = max(1e-9, 1.0 + beta_abs)
        alt_notional_cap = float(budget) * lev / denom
        alt_notional_cfg = float(cfg.pair_margin_usdt) * lev
        return max(0.0, min(alt_notional_cfg, alt_notional_cap))

    def _deque(self, symbol: str) -> Deque[float]:
        cfg = self._cfg()
        maxlen = max(32, int(cfg.lookback_ticks) + 8)
        dq = self._prices.get(symbol)
        if dq is None or dq.maxlen != maxlen:
            dq = deque(list(dq or []), maxlen=maxlen)
            self._prices[symbol] = dq
        return dq

    def _pair_deque(self, alt: str) -> Deque[tuple[float, float]]:
        cfg = self._cfg()
        maxlen = max(32, int(cfg.lookback_ticks) + 8)
        dq = self._pair_samples.get(alt)
        if dq is None or dq.maxlen != maxlen:
            dq = deque(list(dq or []), maxlen=maxlen)
            self._pair_samples[alt] = dq
        return dq

    def _entry_limit_price(self, symbol: str, side: str, last: float) -> float:
        bb, ba = paper_engine._best_bid_ask(symbol)
        if side == "buy":
            return float(bb or last or 0.0)
        return float(ba or last or 0.0)

    def _signal_for_alt(self, alt: str) -> Optional[Dict[str, Any]]:
        cfg = self._cfg()
        if self._is_hedge_leg_symbol(alt):
            return None
        samples = list(self._pair_samples.get(alt) or [])
        if len(samples) < int(cfg.min_points):
            return None
        alts = [p[0] for p in samples if p[0] > 0 and p[1] > 0]
        btcs = [p[1] for p in samples if p[0] > 0 and p[1] > 0]
        n = min(len(alts), len(btcs))
        if n < int(cfg.min_points):
            return None
        alts = alts[-n:]
        btcs = btcs[-n:]
        xs = [math.log(alts[i] / alts[i - 1]) for i in range(1, len(alts)) if alts[i - 1] > 0]
        ys = [math.log(btcs[i] / btcs[i - 1]) for i in range(1, len(btcs)) if btcs[i - 1] > 0]
        n = min(len(xs), len(ys))
        if n < int(cfg.min_points) - 1:
            return None
        xs = xs[-n:]
        ys = ys[-n:]
        my = _mean(ys)
        vy = _variance(ys, my)
        if vy <= 1e-12:
            return None
        mx = _mean(xs)
        beta = _covariance(xs, ys, mx, my) / vy
        if abs(beta) <= 1e-6:
            beta = 1.0
        if abs(beta) > float(cfg.max_beta_abs):
            return None
        vx = _variance(xs, mx)
        corr = _covariance(xs, ys, mx, my) / math.sqrt(max(vx * vy, 1e-12))
        if corr < float(cfg.min_correlation):
            return None
        spread_bps = []
        for ap, bp in zip(alts, btcs):
            if ap <= 0 or bp <= 0:
                continue
            spread_bps.append((math.log(ap) - beta * math.log(bp)) * 1e4)
        if len(spread_bps) < int(cfg.min_points):
            return None
        impulse_bps = [(x - beta * y) * 1e4 for x, y in zip(xs, ys)]
        if not impulse_bps:
            return None
        mu = _mean(spread_bps)
        var = _variance(spread_bps, mu)
        std = math.sqrt(max(var, 0.0))
        impulse_mu = _mean(impulse_bps)
        impulse_var = _variance(impulse_bps, impulse_mu)
        impulse_std = math.sqrt(max(impulse_var, 0.0))
        spread_z = (spread_bps[-1] - mu) / max(std, 1e-9) if std > 0 else 0.0
        impulse_z = (impulse_bps[-1] - impulse_mu) / max(impulse_std, 1e-9) if impulse_std > 0 else 0.0
        min_spread_std = float(getattr(cfg, "min_spread_std_bps", 0.35) or 0.35)
        min_impulse = float(getattr(cfg, "min_impulse_bps", 0.8) or 0.8)
        impulse_mult = float(getattr(cfg, "impulse_zscore_mult", 0.8) or 0.8)
        spread_ready = std >= min_spread_std
        impulse_ready = abs(impulse_bps[-1]) >= min_impulse and abs(impulse_z) >= float(cfg.entry_zscore) * impulse_mult
        if not spread_ready and not impulse_ready:
            return None
        final_z = spread_z if spread_ready else impulse_z
        trigger = "spread" if spread_ready else "impulse"
        return {
            "alt": alt,
            "beta": beta,
            "corr": corr,
            "zscore": final_z,
            "spread_bps": spread_bps[-1],
            "spread_std_bps": std,
            "impulse_bps": impulse_bps[-1],
            "impulse_std_bps": impulse_std,
            "impulse_zscore": impulse_z,
            "trigger": trigger,
        }

    def _raw_relative_snapshot(self, alt: str) -> Optional[Dict[str, Any]]:
        cfg = self._cfg()
        if self._is_hedge_leg_symbol(alt):
            return None
        samples = list(self._pair_samples.get(alt) or [])
        if len(samples) < int(cfg.min_points):
            return None
        alts = [p[0] for p in samples if p[0] > 0 and p[1] > 0]
        btcs = [p[1] for p in samples if p[0] > 0 and p[1] > 0]
        n = min(len(alts), len(btcs))
        if n < int(cfg.min_points):
            return None
        alts = alts[-n:]
        btcs = btcs[-n:]
        xs = [math.log(alts[i] / alts[i - 1]) for i in range(1, len(alts)) if alts[i - 1] > 0]
        ys = [math.log(btcs[i] / btcs[i - 1]) for i in range(1, len(btcs)) if btcs[i - 1] > 0]
        n = min(len(xs), len(ys))
        if n < int(cfg.min_points) - 1:
            return None
        xs = xs[-n:]
        ys = ys[-n:]
        my = _mean(ys)
        vy = _variance(ys, my)
        if vy <= 1e-12:
            return None
        mx = _mean(xs)
        beta = _covariance(xs, ys, mx, my) / vy
        if abs(beta) <= 1e-6:
            beta = 1.0
        if abs(beta) > float(cfg.max_beta_abs):
            return None
        vx = _variance(xs, mx)
        corr = _covariance(xs, ys, mx, my) / math.sqrt(max(vx * vy, 1e-12))
        if corr < float(cfg.min_correlation):
            return None
        impulse_bps = [(x - beta * y) * 1e4 for x, y in zip(xs, ys)]
        if not impulse_bps:
            return None
        return {
            "alt": alt,
            "beta": beta,
            "corr": corr,
            "impulse_bps": float(impulse_bps[-1]),
            "score": abs(float(impulse_bps[-1])) * max(float(corr), 0.0),
        }

    def _cross_sectional_fallback(self) -> List[Dict[str, Any]]:
        cfg = self._cfg()
        anchor = str(cfg.anchor_symbol)
        anchor_px = list(self._prices.get(anchor) or [])
        lookback = max(3, int(getattr(cfg, "cross_section_lookback", 8) or 8))
        if len(anchor_px) < lookback + 1:
            return []
        anchor_now = anchor_px[-1]
        anchor_prev = anchor_px[-1 - lookback]
        if anchor_now <= 0 or anchor_prev <= 0:
            return []
        anchor_move_bps = math.log(anchor_now / anchor_prev) * 1e4
        raws = []
        for alt in self._alpha_symbols():
            px = list(self._prices.get(alt) or [])
            if len(px) < lookback + 1:
                continue
            alt_now = px[-1]
            alt_prev = px[-1 - lookback]
            if alt_now <= 0 or alt_prev <= 0:
                continue
            alt_move_bps = math.log(alt_now / alt_prev) * 1e4
            rel_edge = alt_move_bps - anchor_move_bps
            raws.append(
                {
                    "alt": alt,
                    "beta": 1.0,
                    "corr": 1.0,
                    "impulse_bps": float(rel_edge),
                    "score": abs(float(rel_edge)),
                }
            )
        if len(raws) < 2:
            return []
        raws.sort(key=lambda x: float(x["impulse_bps"]))
        weakest = raws[0]
        strongest = raws[-1]
        edge = float(strongest["impulse_bps"]) - float(weakest["impulse_bps"])
        if edge < float(getattr(cfg, "cross_section_min_edge_bps", 0.6) or 0.6):
            return []
        out = []
        for row in (strongest, weakest):
            out.append(
                {
                    "alt": str(row["alt"]),
                    "beta": float(row["beta"]),
                    "corr": float(row["corr"]),
                    "zscore": max(float(cfg.entry_zscore), 1.0) + 0.01,
                    "spread_bps": 0.0,
                    "spread_std_bps": 0.0,
                    "impulse_bps": float(row["impulse_bps"]),
                    "impulse_zscore": 0.0,
                    "trigger": "cross_section",
                    "score": abs(float(row["impulse_bps"])) * max(float(row["corr"]), 0.0),
                    "status": self._candidate_status(str(row["alt"])),
                    "direction": "short_alt_long_btc" if row is strongest else "long_alt_short_btc",
                    "cross_section_edge_bps": edge,
                }
            )
        return out

    def _signed_position_contracts(self, symbol: str) -> float:
        pos = paper_engine.positions.get(symbol)
        if not pos or float(pos.get("size", 0.0) or 0.0) <= 0:
            return 0.0
        qty = float(pos.get("size", 0.0) or 0.0)
        side = str(pos.get("side", "")).lower()
        return qty if side == "long" else -qty

    def _signed_contract_pnl(self, symbol: str, signed_contracts: float) -> float:
        if abs(signed_contracts) <= 1e-12:
            return 0.0
        pos = paper_engine.positions.get(symbol)
        if not pos:
            return 0.0
        entry = float(pos.get("entry_price", 0.0) or 0.0)
        last = float(paper_engine.latest_prices.get(symbol, entry) or entry or 0.0)
        cs = float(paper_engine._resolve_contract_size(symbol))
        if entry <= 0 or last <= 0 or cs <= 0:
            return 0.0
        if signed_contracts > 0:
            return signed_contracts * cs * (last - entry)
        return abs(signed_contracts) * cs * (entry - last)

    @staticmethod
    def _gate_notional_usdt(symbol: str, contracts: float, execution_price: float) -> float:
        """Gate.io USDT linear: Notional = abs(Contracts) × Contract_Size × Execution_Price."""
        if execution_price <= 0:
            return 0.0
        cs = float(paper_engine._resolve_contract_size(symbol))
        return max(0.0, abs(float(contracts)) * cs * float(execution_price))

    def _gate_round_trip_fee_usdt(
        self,
        alt: str,
        anchor: str,
        alt_contracts: float,
        anchor_contracts: float,
        alt_exec_px: float,
        anchor_exec_px: float,
    ) -> float:
        """
        圆桌手续费：默认 ALT Taker×2 + Anchor Maker×2。
        **BTC 雷达模式**（不下 BTC 单）：anchor 张数为 0 时仅计 ALT 双程 Taker。
        """
        n_alt = self._gate_notional_usdt(alt, alt_contracts, alt_exec_px)
        if abs(float(anchor_contracts)) <= 1e-12:
            return n_alt * float(TAKER_FEE_RATE) * 2.0
        n_anc = self._gate_notional_usdt(anchor, abs(float(anchor_contracts)), anchor_exec_px)
        return n_alt * float(TAKER_FEE_RATE) * 2.0 + n_anc * float(MAKER_FEE_RATE) * 2.0

    def _micro_atr_bps(self, symbol: str) -> float:
        pxs = list(self._prices.get(symbol) or [])
        if len(pxs) < 3:
            return _BNHF_ATR_FLOOR_BPS
        diffs = []
        for i in range(1, len(pxs)):
            p0 = float(pxs[i - 1] or 0.0)
            p1 = float(pxs[i] or 0.0)
            if p0 > 0 and p1 > 0:
                diffs.append(abs(math.log(p1 / p0)) * 1e4)
        if not diffs:
            return _BNHF_ATR_FLOOR_BPS
        recent = diffs[- max(6, min(24, len(diffs))):]
        calculated = _mean(recent)
        # Effective_ATR = max(calculated, 0.0005) in relative terms → bps floor
        return max(float(calculated), _BNHF_ATR_FLOOR_BPS)

    def _matrix_regime_str(self, symbol: str) -> str:
        raw = str((ai_context.get(symbol) or {}).get("matrix_regime") or "STABLE").upper()
        if raw in ("TRENDING_UP", "TRENDING_DOWN"):
            return raw
        return "STABLE"

    def _leg_stance(self, symbol: str, pos: Dict[str, Any]) -> str:
        """scalp=震荡微利+可续杯; ride/counter=单边矩阵下仅影响是否续杯（微利平仓一律执行）。"""
        mr = self._matrix_regime_str(symbol)
        ps = str(pos.get("side", "long")).lower()
        if mr == "STABLE":
            return "scalp"
        if mr == "TRENDING_UP":
            return "ride" if ps == "long" else "counter"
        if mr == "TRENDING_DOWN":
            return "ride" if ps == "short" else "counter"
        return "scalp"

    def _ai_snapshot(self, symbol: str) -> Dict[str, Any]:
        try:
            snap = regime_classifier.snapshot(symbol)
        except Exception:
            snap = {}
        return {
            "regime": str(snap.get("regime", MarketRegime.STABLE.value) or MarketRegime.STABLE.value),
            "suggested_leverage_cap": max(10, int(float(snap.get("suggested_leverage_cap", 10) or 10))),
            "tp_atr_multiplier": max(0.5, min(1.5, float(snap.get("tp_atr_multiplier", 0.8) or 0.8))),
            "sl_atr_multiplier": max(1.0, min(3.0, float(snap.get("sl_atr_multiplier", 1.8) or 1.8))),
            "score": float(snap.get("score", 50.0) or 50.0),
            "reason": str(snap.get("reason", "") or ""),
        }

    def _leg_market_close_fee_usdt(self, symbol: str, pos: Dict[str, Any]) -> float:
        """单腿一次市价平仓的预估 Taker 手续费（名义 × taker）。"""
        sz = abs(float(pos.get("size", 0.0) or 0.0))
        if sz <= 1e-12:
            return 0.0
        px = float(paper_engine.latest_prices.get(symbol, 0.0) or 0.0)
        cs = float(paper_engine._position_contract_size(pos, symbol))
        tk, _ = paper_engine._fee_rates_for_symbol(symbol)
        return float(sz * cs * max(px, 1e-12) * float(tk))

    def _absolute_cost_usdt(self, alt: str, anchor: str, alt_contracts: float, anchor_contracts: float) -> float:
        """当前市价下的圆桌双边 Gate 手续费（无滑点项）；用于持仓内摩擦参考。"""
        pa = float(paper_engine.latest_prices.get(alt, 0.0) or 0.0)
        pb = float(paper_engine.latest_prices.get(anchor, 0.0) or 0.0)
        return self._gate_round_trip_fee_usdt(alt, anchor, float(alt_contracts), float(anchor_contracts), pa, pb)

    def _book_level_price(self, symbol: str, side: str, level_index: int) -> float:
        ob = paper_engine.orderbooks_cache.get(symbol) or {}
        levels = ob.get("asks") if str(side).lower() == "buy" else ob.get("bids")
        try:
            idx = min(max(int(level_index), 0), max(len(levels) - 1, 0))
            return float(levels[idx][0])
        except Exception:
            return float(paper_engine.latest_prices.get(symbol, 0.0) or 0.0)

    def _alt_sniper_price(self, symbol: str, side: str) -> float:
        return self._book_level_price(symbol, side, 1)

    def _anchor_maker_price(self, symbol: str, side: str) -> float:
        return self._book_level_price(symbol, side, 0)

    def _expected_pair_take_profit_usdt(
        self,
        alt: str,
        anchor: str,
        alt_contracts: float,
        anchor_contracts: float,
        alt_entry_px: float,
        anchor_entry_px: float,
        tp_sl: Dict[str, float],
    ) -> float:
        gross_tp_alt = abs(alt_contracts) * float(paper_engine._resolve_contract_size(alt)) * abs(float(tp_sl["alt_tp_price"]) - float(alt_entry_px))
        gross_tp_anchor = abs(anchor_contracts) * float(paper_engine._resolve_contract_size(anchor)) * abs(
            float(tp_sl["anchor_tp_price"]) - float(anchor_entry_px)
        )
        # 仅用于开仓 EV 估计；对冲腿不在 entry_context 挂独立 OCO（见 _pair_order_intent）。
        return gross_tp_alt + gross_tp_anchor

    def _pair_order_intent(
        self,
        *,
        pair_id: str,
        alt: str,
        anchor: str,
        alt_side: str,
        anchor_side: str,
        alt_contracts: float,
        anchor_contracts: float,
        leverage: int,
        ctx: Dict[str, Any],
        tp_sl: Dict[str, float],
        pyramid_add: bool = False,
    ) -> PairOrderIntent:
        alt_contracts = self._quantize_contracts(alt, alt_contracts, allow_zero=True)
        # 锚（BTC）仅雷达：不下单，张数恒 0；beta 仍由开仓侧用于 ALT 名义缩放。
        anchor_amt = 0.0
        _no_anchor_tp_sl_keys = frozenset(
            {
                "take_profit_limit_price",
                "stop_loss_limit_price",
                "dynamic_stop_loss_price",
                "beta_display_take_profit_price",
                "beta_display_stop_loss_price",
            }
        )
        ctx_anchor = {k: v for k, v in ctx.items() if k not in _no_anchor_tp_sl_keys}
        alt_ctx = {
            **ctx,
            "pair_id": pair_id,
            "pair_role": "alt",
            "beta_neutral_hf": True,
            "beta_bypass_playbook": True,
            "route_profile": "bnhf_atomic_market_pair",
            "sniper_pair_atomic": True,
            "bnhf_pair_market_entry": True,
            "beta_hf_infinite_reload": True,
            "beta_hf_btc_radar_only": True,
            "client_oid": f"bnhf-alt-{uuid.uuid4().hex[:14]}",
            "pyramid_add": bool(pyramid_add),
            "take_profit_limit_price": float(tp_sl["alt_tp_price"]),
            "stop_loss_limit_price": float(tp_sl["alt_sl_price"]),
            "dynamic_stop_loss_price": float(tp_sl["alt_sl_price"]),
        }
        anchor_ctx = {
            **ctx_anchor,
            "pair_id": pair_id,
            "pair_role": "anchor",
            "beta_neutral_hf": True,
            "beta_anchor_pair_managed": True,
            "beta_bypass_playbook": True,
            "route_profile": "bnhf_atomic_market_pair",
            "sniper_pair_atomic": True,
            "bnhf_pair_market_entry": True,
            "beta_hf_infinite_reload": True,
            "beta_hf_btc_radar_only": True,
            "client_oid": f"bnhf-hedge-{uuid.uuid4().hex[:12]}",
            "pyramid_add": bool(pyramid_add),
        }
        return PairOrderIntent(
            pair_id=pair_id,
            strategy_name=self.name,
            alt_leg=PairLegIntent(
                symbol=alt,
                side=alt_side,
                order_type="market",
                amount=float(alt_contracts),
                price=None,
                leverage=int(leverage),
                margin_mode=_BNHF_MARGIN_MODE,
                post_only=False,
                entry_context=alt_ctx,
            ),
            anchor_leg=PairLegIntent(
                symbol=anchor,
                side=anchor_side,
                order_type="market",
                amount=float(anchor_amt),
                price=None,
                leverage=int(leverage),
                margin_mode=_BNHF_MARGIN_MODE,
                post_only=False,
                entry_context=anchor_ctx,
            ),
            panic_close_on_partial=False,
            maker_grace_ms=max(50, int(float(self._cfg().hedge_grace_sec) * 1000.0)),
        )

    def _dynamic_pair_tp_sl(self, alt: str, anchor: str, alt_side: str, anchor_side: str, sig: Dict[str, Any]) -> Dict[str, float]:
        """
        仅作开仓前展示/预检用的宽 TP 与极宽 SL 占位；**策略不对任何腿下达常规止损**。
        平仓与续杯仅由独立腿微利逻辑驱动。
        """
        alt_ai = self._ai_snapshot(alt)
        anchor_ai = self._ai_snapshot(anchor)
        pair_atr_bps = max(self._micro_atr_bps(alt), self._micro_atr_bps(anchor))
        tp_mult = min(float(alt_ai["tp_atr_multiplier"]), float(anchor_ai["tp_atr_multiplier"]))
        tp_bps = max(0.8, pair_atr_bps * tp_mult)
        sl_bps = 8000.0
        alt_last = float(paper_engine.latest_prices.get(alt, 0.0) or 0.0)
        anchor_last = float(paper_engine.latest_prices.get(anchor, 0.0) or 0.0)
        if alt_side == "buy":
            alt_tp = alt_last * (1.0 + tp_bps / 1e4)
            alt_sl = alt_last * (1.0 - sl_bps / 1e4)
        else:
            alt_tp = alt_last * (1.0 - tp_bps / 1e4)
            alt_sl = alt_last * (1.0 + sl_bps / 1e4)
        if anchor_side == "buy":
            anchor_tp = anchor_last * (1.0 + tp_bps / 1e4)
            anchor_sl = anchor_last * (1.0 - sl_bps / 1e4)
        else:
            anchor_tp = anchor_last * (1.0 - tp_bps / 1e4)
            anchor_sl = anchor_last * (1.0 + sl_bps / 1e4)
        return {
            "pair_atr_bps": float(pair_atr_bps),
            "tp_bps": float(tp_bps),
            "sl_bps": float(sl_bps),
            "alt_tp_price": float(alt_tp),
            "alt_sl_price": float(alt_sl),
            "anchor_tp_price": float(anchor_tp),
            "anchor_sl_price": float(anchor_sl),
        }

    def _symbol_exchange_leverage_max(self, symbol: str) -> int:
        """Gate 合约 physics 里的该品种最大杠杆；无同步值时回落到 pair_leverage。"""
        cfg = self._cfg()
        fb = max(10, int(getattr(cfg, "pair_leverage", 10) or 10))
        try:
            sp = paper_engine._exchange_physics_specs(symbol) or {}
            v = int(float(sp.get("leverage_max") or 0))
            return max(1, v) if v > 0 else fb
        except Exception:
            return fb

    def _group_entry_leverage(self, alt_syms: List[str]) -> int:
        """
        同一组同时开仓时统一杠杆：取组内各 ALT 与锚的「交易所上限」中的最小值，
        保证每个品种都不超自身上限，且组内倍数一致。
        """
        cfg = self._cfg()
        anchor = str(cfg.anchor_symbol)
        if str(getattr(cfg, "entry_leverage_mode", "exchange_group_min_max") or "").strip().lower() == "dynamic":
            levs_dyn: List[int] = []
            for raw in list(alt_syms or []):
                if self._is_hedge_leg_symbol(str(raw)):
                    continue
                a = self._canonical_market_symbol(str(raw))
                levs_dyn.append(self._effective_pair_leverage(a, anchor, None))
            return max(1, min(levs_dyn)) if levs_dyn else max(10, int(getattr(cfg, "pair_leverage", 10) or 10))
        caps: List[int] = []
        for raw in list(alt_syms or []):
            if self._is_hedge_leg_symbol(str(raw)):
                continue
            caps.append(self._symbol_exchange_leverage_max(self._canonical_market_symbol(str(raw))))
        caps.append(self._symbol_exchange_leverage_max(anchor))
        return max(1, min(caps)) if caps else max(10, int(cfg.pair_leverage))

    def _resolve_open_leverage(
        self,
        alt: str,
        anchor: str,
        sig: Optional[Dict[str, Any]],
        forced: Optional[int],
    ) -> int:
        if forced is not None:
            return max(1, int(forced))
        cfg = self._cfg()
        if str(getattr(cfg, "entry_leverage_mode", "exchange_group_min_max") or "").strip().lower() == "dynamic":
            return self._effective_pair_leverage(alt, anchor, sig)
        return self._group_entry_leverage([alt])

    def _effective_pair_leverage(self, alt: str, anchor: str, sig: Optional[Dict[str, Any]] = None) -> int:
        cfg = self._cfg()
        base = max(10, int(cfg.pair_leverage))
        ai_alt = self._ai_snapshot(alt)
        ai_anchor = self._ai_snapshot(anchor)
        ai_cap = max(10, min(int(ai_alt["suggested_leverage_cap"]), int(ai_anchor["suggested_leverage_cap"])))
        strength = 1.0
        if sig:
            try:
                trigger = str(sig.get("trigger") or "")
                z_strength = abs(float(sig.get("zscore", 0.0) or 0.0)) / max(float(cfg.entry_zscore), 1e-6)
                if trigger == "cross_section":
                    edge = abs(float(sig.get("cross_section_edge_bps", 0.0) or 0.0))
                    strength = max(z_strength, edge / max(float(getattr(cfg, "cross_section_min_edge_bps", 0.2) or 0.2), 1e-6))
                elif trigger == "impulse":
                    edge = abs(float(sig.get("impulse_bps", 0.0) or 0.0))
                    strength = max(z_strength, edge / max(float(getattr(cfg, "min_impulse_bps", 0.35) or 0.35), 1e-6))
                else:
                    edge = abs(float(sig.get("spread_bps", 0.0) or 0.0))
                    std = max(float(sig.get("spread_std_bps", 0.0) or 0.0), 1e-6)
                    strength = max(z_strength, edge / std)
            except Exception:
                strength = 1.0
        regime = str(ai_alt.get("regime", MarketRegime.STABLE.value) or MarketRegime.STABLE.value)
        hard_cap = ai_cap
        try:
            alt_specs = paper_engine._exchange_physics_specs(alt) or {}
            anchor_specs = paper_engine._exchange_physics_specs(anchor) or {}
            alt_cap = int(float(alt_specs.get("leverage_max") or hard_cap))
            anchor_cap = int(float(anchor_specs.get("leverage_max") or hard_cap))
            hard_cap = min(hard_cap, alt_cap, anchor_cap)
        except Exception:
            pass
        base_target = max(10, min(base, hard_cap))
        floor_lev = max(10, int(round(base_target * 0.6)))
        strength_norm = min(1.0, max(0.0, (float(strength) - 1.0) / 1.5))
        regime_mult = 0.85 if regime == MarketRegime.VOLATILE.value else 1.0
        strength_boost = 0.90 + 0.20 * strength_norm
        target = int(round(base_target * regime_mult * strength_boost))
        return max(1, min(max(target, floor_lev), hard_cap))

    def _quantize_contracts(self, symbol: str, contracts: float, *, allow_zero: bool = True) -> float:
        amt = abs(float(contracts or 0.0))
        if amt <= 1e-12:
            return 0.0
        specs = None
        try:
            specs = paper_engine._exchange_physics_specs(symbol)
        except Exception:
            specs = None
        order_min = 0.0
        if specs:
            try:
                order_min = float(specs.get("order_size_min") or 0.0)
            except Exception:
                order_min = 0.0
            if not bool(specs.get("enable_decimal", False)):
                q = float(math.floor(amt + 1e-12))
                if q < max(order_min, 1.0):
                    return 0.0 if allow_zero else float(max(order_min, 1.0))
                return q
        step = order_min if order_min > 0 else 0.0
        if step > 0:
            q = math.floor((amt + 1e-12) / step) * step
            if q + 1e-12 < step:
                return 0.0 if allow_zero else float(step)
            return float(q)
        return float(amt)

    def _quantize_signed_contracts(self, symbol: str, contracts: float, *, allow_zero: bool = True) -> float:
        q = self._quantize_contracts(symbol, abs(float(contracts or 0.0)), allow_zero=allow_zero)
        if q <= 0:
            return 0.0
        return math.copysign(q, float(contracts))

    def _emit_leg(
        self,
        *,
        pair_id: str,
        symbol: str,
        side: str,
        price: float,
        amount: float,
        leverage: int,
        reduce_only: bool,
        post_only: bool = False,
        role: str,
        ctx_extra: Dict[str, Any],
    ) -> None:
        cfg = self._cfg()
        quant_amount = self._quantize_contracts(symbol, amount, allow_zero=True)
        if quant_amount <= 0:
            return
        entry_ctx = {
            "pair_id": pair_id,
            "pair_role": role,
            "beta_neutral_hf": True,
            **ctx_extra,
        }
        aggressive_open = not reduce_only and (
            role in {"alt", "anchor_net"} or bool(ctx_extra.get("beta_hf_instant_reload"))
        )
        if aggressive_open and not post_only:
            entry_ctx["beta_bypass_playbook"] = True
        if post_only:
            order_type = "limit"
        else:
            order_type = (
                "market"
                if aggressive_open
                else str(cfg.exit_order_type if reduce_only else cfg.entry_order_type).lower()
            )
        post_only = bool(post_only or (order_type == "limit" and not reduce_only))
        if order_type == "limit":
            entry_ctx["resting_quote"] = True
            entry_ctx["client_oid"] = f"bnhf-{uuid.uuid4().hex[:18]}"
            if post_only:
                entry_ctx.pop("core_limit_requote_enabled", None)
                entry_ctx.pop("core_limit_ttl_ms", None)
                entry_ctx.pop("core_limit_requote_max", None)
                entry_ctx.pop("paper_shadow_limit", None)
            else:
                entry_ctx["core_limit_requote_enabled"] = True
                entry_ctx["core_limit_ttl_ms"] = int(cfg.entry_limit_ttl_ms)
                entry_ctx["core_limit_requote_max"] = int(cfg.entry_limit_requote_max)
                entry_ctx["paper_shadow_limit"] = True
        else:
            entry_ctx.pop("resting_quote", None)
            entry_ctx.pop("paper_shadow_limit", None)
        self.emit_signal(
            SignalEvent(
                strategy_name=self.name,
                symbol=symbol,
                side=side,
                order_type=order_type,
                price=float(price or 0.0),
                amount=float(quant_amount),
                leverage=int(leverage),
                reduce_only=bool(reduce_only),
                post_only=bool(post_only),
                margin_mode=_BNHF_MARGIN_MODE,
                entry_context=entry_ctx,
            )
        )

    def _record_closed(self, pair: Dict[str, Any], reason: str, net_pnl: float) -> None:
        cfg = self._cfg()
        item = {
            "pair_id": str(pair["id"]),
            "alt": str(pair["alt"]),
            "anchor": str(pair["anchor"]),
            "entry_zscore": float(pair.get("entry_zscore", 0.0) or 0.0),
            "beta": float(pair.get("beta", 0.0) or 0.0),
            "reason": str(reason),
            "net_pnl": float(net_pnl),
            "closed_at": time.time(),
        }
        self._recent_closed = [item, *self._recent_closed][: int(cfg.closed_history_limit)]

    def _maybe_pyramid_pair(self, pair: Dict[str, Any], sig: Optional[Dict[str, Any]], now: float) -> bool:
        # Strict 1:1 — no post-entry adds; hedge follows only the initial PairOrderIntent.
        return False

    def _candidate_status(self, alt: str) -> str:
        pair = self._active_pairs.get(alt)
        if pair:
            return str(pair.get("status", "open"))
        return "idle"

    def _refresh_candidates(self) -> List[Dict[str, Any]]:
        cfg = self._cfg()
        out: List[Dict[str, Any]] = []
        diagnostics = {"insufficient_samples": 0, "flat": 0, "live_candidates": 0, "entry_ready": 0, "impulse_ready": 0, "cross_section_ready": 0}
        for alt in self._alpha_symbols():
            sig = self._signal_for_alt(alt)
            if not sig:
                samples = len(self._pair_samples.get(alt) or [])
                if samples < int(cfg.min_points):
                    diagnostics["insufficient_samples"] += 1
                else:
                    diagnostics["flat"] += 1
                continue
            z = float(sig["zscore"])
            diagnostics["live_candidates"] += 1
            if str(sig.get("trigger", "")) == "impulse":
                diagnostics["impulse_ready"] += 1
            if abs(z) >= float(cfg.entry_zscore):
                diagnostics["entry_ready"] += 1
            out.append(
                {
                    "alt": alt,
                    "zscore": z,
                    "beta": float(sig["beta"]),
                    "corr": float(sig["corr"]),
                    "spread_bps": float(sig.get("spread_bps", 0.0) or 0.0),
                    "spread_std_bps": float(sig.get("spread_std_bps", 0.0) or 0.0),
                    "impulse_bps": float(sig.get("impulse_bps", 0.0) or 0.0),
                    "impulse_zscore": float(sig.get("impulse_zscore", 0.0) or 0.0),
                    "trigger": str(sig.get("trigger", "")),
                    "score": abs(z) * max(float(sig["corr"]), 0.0),
                    "status": self._candidate_status(alt),
                    "direction": "short_alt_long_btc" if z > 0 else "long_alt_short_btc",
                }
            )
            if abs(z) <= float(cfg.rearm_zscore):
                self._rearm_ready[alt] = True
        out.sort(key=lambda x: (float(x["score"]), abs(float(x["zscore"]))), reverse=True)
        if not out:
            out = self._cross_sectional_fallback()
            if out:
                diagnostics["live_candidates"] = len(out)
                diagnostics["entry_ready"] = len(out)
                diagnostics["cross_section_ready"] = len(out)
        self._candidate_snapshots = out[: int(cfg.candidate_limit_ui)]
        now = time.time()
        if now - self._last_diag_log_ts >= 20.0:
            self._last_diag_log_ts = now
            top = self._candidate_snapshots[:3]
            print_top = ", ".join(
                f"{row['alt']} {row['trigger']} z={float(row['zscore']):.2f} "
                f"spr={float(row['spread_bps']):.2f}bps imp={float(row['impulse_bps']):.2f}bps "
                f"beta={float(row['beta']):.2f} corr={float(row['corr']):.2f}"
                for row in top
            ) or "none"
            self.log_sync(
                f"[BetaNeutralHF] candidates={len(out)} active={len(self._active_pairs)} "
                f"diag={diagnostics} top={print_top}"
            )
        if float(getattr(cfg, "cooldown_sec", 12.0) or 0.0) <= 0.0:
            for sym in self._alpha_symbols():
                if sym not in self._active_pairs:
                    self._rearm_ready[sym] = True
        self._last_candidate_by_alt = {str(r["alt"]): r for r in out}
        return out

    def _entry_signal_for_alt(self, alt: str) -> Optional[Dict[str, Any]]:
        """与 _refresh_candidates 产出一致；含截面 fallback 行。"""
        cfg = self._cfg()
        row = self._last_candidate_by_alt.get(str(alt))
        if not row:
            return None
        if abs(float(row["zscore"])) < float(cfg.entry_zscore):
            return None
        return dict(row)

    def _pair_entry_feasible(self, sig: Dict[str, Any], *, forced_leverage: Optional[int] = None) -> bool:
        """_open_pair 在发单前的检查（不落库、不发单），用于同 tick 组开多腿预检。"""
        cfg = self._cfg()
        alt = str(sig["alt"])
        if self._is_hedge_leg_symbol(alt):
            return False
        if alt in self._active_pairs:
            return False
        if len(self._active_pairs) >= int(cfg.max_active_pairs):
            return False
        if not self._rearm_ready.get(alt, True):
            return False
        alt_last = float(paper_engine.latest_prices.get(alt, 0.0) or 0.0)
        anchor = str(cfg.anchor_symbol)
        anchor_last = float(paper_engine.latest_prices.get(anchor, 0.0) or 0.0)
        if alt_last <= 0 or anchor_last <= 0:
            return False
        z = float(sig["zscore"])
        beta_abs = abs(float(sig["beta"]))
        direction = str(sig.get("direction") or "")
        if direction == "short_alt_long_btc":
            alt_side, anchor_side = "sell", "buy"
        elif direction == "long_alt_short_btc":
            alt_side, anchor_side = "buy", "sell"
        else:
            alt_side = "sell" if z > 0 else "buy"
            anchor_side = "buy" if alt_side == "sell" else "sell"
        lev = self._resolve_open_leverage(alt, anchor, sig, forced_leverage)
        alt_notional = self._compute_pair_alt_notional_usdt(cfg, float(lev), beta_abs)
        if alt_notional <= 0:
            return False
        alt_contracts = self._quantize_contracts(
            alt, paper_engine.contracts_for_target_usdt_notional(alt, alt_last, alt_notional), allow_zero=True
        )
        if alt_contracts <= 0:
            return False
        tp_sl = self._dynamic_pair_tp_sl(alt, anchor, alt_side, anchor_side, sig)
        alt_entry_px = self._alt_sniper_price(alt, alt_side)
        anchor_entry_px = self._anchor_maker_price(anchor, anchor_side)
        round_trip = self._gate_round_trip_fee_usdt(
            alt, anchor, float(alt_contracts), 0.0, float(alt_entry_px), float(anchor_entry_px)
        )
        rt_mult = float(getattr(cfg, "entry_ev_round_trip_mult", 0.22) or 0.22)
        ev_need = round_trip * max(1e-6, rt_mult)
        expected_take_profit_usdt = self._expected_pair_take_profit_usdt(
            alt, anchor, alt_contracts, 0.0, alt_entry_px, anchor_entry_px, tp_sl
        )
        return bool(expected_take_profit_usdt > ev_need)

    def _open_pair(self, sig: Dict[str, Any], forced_leverage: Optional[int] = None) -> None:
        cfg = self._cfg()
        alt = str(sig["alt"])
        if self._is_hedge_leg_symbol(alt):
            return
        if alt in self._active_pairs:
            return
        if len(self._active_pairs) >= int(cfg.max_active_pairs):
            return
        now = time.time()
        if not self._rearm_ready.get(alt, True):
            return
        alt_last = float(paper_engine.latest_prices.get(alt, 0.0) or 0.0)
        anchor = str(cfg.anchor_symbol)
        anchor_last = float(paper_engine.latest_prices.get(anchor, 0.0) or 0.0)
        if alt_last <= 0 or anchor_last <= 0:
            return
        z = float(sig["zscore"])
        beta_abs = abs(float(sig["beta"]))
        direction = str(sig.get("direction") or "")
        if direction == "short_alt_long_btc":
            alt_side, anchor_side = "sell", "buy"
        elif direction == "long_alt_short_btc":
            alt_side, anchor_side = "buy", "sell"
        else:
            alt_side = "sell" if z > 0 else "buy"
            anchor_side = "buy" if alt_side == "sell" else "sell"
        lev = self._resolve_open_leverage(alt, anchor, sig, forced_leverage)
        alt_notional = self._compute_pair_alt_notional_usdt(cfg, float(lev), beta_abs)
        if alt_notional <= 0:
            return
        alt_contracts = self._quantize_contracts(
            alt, paper_engine.contracts_for_target_usdt_notional(alt, alt_last, alt_notional), allow_zero=True
        )
        if alt_contracts <= 0:
            return
        tp_sl = self._dynamic_pair_tp_sl(alt, anchor, alt_side, anchor_side, sig)
        alt_entry_px = self._alt_sniper_price(alt, alt_side)
        anchor_entry_px = self._anchor_maker_price(anchor, anchor_side)
        # 雷达模式：不对 BTC 下真实单，手续费/EV 仅 ALT 双程 Taker。
        round_trip = self._gate_round_trip_fee_usdt(
            alt, anchor, float(alt_contracts), 0.0, float(alt_entry_px), float(anchor_entry_px)
        )
        rt_mult = float(getattr(cfg, "entry_ev_round_trip_mult", 0.22) or 0.22)
        ev_need = round_trip * max(1e-6, rt_mult)
        expected_take_profit_usdt = self._expected_pair_take_profit_usdt(
            alt,
            anchor,
            alt_contracts,
            0.0,
            alt_entry_px,
            anchor_entry_px,
            tp_sl,
        )
        self._last_expected_tp_vs_cost = float(expected_take_profit_usdt / max(ev_need, 1e-12))
        if expected_take_profit_usdt <= ev_need:
            log.info(
                f"[Signal Blocked] Symbol: {alt}, Expected PnL: {expected_take_profit_usdt:.4f} USDT, "
                f"Gate Round-Trip Fee: {round_trip:.4f} USDT. (Need EV > {ev_need:.4f} = fee×{rt_mult:.3f})"
            )
            return
        absolute_cost = round_trip
        lev_f = max(float(lev), 1.0)
        pair_alloc = self._gate_notional_usdt(alt, float(alt_contracts), float(alt_entry_px)) / lev_f
        pair_id = f"bnhf-{alt.replace('/', '').replace(':', '')}-{int(now * 1000)}"
        pair = {
            "id": pair_id,
            "alt": alt,
            "anchor": anchor,
            "status": "pending_entry",
            "created_ts": now,
            "opened_ts": now,
            "entry_zscore": float(z),
            "beta": float(sig["beta"]),
            "corr": float(sig["corr"]),
            "effective_leverage": int(lev),
            "ai_regime": self._ai_snapshot(alt).get("regime"),
            "alt_side": alt_side,
            "anchor_side": anchor_side,
            "unit_alt_contracts": float(alt_contracts),
            "unit_anchor_contracts": 0.0,
            "alt_contracts": float(alt_contracts),
            "anchor_target_contracts": 0.0,
            "alt_entry_ref_price": float(alt_entry_px),
            "anchor_entry_ref_price": float(anchor_entry_px),
            "anchor_armed": False,
            "absolute_cost_usdt": float(absolute_cost),
            "expected_take_profit_usdt": float(expected_take_profit_usdt),
            "tp_bps": float(tp_sl["tp_bps"]),
            "sl_bps": float(tp_sl["sl_bps"]),
            "pair_atr_bps": float(tp_sl["pair_atr_bps"]),
            "pair_allocated_margin_usdt": float(pair_alloc),
            "close_reason": "",
            "_bnhf_leg_reload_until": {},
        }
        ctx = {
            "beta_anchor_symbol": anchor,
            "beta_hedge_beta": float(sig["beta"]),
            "beta_corr": float(sig["corr"]),
            "beta_entry_zscore": float(z),
            "beta_pair_alt": alt,
            "take_profit_limit_price": float(tp_sl["alt_tp_price"]),
            "stop_loss_limit_price": float(tp_sl["alt_sl_price"]),
            "dynamic_stop_loss_price": float(tp_sl["alt_sl_price"]),
            "beta_expected_take_profit_usdt": float(expected_take_profit_usdt),
            "beta_absolute_cost_usdt": float(absolute_cost),
            "beta_expected_edge_ratio": float(expected_take_profit_usdt / max(absolute_cost, 1e-9)),
            "beta_micro_atr_bps": float(tp_sl["pair_atr_bps"]),
            "beta_tp_atr_multiplier": float(tp_sl["tp_bps"] / max(tp_sl["pair_atr_bps"], 1e-9)),
            "beta_sl_atr_multiplier": float(tp_sl["sl_bps"] / max(tp_sl["pair_atr_bps"], 1e-9)),
        }
        pair_intent = self._pair_order_intent(
            pair_id=pair_id,
            alt=alt,
            anchor=anchor,
            alt_side=alt_side,
            anchor_side=anchor_side,
            alt_contracts=float(alt_contracts),
            anchor_contracts=0.0,
            leverage=lev,
            ctx=ctx,
            tp_sl=tp_sl,
        )
        self.emit_signal(
            SignalEvent(
                strategy_name=self.name,
                symbol=alt,
                side=alt_side,
                order_type="limit",
                price=float(alt_entry_px),
                amount=float(alt_contracts),
                leverage=int(lev),
                reduce_only=False,
                post_only=False,
                margin_mode=_BNHF_MARGIN_MODE,
                entry_context={"beta_neutral_hf": True, "pair_order_intent": pair_intent.to_dict(), **ctx},
            )
        )
        self._active_pairs[alt] = pair
        self._last_trade_ts[alt] = now
        self._rearm_ready[alt] = False
        self.log_sync(
            f"[BetaNeutralHF] OPEN {alt} z={z:.2f} beta={float(sig['beta']):.2f} "
            f"corr={float(sig['corr']):.2f} lev={lev} alt={alt_side}@{float(pair['alt_contracts']):.4f} "
            f"(BTC radar only — no BTC orders; ALT micro/reload + matrix on ALT)"
        )

    def _emit_micro_take_and_reload(
        self,
        pair: Dict[str, Any],
        symbol: str,
        pos: Dict[str, Any],
        *,
        reason: str,
        allow_reload: bool = True,
    ) -> None:
        """单腿微利市价平仓；震荡模式 allow_reload 则同向同量立即续杯。"""
        cfg = self._cfg()
        amt = float(pos.get("size", 0.0) or 0.0)
        if amt <= 1e-12:
            return
        pos_side = str(pos.get("side", "long")).lower()
        close_side = "sell" if pos_side == "long" else "buy"
        open_side = "buy" if pos_side == "long" else "sell"
        lev = int(pos.get("leverage", pair.get("effective_leverage", cfg.pair_leverage)) or cfg.pair_leverage)
        pid = str(pair.get("id", ""))
        alt_sym = str(pair["alt"])
        role = "alt" if symbol == alt_sym else "anchor"
        ctx_close = {
            "beta_hf_instant_reload": True,
            "beta_hf_reload_phase": "close",
            "exit_reason": reason,
            "beta_pair_alt": alt_sym,
        }
        ctx_open = {
            "beta_hf_instant_reload": True,
            "beta_hf_reload_phase": "open",
            "beta_pair_alt": alt_sym,
        }
        self._emit_leg(
            pair_id=pid,
            symbol=symbol,
            side=close_side,
            price=0.0,
            amount=amt,
            leverage=lev,
            reduce_only=True,
            role=role,
            ctx_extra=ctx_close,
        )
        if not allow_reload or not bool(getattr(cfg, "instant_reload_enabled", True)):
            return
        self._emit_leg(
            pair_id=pid,
            symbol=symbol,
            side=open_side,
            price=0.0,
            amount=amt,
            leverage=lev,
            reduce_only=False,
            role=role,
            ctx_extra=ctx_open,
        )

    def _pair_net_pnl(self, pair: Dict[str, Any]) -> float:
        alt = str(pair["alt"])
        anchor = str(pair["anchor"])
        alt_pos = paper_engine.positions.get(alt)
        alt_pnl = float(alt_pos.get("unrealized_pnl", 0.0) or 0.0) if alt_pos else 0.0
        anchor_pos = paper_engine.positions.get(anchor)
        if anchor_pos and float(anchor_pos.get("size", 0.0) or 0.0) > 1e-12:
            anchor_pnl = float(anchor_pos.get("unrealized_pnl", 0.0) or 0.0)
        else:
            anchor_pnl = self._signed_contract_pnl(anchor, float(pair.get("anchor_target_contracts", 0.0) or 0.0))
        return alt_pnl + anchor_pnl

    def _estimate_pair_close_fees_usdt(self, pair: Dict[str, Any]) -> float:
        """
        按 paper_engine 同一口径预估双腿市价平仓 Taker 费：sum(|张|×cs×价×taker)。
        与 execute_order 中 Fee = Nominal×Rate 一致，不乘杠杆。
        """
        alt = str(pair["alt"])
        anchor = str(pair["anchor"])
        pos_alt = paper_engine.positions.get(alt)
        pos_ac = paper_engine.positions.get(anchor)
        alt_sz = float((pos_alt or {}).get("size", 0.0) or 0.0)
        anchor_sz = abs(float((pos_ac or {}).get("size", 0.0) or 0.0))
        if anchor_sz <= 1e-12:
            anchor_sz = abs(float(pair.get("anchor_target_contracts", 0.0) or 0.0))
        px_alt = float(paper_engine.latest_prices.get(alt, 0.0) or 0.0)
        px_ac = float(paper_engine.latest_prices.get(anchor, 0.0) or 0.0)
        cs_a = float(paper_engine._position_contract_size(pos_alt, alt) if pos_alt else paper_engine._resolve_contract_size(alt))
        cs_c = float(paper_engine._position_contract_size(pos_ac, anchor) if pos_ac else paper_engine._resolve_contract_size(anchor))
        tk_a, _ = paper_engine._fee_rates_for_symbol(alt)
        tk_c, _ = paper_engine._fee_rates_for_symbol(anchor)
        n_alt = abs(alt_sz) * cs_a * max(px_alt, 1e-12)
        n_ac = anchor_sz * cs_c * max(px_ac, 1e-12)
        return float(n_alt * float(tk_a) + n_ac * float(tk_c))

    def _sync_active_pair_display_tp_sl(
        self, pair: Dict[str, Any], sig: Optional[Dict[str, Any]]
    ) -> None:
        """仅同步微利 TP 参考价；不在面板展示微观止损（策略无主动止损）。"""
        alt = str(pair["alt"])
        anchor = str(pair["anchor"])
        alt_side = str(pair.get("alt_side", ""))
        anchor_side = str(pair.get("anchor_side", ""))
        if not alt_side or not anchor_side:
            return
        tp_sl = self._dynamic_pair_tp_sl(alt, anchor, alt_side, anchor_side, dict(sig or {}))
        pair["display_alt_tp_price"] = float(tp_sl["alt_tp_price"])
        pair["display_alt_sl_price"] = 0.0
        pair["display_anchor_tp_price"] = 0.0
        pair["display_anchor_sl_price"] = 0.0
        try:
            paper_engine.sync_beta_hf_display_tp_sl(alt, float(tp_sl["alt_tp_price"]), 0.0)
            paper_engine.clear_beta_hf_symbol_tp_sl(anchor)
        except Exception:
            pass

    def _manage_pairs(self) -> None:
        cfg = self._cfg()
        now = time.time()
        to_delete: List[str] = []
        micro_u = float(getattr(cfg, "leg_micro_take_usdt", 1.0) or 1.0)
        cool = max(0.05, float(getattr(cfg, "reload_cooldown_sec", 0.35) or 0.35))
        for alt, pair in list(self._active_pairs.items()):
            alt_sym = str(pair["alt"])
            pos_alt = paper_engine.positions.get(alt_sym)
            alt_open = bool(pos_alt and float(pos_alt.get("size", 0.0) or 0.0) > 0)
            if pair["status"] == "pending_entry":
                if alt_open:
                    pair["status"] = "open"
                    pair["opened_ts"] = now
                    pair["anchor_armed"] = True
                elif now - float(pair.get("created_ts", now) or now) > max(1.0, float(cfg.hedge_grace_sec) * 2.0):
                    to_delete.append(alt)
                continue
            if pair["status"] != "open":
                continue
            if not alt_open:
                self._record_closed(pair, "beta_alt_flat", 0.0)
                to_delete.append(alt)
                continue
            pair["alt_contracts"] = float(pos_alt.get("size", 0.0) or 0.0) if pos_alt else float(pair.get("alt_contracts", 0.0) or 0.0)
            lock = pair.setdefault("_bnhf_leg_reload_until", {})
            for sym, pos in ((alt_sym, pos_alt),):
                if self._is_hedge_leg_symbol(sym):
                    continue
                if not pos or float(pos.get("size", 0.0) or 0.0) <= 1e-12:
                    continue
                stance = self._leg_stance(sym, pos)
                # 顺势 ride 不再「只挂追踪不平仓」：净利达标一样秒平；仅 scalp 允许续杯
                paper_engine.clear_high_conviction_trailing(sym)
                if now < float(lock.get(sym, 0.0) or 0.0):
                    continue
                last_px = float(paper_engine.latest_prices.get(sym, 0.0) or 0.0)
                if last_px <= 0:
                    continue
                # 必须用「平仓后预估净利」口径：含已扣 accumulated_fees + 本笔平仓 taker；
                # 仅用 unrealized − 单次费会高估净利，导致平完仍亏（尸检里摩擦 > 毛利）。
                net_leg = float(paper_engine.estimate_flat_net_pnl(sym, pos, last_px))
                gross = float(pos.get("unrealized_pnl", 0.0) or 0.0)
                acc = float(pos.get("accumulated_fees", 0.0) or 0.0)
                fee_close = self._leg_market_close_fee_usdt(sym, pos)
                if net_leg <= micro_u:
                    continue
                allow_reload = stance == "scalp"
                self._emit_micro_take_and_reload(
                    pair,
                    sym,
                    pos,
                    reason=f"beta_leg_micro_take_flat_net={net_leg:.4f}",
                    allow_reload=allow_reload,
                )
                lock[sym] = now + cool
                self.log_sync(
                    f"[BetaNeutralHF] LEG_MICRO sym={sym} uPnl={gross:.4f} acc_fees={acc:.4f} "
                    f"est_close_fee={fee_close:.4f} flat_net_est={net_leg:.4f} thr={micro_u:.4f} "
                    f"reload={allow_reload} pair={pair.get('id')}"
                )
            sig = self._signal_for_alt(alt)
            live_z = float(sig["zscore"]) if sig is not None else 0.0
            pair["net_pnl_usdt"] = float(self._pair_net_pnl(pair))
            pair["live_zscore"] = float(live_z)
            pair["estimated_close_fee_usdt"] = float(self._estimate_pair_close_fees_usdt(pair))
            self._sync_active_pair_display_tp_sl(pair, sig)
            self._maybe_pyramid_pair(pair, sig, now)
        for alt in to_delete:
            self._active_pairs.pop(alt, None)
            self._rearm_ready[alt] = True

    def _target_anchor_contracts(self) -> float:
        total = 0.0
        for pair in self._active_pairs.values():
            if not bool(pair.get("anchor_armed", False)):
                continue
            total += float(pair.get("anchor_target_contracts", 0.0) or 0.0)
        return total

    def _has_pending_anchor_order(self) -> bool:
        anchor = str(self._cfg().anchor_symbol)
        rest = paper_engine._maker_resting.get(anchor) or []
        for o in rest:
            ect = dict(o.get("entry_context") or {})
            if bool(ect.get("beta_hedge_anchor_adjust")):
                return True
        for shadow in (paper_engine._shadow_orders or {}).values():
            try:
                if str(shadow.get("symbol") or "") != anchor:
                    continue
                ect = dict(shadow.get("entry_context") or {})
                if bool(ect.get("beta_hedge_anchor_adjust")):
                    return True
            except Exception:
                continue
        return False

    def _rebalance_anchor(self, force: bool = False) -> None:
        """全局锚腿再平衡已关闭；锚腿仓位由原子 Pair 进场 + 独立腿续杯维护。"""
        return

    def runtime_status(self) -> Dict[str, Any]:
        anchor = str(self._cfg().anchor_symbol)
        return {
            "enabled": bool(self._cfg().enabled),
            "anchor_symbol": anchor,
            "anchor_notional_delta": float(self._anchor_notional_delta),
            "anchor_deadband_threshold": float(self._anchor_deadband_threshold),
            "anchor_rebalance_suppressed": bool(self._anchor_rebalance_suppressed),
            "last_expected_tp_vs_cost": float(self._last_expected_tp_vs_cost),
            "configured_leverage": int(self._cfg().pair_leverage),
            "tracked_symbols": list(self._alpha_symbols()),
            "active_pairs": [
                {
                    "pair_id": str(pair["id"]),
                    "alt": str(pair["alt"]),
                    "status": str(pair.get("status", "")),
                    "entry_zscore": float(pair.get("entry_zscore", 0.0) or 0.0),
                    "live_zscore": float(pair.get("live_zscore", 0.0) or 0.0),
                    "beta": float(pair.get("beta", 0.0) or 0.0),
                    "corr": float(pair.get("corr", 0.0) or 0.0),
                    "effective_leverage": int(pair.get("effective_leverage", self._effective_pair_leverage(str(pair["alt"]), anchor))),
                    "net_pnl_usdt": float(pair.get("net_pnl_usdt", self._pair_net_pnl(pair)) or 0.0),
                    "leg_micro_take_usdt": float(self._cfg().leg_micro_take_usdt),
                    "estimated_close_fee_usdt": float(pair.get("estimated_close_fee_usdt", 0.0) or 0.0),
                    "display_alt_tp_price": float(pair.get("display_alt_tp_price", 0.0) or 0.0),
                    "display_alt_sl_price": float(pair.get("display_alt_sl_price", 0.0) or 0.0),
                    "display_anchor_tp_price": float(pair.get("display_anchor_tp_price", 0.0) or 0.0),
                    "display_anchor_sl_price": float(pair.get("display_anchor_sl_price", 0.0) or 0.0),
                    "close_reason": str(pair.get("close_reason", "") or ""),
                    "matrix_regime_alt": self._matrix_regime_str(str(pair["alt"])),
                    "matrix_regime_anchor": self._matrix_regime_str(anchor),
                }
                for pair in self._active_pairs.values()
            ],
            "candidate_pairs": list(self._candidate_snapshots),
            "recent_closed": list(self._recent_closed),
            "anchor_target_contracts": float(self._target_anchor_contracts()),
            "anchor_actual_contracts": float(self._signed_position_contracts(anchor)),
        }

    async def on_tick(self, event: TickEvent) -> None:
        cfg = self._cfg()
        if not cfg.enabled:
            return
        if event.symbol not in set(self._tracked_symbols()):
            return
        px = _last_price(event.ticker or {})
        if px <= 0:
            return
        self._deque(event.symbol).append(px)
        anchor = str(cfg.anchor_symbol)
        ev_c = self._canonical_market_symbol(event.symbol)
        if ev_c == self._canonical_market_symbol(anchor):
            for alt in self._alpha_symbols():
                alt_px = float(paper_engine.latest_prices.get(alt, 0.0) or (self._prices.get(alt)[-1] if self._prices.get(alt) else 0.0) or 0.0)
                if alt_px > 0:
                    self._pair_deque(alt).append((alt_px, px))
        else:
            matched_alt = next(
                (a for a in self._alpha_symbols() if self._canonical_market_symbol(a) == ev_c),
                None,
            )
            if matched_alt is not None:
                btc_px = float(paper_engine.latest_prices.get(anchor, 0.0) or (self._prices.get(anchor)[-1] if self._prices.get(anchor) else 0.0) or 0.0)
                if btc_px > 0:
                    self._pair_deque(matched_alt).append((px, btc_px))
        self._manage_pairs()
        candidates = self._refresh_candidates()
        gs = max(1, int(getattr(cfg, "entry_group_size", 2) or 2))
        max_p = int(cfg.max_active_pairs)
        mode = str(getattr(cfg, "entry_group_mode", "score_batch") or "score_batch").strip().lower()
        if gs <= 1:
            for sig in candidates:
                alt = str(sig["alt"])
                if alt in self._active_pairs:
                    continue
                if abs(float(sig["zscore"])) < float(cfg.entry_zscore):
                    continue
                if len(self._active_pairs) >= max_p:
                    break
                if not self._rearm_ready.get(alt, True):
                    continue
                self._open_pair(sig)
        elif mode in ("symbol_adjacent", "symbol_order", "adjacent"):
            alts = self._alpha_symbols()
            for i in range(0, len(alts), gs):
                group_alts = alts[i : i + gs]
                if len(group_alts) < gs:
                    break
                if len(self._active_pairs) + gs > max_p:
                    break
                sigs: List[Dict[str, Any]] = []
                blocked = False
                for a in group_alts:
                    if a in self._active_pairs or not self._rearm_ready.get(a, True):
                        blocked = True
                        break
                    sig = self._entry_signal_for_alt(a)
                    if not sig:
                        blocked = True
                        break
                    sigs.append(sig)
                if blocked or len(sigs) != gs:
                    continue
                lev_g = self._group_entry_leverage([str(s["alt"]) for s in sigs])
                if not all(self._pair_entry_feasible(s, forced_leverage=lev_g) for s in sigs):
                    continue
                for s in sigs:
                    self._open_pair(s, lev_g)
        else:
            # score_batch：按候选分数从高到低，每波凑满 gs 个且 EV 全过则同 tick 连发；可在一 tick 内开多波直至上限
            max_waves = max(1, int(getattr(cfg, "entry_score_batch_max_waves", 10) or 10))
            for _ in range(max_waves):
                if len(self._active_pairs) + gs > max_p:
                    break
                candidates = self._refresh_candidates()
                batch: List[Dict[str, Any]] = []
                picked: set[str] = set()
                for sig in candidates:
                    alt = str(sig["alt"])
                    if alt in picked or alt in self._active_pairs:
                        continue
                    if abs(float(sig["zscore"])) < float(cfg.entry_zscore):
                        continue
                    if not self._rearm_ready.get(alt, True):
                        continue
                    batch.append(sig)
                    picked.add(alt)
                    if len(batch) >= gs:
                        break
                if len(batch) < gs:
                    break
                lev_g = self._group_entry_leverage([str(s["alt"]) for s in batch])
                if not all(self._pair_entry_feasible(s, forced_leverage=lev_g) for s in batch):
                    break
                for s in batch:
                    self._open_pair(s, lev_g)

import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from src.strategy.base import BaseStrategy
from src.core.events import TickEvent, SignalEvent, OrderBookEvent
from src.utils.logger import log
from src.utils.indicators import get_rsi
from src.ai.scorer import ai_scorer
from src.ai.regime import regime_classifier, MarketRegime
from src.ai.collector import data_collector
from src.core.config_manager import config_manager
from src.ai.analyzer import ai_context
from src.core.risk_engine import (
    risk_engine,
    berserker_max_leverage_for_symbol,
    berserker_obi_threshold_for,
)
from src.core.paper_engine import paper_engine
from src.core.equity_sizing import margin_target_fraction, shark_net_tp_sl_usdt
from src.core.fiat_tp_sl import compute_tp_sl_prices_net_usdt, estimate_fees_usdt
from src.core.globals import bot_context
from src.config import SystemState
from src.strategy import predator_matrix as pmx
from src.strategy.playbook import select_quadrant
from src.strategy.tuner import strategy_auto_tuner


def _neutral_micro_confirm_ok(side: str, px_window: List[float], last: float, p: Any) -> bool:
    """RSI 超买超卖后，要求相对近窗极值已小幅回转，过滤下跌/上涨中继里的逆势单。"""
    if not getattr(p, "neutral_micro_confirm_enabled", True):
        return True
    n = max(5, int(getattr(p, "neutral_micro_window_ticks", 10) or 10))
    if len(px_window) < n:
        return True
    seg = px_window[-n:]
    lo, hi = min(seg), max(seg)
    if last <= 0 or hi <= 0:
        return True
    span = (hi - lo) / last
    if span < 8e-6:
        return True
    bps = float(getattr(p, "neutral_micro_bounce_bps", 6.0) or 0.0) / 1e4
    if side == "buy":
        return last >= lo * (1.0 + bps)
    return last <= hi * (1.0 - bps)


def _neutral_second_confirm_ok(side: str, px_window: List[float], last: float, p: Any) -> bool:
    """更严格的二次确认：要求价格从极值回撤幅度更大，减少第一脚反抽/反弹就贸然开仓。"""
    if not getattr(p, "neutral_second_confirm_enabled", True):
        return True
    n = max(6, int(getattr(p, "neutral_micro_window_ticks", 10) or 10))
    if len(px_window) < n:
        return True
    seg = px_window[-n:]
    lo, hi = min(seg), max(seg)
    if last <= 0 or hi <= 0 or lo <= 0:
        return True
    bps = float(getattr(p, "neutral_second_confirm_bps", 10.0) or 0.0) / 1e4
    if side == "buy":
        return last >= lo * (1.0 + bps)
    return last <= hi * (1.0 - bps)


def _neutral_trend_run_bps(side: str, px_window: List[float], last: float, n: int) -> float:
    """近窗内单边跑动（bps）：卖空侧用低点→现价涨幅；买入侧用高点→现价跌幅。"""
    if last <= 0 or len(px_window) < max(5, n):
        return 0.0
    seg = px_window[-n:]
    lo, hi = min(seg), max(seg)
    if lo <= 0 or hi <= 0:
        return 0.0
    if side == "buy":
        return (hi - last) / hi * 1e4
    return (last - lo) / lo * 1e4


def _neutral_blocked_by_window_run(
    side: str, last: float, px_window: List[float], p: Any
) -> Optional[str]:
    """强单边跑动超过阈值则禁止均值回归（常见量化做法：不与短窗动量对赌）。"""
    cap = float(getattr(p, "neutral_block_if_window_run_bps", 0.0) or 0.0)
    if cap <= 0:
        return None
    n = max(5, int(getattr(p, "neutral_micro_window_ticks", 10) or 10))
    run = _neutral_trend_run_bps(side, px_window, last, n)
    if run >= cap:
        return f"run {run:.0f}bps (cap {cap:.0f})"
    return None


def _neutral_effective_ai_threshold(side: str, last: float, px_window: List[float], p: Any) -> float:
    base = float(getattr(p, "neutral_ai_threshold", 40) or 40)
    if not getattr(p, "neutral_ai_trend_relax_enabled", True):
        return base
    n = max(5, int(getattr(p, "neutral_micro_window_ticks", 10) or 10))
    run = _neutral_trend_run_bps(side, px_window, last, n)
    need = float(getattr(p, "neutral_ai_trend_run_bps", 28.0) or 0.0)
    if run < need:
        return base
    relax = float(getattr(p, "neutral_ai_trend_relax_points", 12.0) or 0.0)
    floor_v = float(getattr(p, "neutral_ai_trend_relax_floor", 38.0) or 0.0)
    return max(floor_v, base - relax)


def _estimated_scene_bias(symbol: str, side: str, strategy_name: str, regime: str, ai_score: float) -> Dict[str, Any]:
    atr_pct = float(risk_engine.symbol_atr_pct.get(symbol, 0.0) or 0.0)
    pb = config_manager.get_config().playbook
    q = select_quadrant(
        float(risk_engine.current_balance or 0.0),
        atr_pct if atr_pct > 0 else float(pb.matrix_volatility_threshold_pct),
        capital_threshold=pb.matrix_capital_threshold_usdt,
        volatility_threshold=pb.matrix_volatility_threshold_pct,
    )
    feat = {
        "regime": str(regime or "UNKNOWN"),
        "symbol": str(symbol or ""),
        "side": str(side or "unknown"),
        "strategy": str(strategy_name or "UNKNOWN"),
        "quadrant": str(q.value if q else "NA"),
        "ai_score": float(ai_score),
        "ai_bucket": strategy_auto_tuner._ai_bucket(float(ai_score)),
    }
    return strategy_auto_tuner.scene_bias_for_features(feat)


def _attack_scene_gate(
    sym: str,
    side: str,
    regime: str,
    ai_score: float,
    trigger_score: float,
    trigger_threshold: float,
    obi: float,
    bias: Dict[str, Any],
    params: Any,
) -> Tuple[bool, str]:
    match_level = str(bias.get("match_level") or "none")
    priority_score = float(bias.get("priority_score", 0.0) or 0.0)
    if match_level.startswith("strong_"):
        return True, f"{match_level}:{priority_score:.2f}"
    if priority_score >= float(getattr(params, "attack_scene_priority_floor", 8.0) or 8.0):
        return True, f"priority_floor:{priority_score:.2f}"
    escape_ai = float(getattr(params, "attack_scene_escape_ai_score", 78.0) or 78.0)
    escape_obi = float(getattr(params, "attack_scene_escape_obi_min", 0.10) or 0.10)
    obi_ok = (side == "buy" and obi >= escape_obi) or (side == "sell" and obi <= -escape_obi)
    if regime in {MarketRegime.TRENDING_UP.value, MarketRegime.TRENDING_DOWN.value} and ai_score >= escape_ai and obi_ok and trigger_score >= trigger_threshold + 4.0:
        return True, f"escape:{ai_score:.1f}/{obi:.3f}"
    return False, f"scene_gate(match={match_level}, priority={priority_score:.2f})"


def _effective_neutral_cooldown(params: Any) -> float:
    return max(8.0, float(getattr(params, "neutral_signal_cooldown_sec", 0.0) or 0.0))


def _effective_attack_cooldown(params: Any) -> float:
    return max(5.0, float(getattr(params, "attack_signal_cooldown_sec", 0.0) or 0.0))


def _core_tp_sl_bps_for_symbol(symbol: Optional[str]) -> Tuple[float, float]:
    """基准 bps；当 symbol 的滚动 ATR% 超阈值时加宽止损并保证 TP/SL 最小盈亏比（类 ATR 止损 + RR 目标）。"""
    p = config_manager.get_config().strategy.params
    tp0 = float(getattr(p, "core_entry_tp_bps", 55.0) or 55.0)
    sl0 = float(getattr(p, "core_entry_sl_bps", 50.0) or 50.0)
    if not symbol:
        return tp0, sl0
    thr = float(getattr(p, "core_high_atr_threshold", 0.01) or 0)
    if thr <= 0:
        return tp0, sl0
    ap = float(risk_engine.symbol_atr_pct.get(symbol, 0.0) or 0.0)
    if ap < thr:
        return tp0, sl0
    widen = float(getattr(p, "core_atr_sl_widen_mult", 1.35) or 1.35)
    cap_sl = float(getattr(p, "core_high_atr_sl_cap_bps", 240.0) or 240.0)
    atr_frac = float(getattr(p, "core_atr_sl_from_atr_frac", 1.08) or 1.08)
    min_rr = float(getattr(p, "core_high_atr_tp_min_rr", 1.2) or 1.2)
    cap_tp = float(getattr(p, "core_high_atr_tp_cap_bps", 400.0) or 400.0)
    atr_bps = ap * 1e4
    sl_b = max(sl0 * widen, min(cap_sl, atr_bps * atr_frac))
    tp_b = max(tp0 * widen, sl_b * min_rr)
    tp_b = min(tp_b, cap_tp)
    sl_b = min(sl_b, cap_sl)
    return tp_b, sl_b


def _core_volatility_profile(symbol: Optional[str]) -> Dict[str, Any]:
    p = config_manager.get_config().strategy.params
    thr = float(getattr(p, "core_high_atr_threshold", 0.01) or 0.01)
    if not symbol or thr <= 0:
        return {"profile": "unknown", "atr_pct": 0.0, "atr_ratio": 0.0, "notional_mult": 1.0, "tp_mult": 1.0, "sl_mult": 1.0}
    ap = float(risk_engine.symbol_atr_pct.get(symbol, 0.0) or 0.0)
    ratio = ap / max(thr, 1e-9) if ap > 0 else 0.0
    if ratio >= 2.5:
        return {"profile": "extreme", "atr_pct": ap, "atr_ratio": ratio, "notional_mult": 0.48, "tp_mult": 1.35, "sl_mult": 1.55}
    if ratio >= 1.8:
        return {"profile": "high", "atr_pct": ap, "atr_ratio": ratio, "notional_mult": 0.62, "tp_mult": 1.22, "sl_mult": 1.35}
    if ratio >= 1.2:
        return {"profile": "elevated", "atr_pct": ap, "atr_ratio": ratio, "notional_mult": 0.80, "tp_mult": 1.12, "sl_mult": 1.16}
    if ratio <= 0.6 and ap > 0:
        return {"profile": "calm", "atr_pct": ap, "atr_ratio": ratio, "notional_mult": 1.10, "tp_mult": 0.94, "sl_mult": 0.92}
    if ratio <= 0.85 and ap > 0:
        return {"profile": "stable", "atr_pct": ap, "atr_ratio": ratio, "notional_mult": 1.04, "tp_mult": 0.97, "sl_mult": 0.96}
    return {"profile": "normal", "atr_pct": ap, "atr_ratio": ratio, "notional_mult": 1.0, "tp_mult": 1.0, "sl_mult": 1.0}


def _core_leverage_bracket_multipliers(leverage: int) -> Tuple[float, float]:
    """
    杠杆越高，同样 equity 括号对应的「价格距离」越紧；略放大净利目标/止损预算，止损放大更多。
    返回 (tp_net_mult, sl_net_mult)，低杠杆为 (1,1) 不改变行为。
    """
    L = max(1, int(leverage))
    if L <= 22:
        return (1.0, 1.0)
    if L <= 45:
        return (1.06, 1.24)
    if L <= 65:
        return (1.10, 1.36)
    return (1.14, 1.48)


def _core_entry_tp_sl_prices(
    side: str,
    ref_px: float,
    symbol: Optional[str] = None,
    leverage: int = 10,
) -> Tuple[float, float]:
    """信号参考价上按 bps 推导限价止盈、止损，写入 entry_context（成交后由 paper_engine 挂 OCO）。"""
    tp_b, sl_b = _core_tp_sl_bps_for_symbol(symbol)
    vp = _core_volatility_profile(symbol)
    tp_b *= float(vp["tp_mult"])
    sl_b *= float(vp["sl_mult"])
    tpm, slm = _core_leverage_bracket_multipliers(leverage)
    tp_b *= tpm
    sl_b *= slm
    if side == "buy":
        return ref_px * (1.0 + tp_b / 1e4), ref_px * (1.0 - sl_b / 1e4)
    return ref_px * (1.0 - tp_b / 1e4), ref_px * (1.0 + sl_b / 1e4)


def _core_entry_limit_price(symbol: str, side: str, last_price: float) -> float:
    """主策略默认被动挂单：优先贴近 best bid/ask，缺盘口时退回到轻微被动偏移。"""
    p = config_manager.get_config().strategy.params
    offset = float(getattr(p, "core_entry_limit_offset_bps", 1.0) or 0.0) / 1e4
    bb, ba = paper_engine._best_bid_ask(symbol)
    if side == "buy":
        if bb > 0:
            return max(0.0, bb)
        return max(0.0, float(last_price) * (1.0 - max(offset, 0.0001)))
    if ba > 0:
        return max(0.0, ba)
    return max(0.0, float(last_price) * (1.0 + max(offset, 0.0001)))


def _core_bracket_limit_prices(
    symbol: str,
    side: str,
    ref_px: float,
    contracts: float,
    leverage: int = 10,
) -> Tuple[float, float]:
    """
    Core 括号限价：默认按权益目标净利（USDT）反推 TP/SL 价格，平仓均为限价撮合；
    关闭 core_use_equity_net_brackets 时退回 bps 路径。
    """
    p = config_manager.get_config().strategy.params
    if (
        float(contracts) <= 0
        or float(ref_px) <= 0
        or not getattr(p, "core_use_equity_net_brackets", True)
    ):
        return _core_entry_tp_sl_prices(side, float(ref_px), symbol, leverage=int(leverage))
    eq = max(float(risk_engine.current_balance), 1e-9)
    tgt_net, rsk_net = shark_net_tp_sl_usdt(
        eq,
        float(getattr(p, "core_tp_net_equity_fraction", 0.01)),
        float(getattr(p, "core_sl_net_equity_fraction", 0.005)),
    )
    thr_atr = float(getattr(p, "core_high_atr_threshold", 0.01) or 0)
    ap = float(risk_engine.symbol_atr_pct.get(symbol, 0.0) or 0.0)
    if thr_atr > 0 and ap >= thr_atr:
        tgt_net *= float(getattr(p, "core_high_atr_net_tp_mult", 1.0))
        rsk_net *= float(getattr(p, "core_high_atr_net_sl_mult", 1.0))
    vp = _core_volatility_profile(symbol)
    tgt_net *= float(vp["tp_mult"])
    rsk_net *= float(vp["sl_mult"])
    tp_lev_m, sl_lev_m = _core_leverage_bracket_multipliers(leverage)
    tgt_net *= tp_lev_m
    rsk_net *= sl_lev_m
    cs = float(paper_engine.contract_size_for_symbol(symbol))
    q = float(contracts)
    n0 = q * cs * float(ref_px)
    taker, maker = paper_engine._fee_rates_for_symbol(symbol)
    fo, fctp, fcsl = estimate_fees_usdt(
        n0,
        n0,
        n0,
        taker_rate=taker,
        maker_rate=maker,
        tp_as_maker=True,
        sl_as_taker=False,
    )
    side_net = "long" if str(side).lower() == "buy" else "short"
    try:
        return compute_tp_sl_prices_net_usdt(
            side_net,
            float(ref_px),
            q,
            cs,
            target_net_usdt=tgt_net,
            risk_net_usdt=rsk_net,
            fee_open_usdt=fo,
            fee_close_tp_usdt=fctp,
            fee_close_sl_usdt=fcsl,
        )
    except (ValueError, ZeroDivisionError):
        return _core_entry_tp_sl_prices(side, float(ref_px), symbol, leverage=int(leverage))


def _core_grinder_leverage_capped(symbol: str, lev: int) -> int:
    """
    与 RiskEngine 一致：近 10 分钟波动已就绪时，用分档结果，不被短窗 ATR 误压到 12x。
    数据不足时沿用「高短窗 ATR → 压低杠杆」保护。
    """
    p = config_manager.get_config().strategy.params
    rc = config_manager.get_config().risk
    if risk_engine._ten_min_window_ready(symbol):
        r10 = risk_engine.ten_min_range_pct(symbol)
        if r10 <= 0.03:
            cap = max(1, int(rc.grinder_leverage_max))
            return max(1, min(int(lev), cap))
        if r10 > 0.05:
            cap = int(getattr(p, "core_high_atr_max_leverage", 20) or 20)
            return max(1, min(int(lev), max(1, cap)))
        return max(1, min(int(lev), 50))
    thr = float(getattr(p, "core_high_atr_threshold", 0.01) or 0)
    if thr <= 0:
        return int(lev)
    ap = float(risk_engine.symbol_atr_pct.get(symbol, 0.0) or 0.0)
    if ap < thr:
        return int(lev)
    cap = int(getattr(p, "core_high_atr_max_leverage", 12) or 12)
    return max(1, min(int(lev), max(1, cap)))


class CoreNeutralStrategy(BaseStrategy):
    """
    Mean Reversion Strategy for Oscillating Markets (Daily Grinder).
    Max 5% equity margin per shot; leverage 10–20x from Kelly + ATR.
    """

    def __init__(self):
        super().__init__("CoreNeutral")
        self._px_window: list = []
        self._neutral_last_fire: Dict[str, float] = {}

    async def on_tick(self, event: TickEvent):
        symbol = event.symbol
        ticker = event.ticker
        last_price = float(ticker["last"])
        if last_price <= 0:
            return

        self._px_window.append(last_price)
        self._px_window = self._px_window[-32:]
        if len(self._px_window) >= 5:
            chunk = self._px_window[-14:]
            atr_pct = max((max(chunk) - min(chunk)) / last_price, 1e-6)
            risk_engine.record_symbol_atr_pct(symbol, atr_pct)

        config = config_manager.get_config().strategy.params
        rsi = get_rsi(symbol, last_price)
        regime = regime_classifier.analyze(symbol)
        if regime != MarketRegime.OSCILLATING:
            return

        side = None
        if rsi < config.neutral_rsi_buy:
            side = "buy"
        elif rsi > config.neutral_rsi_sell:
            side = "sell"
        if not side:
            return

        if not _neutral_micro_confirm_ok(side, self._px_window, last_price, config):
            await self.log(
                f"SKIP mean-revert {side.upper()}: micro-structure (need bounce from window extreme). RSI={rsi:.1f}"
            )
            return
        if not _neutral_second_confirm_ok(side, self._px_window, last_price, config):
            await self.log(
                f"SKIP mean-revert {side.upper()}: second confirmation not ready. RSI={rsi:.1f}"
            )
            return

        blk = _neutral_blocked_by_window_run(side, last_price, self._px_window, config)
        if blk:
            await self.log(f"SKIP mean-revert {side.upper()}: momentum ({blk}). RSI={rsi:.1f}")
            return

        obi_against = float(getattr(config, "neutral_max_obi_against", 0.10) or 0.10)
        attack_strategy = None
        se = bot_context.get_strategy_engine()
        if se is not None:
            for st in getattr(se, "strategies", []) or []:
                if getattr(st, "name", "") == "CoreAttack":
                    attack_strategy = st
                    break
        latest_obi = float(getattr(attack_strategy, "latest_obi", 0.0) or 0.0) if attack_strategy else 0.0
        if side == "buy" and latest_obi < -obi_against:
            await self.log(
                f"SKIP mean-revert BUY: OBI={latest_obi:.3f} < -{obi_against:.2f} (flow against)"
            )
            return
        if side == "sell" and latest_obi > obi_against:
            await self.log(
                f"SKIP mean-revert SELL: OBI={latest_obi:.3f} > {obi_against:.2f} (flow against)"
            )
            return

        score = ai_scorer.score(symbol, ticker, side)
        eff_ai_thr = _neutral_effective_ai_threshold(side, last_price, self._px_window, config)
        scene_bias = _estimated_scene_bias(symbol, side, self.name, regime.value, float(score))
        eff_ai_thr = max(0.0, eff_ai_thr + float(scene_bias.get("threshold_delta", 0.0) or 0.0))
        if score < eff_ai_thr:
            await self.log(
                f"Signal REJECTED by AI. Score: {score:.1f} (need ≥{eff_ai_thr:.1f}, base {float(config.neutral_ai_threshold):.1f})"
            )
            data_collector.log_execution(symbol, ticker, regime.value, score, "REJECTED")
            return

        ncd = _effective_neutral_cooldown(config)
        now = time.time()
        if ncd > 0 and now - self._neutral_last_fire.get(symbol, 0.0) < ncd:
            return

        await self.log(
            f"Signal APPROVED. {side.upper()} {symbol} @ {last_price}. RSI: {rsi:.1f}, AI Score: {score:.1f}"
        )

        lev = _core_grinder_leverage_capped(
            symbol, risk_engine.recommended_grinder_leverage(symbol)
        )
        equity = risk_engine.current_balance
        risk_cfg = config_manager.get_config().risk
        vol_profile = _core_volatility_profile(symbol)
        frac = (
            margin_target_fraction(equity)
            if getattr(risk_cfg, "use_equity_tier_margin", True)
            else float(risk_cfg.max_single_risk)
        )
        max_margin = equity * frac
        notional = max_margin * lev * float(vol_profile["notional_mult"])
        qty = paper_engine.contracts_for_target_usdt_notional(symbol, last_price, notional)

        if qty <= 0:
            return

        sm = bot_context.get_state_machine()
        ai_snap = ai_context.get(symbol)
        tp_px, sl_px = _core_bracket_limit_prices(
            symbol, side, float(last_price), float(qty), leverage=int(lev)
        )
        entry_px = _core_entry_limit_price(symbol, side, float(last_price))
        self.emit_signal(
            SignalEvent(
                strategy_name=self.name,
                symbol=symbol,
                side=side,
                order_type="limit",
                price=entry_px,
                amount=qty,
                leverage=int(lev),
                post_only=True,
                margin_mode="cross",
                entry_context={
                    "strategy": self.name,
                    "regime": regime.value,
                    "ai_regime": regime.value,
                    "rsi": float(rsi),
                    "obi": float(latest_obi),
                    "ai_score": float(score),
                    "ai_score_bucket": strategy_auto_tuner._ai_bucket(float(score)),
                    "ai_reason": str(ai_snap.get("reason", ""))[:200],
                    "scene_learning": dict(scene_bias),
                    "volatility_profile": str(vol_profile["profile"]),
                    "volatility_atr_pct": float(vol_profile["atr_pct"]),
                    "volatility_atr_ratio": float(vol_profile["atr_ratio"]),
                    "volatility_notional_mult": float(vol_profile["notional_mult"]),
                    "volatility_tp_mult": float(vol_profile["tp_mult"]),
                    "volatility_sl_mult": float(vol_profile["sl_mult"]),
                    "entry_limit_price": float(entry_px),
                    "entry_limit_post_only": True,
                    "core_limit_requote_enabled": True,
                    "core_limit_ttl_ms": int(getattr(config, "core_entry_limit_ttl_ms", 8000) or 8000),
                    "core_limit_requote_max": int(getattr(config, "core_entry_limit_requote_max", 2) or 2),
                    "resting_quote": True,
                    "trading_mode": sm.state.value if sm and sm.state else None,
                    "take_profit_limit_price": tp_px,
                    "stop_loss_limit_price": sl_px,
                },
            )
        )
        self._neutral_last_fire[symbol] = time.time()
        data_collector.log_execution(symbol, ticker, regime.value, score, side.upper())


class CoreAttackStrategy(BaseStrategy):
    """
    Trend / resonance (Daily Grinder) + Berserker fast path (OBI-only, post-only isolated).
    """

    def __init__(self):
        super().__init__("CoreAttack")
        # 按合约维护窗口；禁止把 BTC/ETH 等不同品种价格混进同一条 SMA
        self._prices_by_symbol: Dict[str, List[float]] = defaultdict(list)
        self.window = 60
        self.latest_obi = 0.0
        self._berserk_last_fire = 0.0
        self._attack_last_fire: Dict[str, float] = {}
        self._ohlcv_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}

    async def _ensure_ohlcv(self, exchange: Any, sym: str) -> List[Dict[str, Any]]:
        pm = config_manager.get_config().predator_matrix
        if not pm.enabled:
            return []
        now = time.time()
        ts, data = self._ohlcv_cache.get(sym, (0.0, []))
        if data and (now - ts) < float(pm.ohlcv_refresh_sec):
            return data
        if not exchange or not hasattr(exchange, "fetch_candlesticks"):
            return data
        try:
            cand = await exchange.fetch_candlesticks(
                sym, interval=pm.ohlcv_interval, limit=int(pm.ohlcv_limit)
            )
            if cand:
                self._ohlcv_cache[sym] = (now, cand)
                return cand
        except Exception:
            pass
        return data

    async def on_orderbook(self, event: OrderBookEvent):
        self.latest_obi = event.obi

    async def on_tick(self, event: TickEvent):
        price = event.ticker.get("last", 0)
        if price == 0:
            return

        sym = event.symbol
        q = self._prices_by_symbol[sym]
        q.append(float(price))
        if len(q) > self.window:
            q.pop(0)

        if len(q) >= 5:
            chunk = q[-14:]
            atr_pct = max((max(chunk) - min(chunk)) / float(price), 1e-6)
            risk_engine.record_symbol_atr_pct(sym, atr_pct)

        sm = bot_context.get_state_machine()
        if sm and sm.state == SystemState.BERSERKER:
            now = time.time()
            obi_thr = berserker_obi_threshold_for(event.symbol)
            if now - self._berserk_last_fire >= 0.1:
                cap = berserker_max_leverage_for_symbol(event.symbol)
                ob = paper_engine.orderbooks_cache.get(event.symbol)
                if ob and ob.get("bids") and ob.get("asks"):
                    if self.latest_obi >= obi_thr:
                        lim = float(ob["bids"][0][0]) * 1.0001
                        avail = max(paper_engine.available_balance, 0.0)
                        max_notional = avail * 0.99 * float(cap)
                        qty = paper_engine.contracts_for_target_usdt_notional(
                            event.symbol, float(lim), max_notional
                        )
                        if qty > 0:
                            lev_bb = int(_core_grinder_leverage_capped(event.symbol, int(cap)))
                            tp_px, sl_px = _core_bracket_limit_prices(
                                event.symbol, "buy", float(lim), float(qty), leverage=lev_bb
                            )
                            self.emit_signal(
                                SignalEvent(
                                    strategy_name=self.name,
                                    symbol=event.symbol,
                                    side="buy",
                                    order_type="limit",
                                    price=lim,
                                    amount=qty,
                                    leverage=lev_bb,
                                    berserker=True,
                                    post_only=True,
                                    margin_mode="isolated",
                                    entry_context={
                                        "strategy": self.name,
                                        "berserker": True,
                                        "obi": float(self.latest_obi),
                                        "obi_threshold": float(obi_thr),
                                        "trading_mode": "BERSERKER",
                                        "take_profit_limit_price": tp_px,
                                        "stop_loss_limit_price": sl_px,
                                    },
                                )
                            )
                            self._berserk_last_fire = now
                            log.info(f"[{self.name}] BERSERKER OBI buy | {event.symbol} @ ~{lim:.4f} {cap}x")
                        return
                    if self.latest_obi <= -obi_thr:
                        lim = float(ob["asks"][0][0]) * 0.9999
                        avail = max(paper_engine.available_balance, 0.0)
                        max_notional = avail * 0.99 * float(cap)
                        qty = paper_engine.contracts_for_target_usdt_notional(
                            event.symbol, float(lim), max_notional
                        )
                        if qty > 0:
                            lev_bb = int(_core_grinder_leverage_capped(event.symbol, int(cap)))
                            tp_px, sl_px = _core_bracket_limit_prices(
                                event.symbol, "sell", float(lim), float(qty), leverage=lev_bb
                            )
                            self.emit_signal(
                                SignalEvent(
                                    strategy_name=self.name,
                                    symbol=event.symbol,
                                    side="sell",
                                    order_type="limit",
                                    price=lim,
                                    amount=qty,
                                    leverage=lev_bb,
                                    berserker=True,
                                    post_only=True,
                                    margin_mode="isolated",
                                    entry_context={
                                        "strategy": self.name,
                                        "berserker": True,
                                        "obi": float(self.latest_obi),
                                        "obi_threshold": float(obi_thr),
                                        "trading_mode": "BERSERKER",
                                        "take_profit_limit_price": tp_px,
                                        "stop_loss_limit_price": sl_px,
                                    },
                                )
                            )
                            self._berserk_last_fire = now
                            log.info(f"[{self.name}] BERSERKER OBI sell | {event.symbol} @ ~{lim:.4f} {cap}x")
                        return
            return

        if len(q) < self.window:
            return

        pm_cfg = config_manager.get_config().predator_matrix
        regime = regime_classifier.analyze(sym)
        if regime == MarketRegime.CHAOTIC and not pm_cfg.enabled:
            return

        exchange = bot_context.get_exchange()
        candles: List[Dict[str, Any]] = await self._ensure_ohlcv(exchange, sym) if pm_cfg.enabled else []

        ai_data = ai_context.get(sym)
        ai_score_raw = float(ai_data.get("score", 50.0))
        ai_bull_score = ai_score_raw
        ai_bear_score = 100.0 - ai_score_raw

        sma = sum(q) / len(q)
        sma_bull = 0.0
        sma_bear = 0.0
        if price > sma:
            distance = (price / sma) - 1.0
            sma_bull = min(100.0, 50.0 + (distance / 0.002) * 50.0)
        elif price < sma:
            distance = 1.0 - (price / sma)
            sma_bear = min(100.0, 50.0 + (distance / 0.002) * 50.0)

        tech_bull_score = sma_bull
        tech_bear_score = sma_bear
        pred_meta: Dict[str, Any] = {"regime": regime.value, "predator_enabled": pm_cfg.enabled}

        if pm_cfg.enabled and candles:
            vp = pmx.volume_profile_poc_vah_val(
                candles,
                bins=int(pm_cfg.vp_bins),
                value_area_pct=float(pm_cfg.vp_value_area_pct),
            )
            if vp:
                poc, vah, val, _bw = vp
                pred_meta["poc"] = poc
                pred_meta["vah"] = vah
                pred_meta["val"] = val
                vb, vr = pmx.vp_structure_scores(float(price), poc, vah, val)
                tech_bull_score, tech_bear_score = pmx.combine_tech_scores(
                    sma_bull,
                    sma_bear,
                    vb,
                    vr,
                    vp_weight=float(pm_cfg.vp_tech_weight),
                )
            if pmx.liquidity_sweep_long(
                candles,
                self.latest_obi,
                int(pm_cfg.liquidity_swing_lookback),
                float(pm_cfg.liquidity_obi_floor_long),
            ):
                pred_meta["liquidity_sweep_long"] = True
            if pmx.liquidity_sweep_short(
                candles,
                self.latest_obi,
                int(pm_cfg.liquidity_swing_lookback),
                float(pm_cfg.liquidity_obi_ceiling_short),
            ):
                pred_meta["liquidity_sweep_short"] = True

        obi_bull_score = 0.0
        obi_bear_score = 0.0
        if self.latest_obi > 0:
            obi_bull_score = min(100.0, (self.latest_obi / 0.2) * 100.0)
        elif self.latest_obi < 0:
            obi_bear_score = min(100.0, (abs(self.latest_obi) / 0.2) * 100.0)

        w_ai, w_tech, w_obi = pmx.weights_for_regime(regime, pm_cfg.regime_weights)

        total_bull_score = (
            (ai_bull_score * w_ai)
            + (tech_bull_score * w_tech)
            + (obi_bull_score * w_obi)
        )
        total_bear_score = (
            (ai_bear_score * w_ai)
            + (tech_bear_score * w_tech)
            + (obi_bear_score * w_obi)
        )

        if pm_cfg.enabled:
            spec: Dict[str, Any] = {}
            if exchange is not None:
                spec = getattr(exchange, "contract_specs_cache", {}).get(sym, {}) or {}
            funding = float(spec.get("funding_rate", 0) or 0)
            pred_meta["funding_rate"] = funding
            fb, fe = pmx.funding_obi_divergence_points(
                funding,
                self.latest_obi,
                float(pm_cfg.funding_neg_extreme),
                float(pm_cfg.funding_pos_extreme),
                float(pm_cfg.funding_obi_min_align),
                float(pm_cfg.funding_boost_points),
            )
            total_bull_score += fb
            total_bear_score += fe
            if fb > 0:
                pred_meta["funding_obi_long"] = True
            if fe > 0:
                pred_meta["funding_obi_short"] = True

            if candles:
                if pred_meta.get("liquidity_sweep_long"):
                    total_bull_score += float(pm_cfg.liquidity_boost_points)
                if pred_meta.get("liquidity_sweep_short"):
                    total_bear_score += float(pm_cfg.liquidity_boost_points)

                sq = pmx.volatility_squeeze_breakout(
                    candles,
                    bb_period=int(pm_cfg.squeeze_bb_period),
                    bbw_percentile=float(pm_cfg.squeeze_bbw_percentile),
                    volume_mult=float(pm_cfg.squeeze_volume_mult),
                    donchian_lookback=int(pm_cfg.squeeze_donchian),
                    bbw_history=int(pm_cfg.squeeze_bbw_history),
                )
                pred_meta["squeeze_signal"] = sq
                if sq == 1:
                    total_bull_score += float(pm_cfg.squeeze_boost_points)
                elif sq == -1:
                    total_bear_score += float(pm_cfg.squeeze_boost_points)

        signal_action = None
        trigger_score = 0.0
        params = config_manager.get_config().strategy.params
        trigger_threshold = float(params.attack_ai_threshold)
        atr_sym = float(risk_engine.symbol_atr_pct.get(sym, 0.0) or 0.0)
        h_thr = float(getattr(params, "core_high_atr_threshold", 0.01) or 0)
        if h_thr > 0 and atr_sym >= h_thr:
            trigger_threshold += float(getattr(params, "attack_high_atr_score_padding", 12.0) or 0.0)

        bull_scene_bias = _estimated_scene_bias(sym, "buy", self.name, regime.value, float(ai_bull_score))
        bear_scene_bias = _estimated_scene_bias(sym, "sell", self.name, regime.value, float(ai_bear_score))
        bull_threshold = max(0.0, trigger_threshold + float(bull_scene_bias.get("threshold_delta", 0.0) or 0.0))
        bear_threshold = max(0.0, trigger_threshold + float(bear_scene_bias.get("threshold_delta", 0.0) or 0.0))

        chosen_scene_bias: Dict[str, Any] = {}
        chosen_threshold = trigger_threshold
        if total_bull_score >= bull_threshold:
            signal_action = "buy"
            trigger_score = total_bull_score
            chosen_scene_bias = bull_scene_bias
            chosen_threshold = bull_threshold
        elif total_bear_score >= bear_threshold:
            signal_action = "sell"
            trigger_score = total_bear_score
            chosen_scene_bias = bear_scene_bias
            chosen_threshold = bear_threshold

        if not signal_action:
            return

        allow_attack, attack_scene_reason = _attack_scene_gate(
            sym,
            signal_action,
            regime.value,
            float(ai_score_raw),
            float(trigger_score),
            float(chosen_threshold),
            float(self.latest_obi),
            chosen_scene_bias,
            params,
        )
        if not allow_attack:
            log.info(f"[{self.name}] Skip {signal_action.upper()} {sym}: {attack_scene_reason}")
            return

        if getattr(params, "attack_momentum_confirm_enabled", True) and len(q) >= 8:
            ft = max(3, int(getattr(params, "attack_sma_fast_ticks", 12)))
            tail = q[-ft:]
            sma_f = sum(tail) / float(len(tail))
            adv = float(getattr(params, "attack_sma_align_max_adverse_bps", 12.0)) / 1e4
            px = float(price)
            if signal_action == "buy" and px < sma_f * (1.0 - adv):
                log.info(
                    f"[{self.name}] Skip BUY {sym}: price {px:.6g} below fastSMA band (sma={sma_f:.6g})"
                )
                return
            if signal_action == "sell" and px > sma_f * (1.0 + adv):
                log.info(
                    f"[{self.name}] Skip SELL {sym}: price {px:.6g} above fastSMA band (sma={sma_f:.6g})"
                )
                return

        obi_lim = float(getattr(params, "attack_max_obi_against", 0.28))
        if signal_action == "buy" and self.latest_obi < -obi_lim:
            log.info(
                f"[{self.name}] Skip BUY {sym}: OBI={self.latest_obi:.3f} < -{obi_lim:.2f} (order flow against)"
            )
            return
        if signal_action == "sell" and self.latest_obi > obi_lim:
            log.info(
                f"[{self.name}] Skip SELL {sym}: OBI={self.latest_obi:.3f} > {obi_lim:.2f} (order flow against)"
            )
            return

        slow_guard = float(getattr(params, "attack_slow_sma_trend_guard_bps", 0.0) or 0.0)
        if slow_guard > 0 and len(q) >= self.window:
            slow = sum(q) / float(len(q))
            b = slow_guard / 1e4
            px = float(price)
            if signal_action == "sell" and px > slow * (1.0 + b):
                log.info(
                    f"[{self.name}] Skip SELL {sym}: px {px:.6g} > slowSMA×(1+guard) "
                    f"(slow={slow:.6g}, {slow_guard:g}bps)"
                )
                return
            if signal_action == "buy" and px < slow * (1.0 - b):
                log.info(
                    f"[{self.name}] Skip BUY {sym}: px {px:.6g} < slowSMA×(1-guard) "
                    f"(slow={slow:.6g}, {slow_guard:g}bps)"
                )
                return

        cooldown = _effective_attack_cooldown(params)
        now = time.time()
        if now - self._attack_last_fire.get(sym, 0.0) < cooldown:
            return
        self._attack_last_fire[sym] = now

        log.info(
            f"[{self.name}] PredatorMatrix {signal_action.upper()} {sym} score={trigger_score:.1f} "
            f"w=({w_ai:.2f},{w_tech:.2f},{w_obi:.2f}) obi={self.latest_obi:.3f} "
            f"keys={list(pred_meta.keys())}"
        )

        lev = _core_grinder_leverage_capped(
            sym, risk_engine.recommended_grinder_leverage(sym)
        )
        equity = risk_engine.current_balance
        risk_cfg = config_manager.get_config().risk
        vol_profile = _core_volatility_profile(sym)
        frac = (
            margin_target_fraction(equity)
            if getattr(risk_cfg, "use_equity_tier_margin", True)
            else float(risk_cfg.max_single_risk)
        )
        max_margin = equity * frac
        notional = max_margin * lev * float(vol_profile["notional_mult"])
        qty = paper_engine.contracts_for_target_usdt_notional(sym, float(price), notional)

        if qty > 0:
            sm2 = bot_context.get_state_machine()
            pred_meta["weights_ai_tech_obi"] = [w_ai, w_tech, w_obi]
            tp_px, sl_px = _core_bracket_limit_prices(
                sym, signal_action, float(price), float(qty), leverage=int(lev)
            )
            entry_px = _core_entry_limit_price(sym, signal_action, float(price))
            self.emit_signal(
                SignalEvent(
                    strategy_name=self.name,
                    symbol=sym,
                    side=signal_action,
                    order_type="limit",
                    price=float(entry_px),
                    amount=qty,
                    leverage=int(lev),
                    post_only=True,
                    margin_mode="cross",
                    entry_context={
                        "strategy": self.name,
                        "ai_score": float(ai_score_raw),
                        "ai_score_bucket": strategy_auto_tuner._ai_bucket(float(ai_score_raw)),
                        "obi": float(self.latest_obi),
                        "trigger_score": float(trigger_score),
                        "trigger_threshold": float(chosen_threshold),
                        "ai_regime": regime.value,
                        "regime": regime.value,
                        "ai_reason": str(ai_data.get("reason", ""))[:200],
                        "scene_learning": dict(chosen_scene_bias),
                        "volatility_profile": str(vol_profile["profile"]),
                        "volatility_atr_pct": float(vol_profile["atr_pct"]),
                        "volatility_atr_ratio": float(vol_profile["atr_ratio"]),
                        "volatility_notional_mult": float(vol_profile["notional_mult"]),
                        "volatility_tp_mult": float(vol_profile["tp_mult"]),
                        "volatility_sl_mult": float(vol_profile["sl_mult"]),
                        "entry_limit_price": float(entry_px),
                        "entry_limit_post_only": True,
                        "core_limit_requote_enabled": True,
                        "core_limit_ttl_ms": int(getattr(params, "core_entry_limit_ttl_ms", 8000) or 8000),
                        "core_limit_requote_max": int(getattr(params, "core_entry_limit_requote_max", 2) or 2),
                        "resting_quote": True,
                        "trading_mode": sm2.state.value if sm2 and sm2.state else None,
                        "predator_matrix": pred_meta,
                        "take_profit_limit_price": tp_px,
                        "stop_loss_limit_price": sl_px,
                    },
                )
            )
            self._prices_by_symbol[sym] = q[-(self.window // 2) :]

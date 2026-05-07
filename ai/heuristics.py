"""
Shark 2.0 — ATR-based Heuristic Fallback.

Deterministic market analysis that mirrors the LLM's decision interface.
Used as a fallback when the LLM is unavailable, and also provides the
baseline ATR calculations consumed by strategies.

Decision logic:
  - ATR(14) for volatility gauge and stop/target placement.
  - Multi-timeframe trend detection via EMA cross and SMA slope.
  - Regime classification: STABLE / VOLATILE / TRENDING / CHAOTIC.
  - Entry signals based on price distance from moving averages.
  - Always conservative — prefers IDLE over marginal setups.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# – try importing the existing logger; if running standalone, fall back to print
# ---------------------------------------------------------------------------
try:
    from src.utils.logger import log
except ImportError:
    import logging

    log = logging.getLogger("heuristics")


# ---------------------------------------------------------------------------
# Re-use brain types (lazy to avoid circular imports at module level)
# ---------------------------------------------------------------------------
def _strategy_decision_type():
    """Lazy import to avoid circular dependency."""
    from .brain import StrategyDecision, StrategyMode, Direction

    return StrategyDecision, StrategyMode, Direction


# ---------------------------------------------------------------------------
# ATR Calculation
# ---------------------------------------------------------------------------
@dataclass
class ATRResult:
    atr: float
    atr_bps: float  # ATR expressed in basis points relative to price
    price: float
    period: int = 14


def true_range(high: float, low: float, prev_close: float) -> float:
    """Single-period true range."""
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def calculate_atr(
    candles: List[Dict[str, float]],
    period: int = 14,
    price: Optional[float] = None,
) -> ATRResult:
    """
    Calculate ATR (Average True Range) from a list of OHLCV candles.

    Uses Wilder's smoothing (exponential moving average with alpha = 1/period)
    which is the standard for ATR(14).

    Args:
        candles: list of dicts with keys open, high, low, close.
        period: ATR period (default 14).
        price: current price (defaults to last candle close).

    Returns:
        ATRResult with absolute ATR and basis-point ATR.
    """
    if len(candles) < 2:
        return ATRResult(atr=0.0, atr_bps=0.0, price=price or 0.0, period=period)

    # Compute true ranges
    tr_values: List[float] = []
    for i in range(1, len(candles)):
        prev_c = candles[i - 1].get("close", candles[i].get("open", 0.0))
        tr = true_range(
            candles[i].get("high", 0.0),
            candles[i].get("low", 0.0),
            prev_c,
        )
        tr_values.append(tr)

    # Wilder's smoothing
    if len(tr_values) <= period:
        atr = sum(tr_values) / len(tr_values)
    else:
        # First ATR = simple average of first `period` TRs
        atr = sum(tr_values[:period]) / period
        # Then EMA with alpha = 1/period
        alpha = 1.0 / period
        for tr in tr_values[period:]:
            atr = (tr * alpha) + (atr * (1 - alpha))

    current_price = price or candles[-1].get("close", candles[-1].get("open", 0.0))
    atr_bps = (atr / current_price * 1e4) if current_price > 0 else 0.0

    return ATRResult(atr=atr, atr_bps=atr_bps, price=current_price, period=period)


# ---------------------------------------------------------------------------
# Moving averages
# ---------------------------------------------------------------------------
def calc_sma(values: List[float], period: int) -> float:
    """Simple Moving Average."""
    if len(values) < period:
        return sum(values) / max(len(values), 1)
    return sum(values[-period:]) / period


def calc_ema(values: List[float], period: int) -> float:
    """Exponential Moving Average."""
    if len(values) < 2:
        return values[0] if values else 0.0
    alpha = 2.0 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = (v * alpha) + (ema * (1 - alpha))
    return ema


# ---------------------------------------------------------------------------
# Regime classification
# ---------------------------------------------------------------------------
def classify_regime(
    atr_bps: float,
    price: float,
    sma_20: float,
    sma_50: float,
    ema_12: float,
    ema_26: float,
) -> Tuple[str, str, float]:
    """
    Classify market regime and trend strength from volatility and moving averages.

    Returns:
        (regime: str, volatility_grade: str, trend_strength: float 0-100)
    """
    # Volatility grade
    if atr_bps < 5:
        vol_grade = "LOW"
    elif atr_bps < 20:
        vol_grade = "MEDIUM"
    elif atr_bps < 80:
        vol_grade = "HIGH"
    else:
        vol_grade = "EXTREME"

    # Trend detection
    price_vs_sma20 = (price - sma_20) / max(sma_20, 1e-8)  # relative distance
    price_vs_sma50 = (price - sma_50) / max(sma_50, 1e-8)
    sma_slope = (sma_20 - sma_50) / max(sma_50, 1e-8)  # golden/death cross proxy
    ema_diff = (ema_12 - ema_26) / max(ema_26, 1e-8)

    trend_score = 0.0

    # Price above both SMAs → bullish
    if price_vs_sma20 > 0 and price_vs_sma50 > 0:
        trend_score += 30
    elif price_vs_sma20 < 0 and price_vs_sma50 < 0:
        trend_score -= 30

    # SMA slope
    trend_score += max(-25, min(25, sma_slope * 500))

    # EMA cross
    trend_score += max(-25, min(25, ema_diff * 500))

    # Absolute trend strength (for classification)
    abs_trend = abs(trend_score)

    # Regime
    if abs_trend < 15 and atr_bps < 10:
        regime = "STABLE"
    elif abs_trend < 15:
        regime = "VOLATILE"
    elif trend_score > 0 and abs_trend > 25:
        regime = "TRENDING_UP"
    elif trend_score < 0 and abs_trend > 25:
        regime = "TRENDING_DOWN"
    elif atr_bps > 80:
        regime = "CHAOTIC"
    else:
        regime = "VOLATILE"

    # Normalize trend strength to 0-100
    trend_strength = max(0.0, min(100.0, abs_trend * 1.5))

    return regime, vol_grade, trend_strength


# ---------------------------------------------------------------------------
# Signal generation
# ---------------------------------------------------------------------------
def _strategy_for_regime(
    regime: str,
    atr_bps: float,
    trend_strength: float,
    sma_20: float,
    sma_50: float,
    price: float,
) -> Tuple[str, str, float]:
    """
    Determine which strategy mode fits the current regime.

    Returns: (mode, direction, confidence)
    """
    StrategyDecision, StrategyMode, Direction = _strategy_decision_type()

    # --- IDLE conditions ---
    if atr_bps < 1.0:
        return StrategyMode.IDLE.value, Direction.NEUTRAL.value, 0.0

    # --- CHAOTIC → stay out ---
    if regime == "CHAOTIC":
        return StrategyMode.IDLE.value, Direction.NEUTRAL.value, 5.0

    # --- STABLE / low-volatility → GRID_MAKER ---
    if regime == "STABLE" and atr_bps < 15:
        conf = 60.0 - atr_bps  # higher confidence in very stable markets
        conf = max(30.0, min(70.0, conf))
        return StrategyMode.GRID_MAKER.value, Direction.NEUTRAL.value, conf

    # --- STRONG TREND → MOMENTUM ---
    if regime in ("TRENDING_UP", "TRENDING_DOWN") and trend_strength > 40:
        direction = Direction.LONG.value if regime == "TRENDING_UP" else Direction.SHORT.value
        conf = min(85.0, 40.0 + trend_strength * 0.5)
        return StrategyMode.MOMENTUM.value, direction, conf

    # --- MODERATE TREND → MOMENTUM with lower confidence ---
    if trend_strength > 25:
        direction = Direction.LONG.value if price > sma_20 else Direction.SHORT.value
        conf = 30.0 + trend_strength * 0.4
        return StrategyMode.MOMENTUM.value, direction, conf

    # --- Compressed range → BREAKOUT ---
    if atr_bps < 20 and trend_strength < 25:
        # Low volatility compression
        if price > sma_50:
            return StrategyMode.BREAKOUT.value, Direction.LONG.value, 35.0
        else:
            return StrategyMode.BREAKOUT.value, Direction.SHORT.value, 35.0

    # --- Price stretched from mean → MEAN_REVERT ---
    price_dist = abs(price - sma_20) / max(sma_20, 1e-8)
    if price_dist > atr_bps / 1e4 * 2:  # price > 2 ATR from SMA20
        direction = Direction.SHORT.value if price > sma_20 else Direction.LONG.value
        conf = min(50.0, 25.0 + price_dist * 200)
        return StrategyMode.MEAN_REVERT.value, direction, conf

    # --- Default: IDLE ---
    return StrategyMode.IDLE.value, Direction.NEUTRAL.value, 10.0


def _position_sizing(atr_bps: float, confidence: float) -> Tuple[float, int]:
    """Calculate position size and leverage from volatility and confidence.

    Returns: (position_size_pct, suggested_leverage)
    """
    # Higher volatility → smaller size, lower leverage
    if atr_bps > 80:
        size = 0.02
        leverage = 1
    elif atr_bps > 40:
        size = 0.04
        leverage = 2
    elif atr_bps > 20:
        size = 0.08
        leverage = 3
    elif atr_bps > 10:
        size = 0.10
        leverage = 5
    else:
        size = 0.15
        leverage = 10

    # Scale by confidence
    size = size * (confidence / 60.0)
    size = max(0.0, min(0.25, size))

    return size, leverage


# ---------------------------------------------------------------------------
# Main heuristic decision function
# ---------------------------------------------------------------------------
def heuristic_decision(snapshot) -> "StrategyDecision":
    """
    Generate a StrategyDecision from a MarketSnapshot using only deterministic rules.

    This is the primary fallback when the LLM is unavailable.

    Args:
        snapshot: MarketSnapshot from brain.py (lazy import to avoid coupling).

    Returns:
        StrategyDecision with source='heuristic'.
    """
    StrategyDecision, StrategyMode, Direction = _strategy_decision_type()

    price = snapshot.price
    symbol = snapshot.symbol

    # Extract close prices for MA calculations
    closes_1m = [c.get("close", c.get("open", 0.0)) for c in snapshot.candles_1m] if snapshot.candles_1m else []
    closes_5m = [c.get("close", c.get("open", 0.0)) for c in snapshot.candles_5m] if snapshot.candles_5m else []
    closes_15m = [c.get("close", c.get("open", 0.0)) for c in snapshot.candles_15m] if snapshot.candles_15m else []

    # Use 15m candles for ATR if available, else 5m, else 1m
    candles_for_atr = snapshot.candles_15m if len(snapshot.candles_15m) >= 14 else (
        snapshot.candles_5m if len(snapshot.candles_5m) >= 14 else snapshot.candles_1m
    )

    # ATR
    if snapshot.atr_14 > 0:
        atr_result = ATRResult(
            atr=snapshot.atr_14,
            atr_bps=snapshot.atr_14_bps,
            price=price,
            period=14,
        )
    else:
        atr_result = calculate_atr(candles_for_atr, period=14, price=price)

    # Moving averages (use whatever data we have)
    all_closes = closes_15m or closes_5m or closes_1m
    if not all_closes and price > 0:
        all_closes = [price]

    sma_20 = snapshot.sma_20 if snapshot.sma_20 > 0 else calc_sma(all_closes, 20)
    sma_50 = snapshot.sma_50 if snapshot.sma_50 > 0 else calc_sma(all_closes, min(50, len(all_closes)))
    ema_12 = snapshot.ema_12 if snapshot.ema_12 > 0 else calc_ema(all_closes, 12)
    ema_26 = snapshot.ema_26 if snapshot.ema_26 > 0 else calc_ema(all_closes, 26)

    # Regime & trend
    regime, vol_grade, trend_strength = classify_regime(
        atr_result.atr_bps,
        price,
        sma_20,
        sma_50,
        ema_12,
        ema_26,
    )

    # Strategy selection
    mode_str, dir_str, confidence = _strategy_for_regime(
        regime, atr_result.atr_bps, trend_strength, sma_20, sma_50, price
    )

    # Position sizing
    size_pct, leverage = _position_sizing(atr_result.atr_bps, confidence)

    # Entry zone: ± 0.5 ATR around current price
    half_atr = atr_result.atr * 0.5
    entry_low = max(0.0, price - half_atr)
    entry_high = price + half_atr

    # TP/SL multipliers
    tp_mult = 2.0 if atr_result.atr_bps < 30 else 1.5
    sl_mult = 1.5 if atr_result.atr_bps < 30 else 1.0
    trail_mult = 1.0 if trend_strength > 40 else 0.0

    # Reasoning
    reasoning = (
        f"Heuristic: {regime} | ATR={atr_result.atr_bps:.1f}bps | "
        f"Trend={trend_strength:.0f} | SMA20 dist={((price-sma_20)/max(sma_20,1e-8))*100:.1f}%"
    )

    return StrategyDecision(
        symbol=symbol,
        recommended_mode=StrategyMode(mode_str),
        direction=Direction(dir_str),
        confidence=confidence,
        position_size_pct=size_pct,
        suggested_leverage=leverage,
        entry_price_zone=(entry_low, entry_high),
        take_profit_atr_mult=tp_mult,
        stop_loss_atr_mult=sl_mult,
        trailing_stop_atr_mult=trail_mult,
        regime=regime,
        volatility_grade=vol_grade,
        trend_strength=trend_strength,
        reasoning=reasoning,
        source="heuristic",
    )


# ---------------------------------------------------------------------------
# Quick risk limit check
# ---------------------------------------------------------------------------
def atr_risk_check(
    price: float,
    atr: float,
    position_size_usdt: float,
    account_equity: float,
    max_risk_pct: float = 0.02,
) -> Tuple[bool, str]:
    """
    Quick pre-trade risk check: does the stop distance exceed max risk?

    Args:
        price: current price.
        atr: ATR(14) value.
        position_size_usdt: intended position size in USDT.
        account_equity: total account equity.
        max_risk_pct: maximum % of equity at risk (default 2%).

    Returns:
        (allowed: bool, reason: str)
    """
    if account_equity <= 0:
        return False, "No equity."
    if position_size_usdt <= 0:
        return False, "Zero position size."

    stop_distance = atr * 1.5
    stop_loss_abs = (stop_distance / price) * position_size_usdt
    risk_pct = stop_loss_abs / account_equity

    if risk_pct > max_risk_pct:
        return (
            False,
            f"Risk {risk_pct:.2%} > max {max_risk_pct:.2%} (stop={stop_distance:.6f})",
        )
    return True, f"Risk {risk_pct:.2%} OK."


# ---------------------------------------------------------------------------
# BaselineHeuristic — wrapper class used by orchestrator and strategies
# ---------------------------------------------------------------------------
class BaselineHeuristic:
    """ATR-based heuristic fallback with three-tier decision logic.

    Provides the same interface as AIBrain so the orchestrator can switch
    transparently between LLM and heuristic modes.
    """

    def __init__(self):
        self.name = "BaselineHeuristic"

    def suggest_position_size(
        self,
        equity: float,
        atr_bps: float,
        confidence: float = 50.0,
    ) -> float:
        """Suggest position size as fraction of equity (0.0–1.0)."""
        if atr_bps > 80:
            base = 0.005
        elif atr_bps > 40:
            base = 0.01
        elif atr_bps > 15:
            base = 0.02
        else:
            base = 0.03
        return base * (confidence / 50.0)

    def suggest_stop_loss(
        self, price: float, atr: float, atr_mult: float = 1.5
    ) -> float:
        """Suggest stop-loss price based on ATR."""
        return price - atr * atr_mult

    def suggest_take_profit(
        self, price: float, atr: float, atr_mult: float = 2.0
    ) -> float:
        """Suggest take-profit price based on ATR."""
        return price + atr * atr_mult

    def suggest_grid_params(
        self, atr_bps: float, equity: float
    ) -> dict:
        """Suggest grid parameters for GridMaker strategy."""
        return {
            "initial_margin_frac": min(0.03, 0.01 * (100 / max(equity, 1))),
            "grid_spacing_atr_mult": 1.0 + atr_bps / 40,
            "tier1_adverse_pct": 0.02,
            "tier2_adverse_pct": 0.05,
            "tier3_adverse_pct": 0.10,
        }

    def analyze(self, snapshot) -> dict:
        """Quick heuristic analysis of a market snapshot."""
        price = getattr(snapshot, "price", getattr(snapshot, "last", 0.0))
        return {
            "symbol": getattr(snapshot, "symbol", "?"),
            "price": price,
            "regime": "STABLE",
            "confidence": 40.0,
            "direction": "hold",
            "source": "heuristic",
        }

"""Bollinger (k·σ) + RSI for Slingshot extreme mean-reversion triggers."""

from __future__ import annotations

import math
from typing import List, Optional, Tuple


def bollinger_bands(
    closes: List[float], period: int, std_mult: float
) -> Optional[Tuple[float, float, float]]:
    """Return (mid, upper, lower) or None if insufficient data."""
    if period < 2 or len(closes) < period:
        return None
    window = closes[-period:]
    mu = sum(window) / period
    var = sum((x - mu) ** 2 for x in window) / period
    sigma = math.sqrt(max(var, 0.0))
    return (mu, mu + std_mult * sigma, mu - std_mult * sigma)


def rsi_sma(closes: List[float], period: int) -> Optional[float]:
    """Classic RSI with simple average of gains/losses (adequate for very short period e.g. 3)."""
    if period < 1 or len(closes) < period + 1:
        return None
    deltas: List[float] = []
    for i in range(1, len(closes)):
        deltas.append(closes[i] - closes[i - 1])
    seg = deltas[-period:]
    avg_gain = sum(max(d, 0.0) for d in seg) / period
    avg_loss = sum(-min(d, 0.0) for d in seg) / period
    if avg_loss <= 1e-12:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

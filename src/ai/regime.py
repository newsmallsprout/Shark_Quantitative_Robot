from enum import Enum
from typing import Any, Dict

class MarketRegime(str, Enum):
    STABLE = "STABLE"
    VOLATILE = "VOLATILE"
    OSCILLATING = "STABLE"
    TRENDING_UP = "VOLATILE"
    TRENDING_DOWN = "VOLATILE"
    CHAOTIC = "VOLATILE"

class RegimeClassifier:
    """
    Classifies the current market state by querying the AI Context shared memory.
    """
    def __init__(self):
        self.current_regime = MarketRegime.OSCILLATING

    def analyze(self, symbol: str) -> MarketRegime:
        # Lazy import: avoids analyzer <-> regime import cycle at module load.
        from src.ai.analyzer import ai_context

        data = ai_context.get(symbol)
        raw = data.get("regime", MarketRegime.STABLE)
        if isinstance(raw, MarketRegime):
            return raw
        if isinstance(raw, str):
            try:
                return MarketRegime(raw)
            except ValueError:
                return MarketRegime.STABLE
        return MarketRegime.STABLE

    def snapshot(self, symbol: str) -> Dict[str, Any]:
        from src.ai.analyzer import ai_context

        data = dict(ai_context.get(symbol) or {})
        try:
            data["regime"] = self.analyze(symbol).value
        except Exception:
            data["regime"] = MarketRegime.STABLE.value
        data.setdefault("suggested_leverage_cap", 100)
        data.setdefault("tp_atr_multiplier", 2.0)
        data.setdefault("sl_atr_multiplier", 1.8)
        data.setdefault("dynamic_tp_fee_multiplier", 1.5)
        data.setdefault("dynamic_max_loss_margin_pct", 0.85)
        return data

regime_classifier = RegimeClassifier()

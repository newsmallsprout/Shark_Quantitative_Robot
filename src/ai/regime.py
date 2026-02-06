from enum import Enum
from typing import Dict

class MarketRegime(str, Enum):
    OSCILLATING = "OSCILLATING" # Shock/Range
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    CHAOTIC = "CHAOTIC" # High Volatility / Risk

class RegimeClassifier:
    """
    Classifies the current market state.
    """
    def __init__(self):
        self.current_regime = MarketRegime.OSCILLATING

    def analyze(self, ticker: Dict) -> MarketRegime:
        # Simple Heuristic: Percentage Change
        change_24h = ticker.get('percentage', 0)
        
        if change_24h > 5.0:
            return MarketRegime.TRENDING_UP
        elif change_24h < -5.0:
            return MarketRegime.TRENDING_DOWN
        elif abs(change_24h) < 1.0:
            return MarketRegime.OSCILLATING
        else:
            return MarketRegime.CHAOTIC

regime_classifier = RegimeClassifier()

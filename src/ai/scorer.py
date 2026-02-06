import random
from typing import Dict

class AIScorer:
    """
    AI Signal Scorer
    Evaluates the quality of a trading signal (0-100).
    In V1, this uses heuristic logic to emulate AI judgment.
    In V2, this will load a PyTorch model.
    """
    def __init__(self):
        self.model_version = "v1.0.0-heuristic"

    def score(self, symbol: str, ticker: Dict, signal_type: str) -> float:
        """
        Returns a score between 0 and 100.
        """
        # Feature Extraction (Mock)
        last_price = ticker.get('last', 0)
        vol_24h = ticker.get('baseVolume', 0)
        
        # Heuristic Logic:
        # 1. Volume Filter: Higher volume -> Better score
        vol_score = min(vol_24h / 1000.0, 50.0) 
        
        # 2. Random Noise (Simulating Uncertainty/AI complexity)
        noise = random.uniform(-5, 5)
        
        base_score = 50.0
        
        final_score = base_score + vol_score + noise
        
        # Cap at 0-100
        return max(0.0, min(100.0, final_score))

ai_scorer = AIScorer()

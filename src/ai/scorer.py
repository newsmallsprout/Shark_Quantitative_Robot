class AIScorer:
    """
    Side-aware score from shared AIContext (MarketAnalyzer / ZMQ).
    Buy/long approval uses bullish confidence; sell/short uses bearish (100 - score).
    """

    def __init__(self):
        pass

    def score(self, symbol: str, ticker: dict, side: str) -> float:
        from src.ai.analyzer import ai_context

        data = ai_context.get(symbol)
        raw = float(data.get("score", 50.0))
        s = (side or "buy").lower()
        if s in ("sell", "short"):
            return 100.0 - raw
        return raw

ai_scorer = AIScorer()

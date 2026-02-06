from src.strategy.base import BaseStrategy
from src.utils.logger import log
from src.utils.indicators import get_rsi
from src.ai.scorer import ai_scorer
from src.ai.regime import regime_classifier, MarketRegime
from src.ai.collector import data_collector
from src.core.config_manager import config_manager

class CoreNeutralStrategy(BaseStrategy):
    """
    Mean Reversion Strategy for Oscillating Markets.
    """
    def __init__(self):
        super().__init__("CoreNeutral")

    async def on_tick(self, exchange, symbol: str, ticker: dict, balance: float):
        last_price = float(ticker['last'])
        
        # Get Dynamic Config
        config = config_manager.get_config().strategy.params
        
        # 1. Update Indicators
        rsi = get_rsi(symbol, last_price)
        
        # 2. AI Market Regime Check
        regime = regime_classifier.analyze(ticker)
        
        # 3. Logic: Only trade if Oscillating
        if regime != MarketRegime.OSCILLATING:
            return # Skip if trending
            
        # 4. Generate Signal
        signal = None
        if rsi < config.neutral_rsi_buy:
            signal = "buy"
        elif rsi > config.neutral_rsi_sell:
            signal = "sell"
            
        if not signal:
            return

        # 5. AI Scoring
        score = ai_scorer.score(symbol, ticker, signal)
        
        # 6. Filter by AI Score
        if score < config.neutral_ai_threshold:
            await self.log(f"Signal REJECTED by AI. Score: {score:.1f}")
            data_collector.log_execution(symbol, ticker, regime, score, "REJECTED")
            return
            
        # 7. Execute (Mock Execution for V1)
        # In real V2, call exchange.create_order
        await self.log(f"Signal APPROVED. {signal.upper()} {symbol} @ {last_price}. RSI: {rsi:.1f}, AI Score: {score:.1f}")
        
        # Log to data collector
        data_collector.log_execution(symbol, ticker, regime, score, signal.upper())


class CoreAttackStrategy(BaseStrategy):
    """
    Trend Following Strategy for Trending Markets.
    """
    def __init__(self):
        super().__init__("CoreAttack")

    async def on_tick(self, exchange, symbol: str, ticker: dict, balance: float):
        last_price = float(ticker['last'])
        
        # Get Dynamic Config
        config = config_manager.get_config().strategy.params
        
        # 1. AI Market Regime Check
        regime = regime_classifier.analyze(ticker)
        
        # 2. Logic: Only trade if Trending
        signal = None
        if regime == MarketRegime.TRENDING_UP:
            signal = "buy"
        elif regime == MarketRegime.TRENDING_DOWN:
            signal = "sell"
            
        if not signal:
            return

        # 3. AI Scoring
        score = ai_scorer.score(symbol, ticker, signal)
        
        # 4. Filter by AI Score (Higher threshold for Attack)
        if score < config.attack_ai_threshold:
            # Attack requires higher confidence
            return
            
        # 5. Execute
        await self.log(f"ATTACK Signal. {signal.upper()} {symbol} @ {last_price}. Regime: {regime}, AI Score: {score:.1f}")
        data_collector.log_execution(symbol, ticker, regime, score, f"ATTACK_{signal.upper()}")

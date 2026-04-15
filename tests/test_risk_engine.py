import unittest
import time
from src.core.risk_engine import RiskEngine, RiskRejection
from src.core.config_manager import config_manager

class TestRiskEngine(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures before each test method."""
        self.risk_engine = RiskEngine()
        self.risk_engine.update_balance(10000.0) # Set initial balance to $10,000
        
        # Mock global config for consistent testing
        config_manager.config.risk.max_single_risk = 0.05
        config_manager.config.risk.use_equity_tier_margin = False
        config_manager.config.risk.daily_drawdown_limit = 0.08
        config_manager.config.risk.hard_drawdown_limit = 0.15
        config_manager.config.risk.max_leverage = 200
        config_manager.config.risk.max_margin_per_trade_usdt = 10.0
        config_manager.config.risk.max_notional_per_trade_usdt = 2000.0
        config_manager.config.risk.grinder_leverage_min = 10
        config_manager.config.risk.grinder_leverage_max = 20
        config_manager.config.risk.sniper_leadlag_max_single_risk = 0.02
        config_manager.config.risk.sniper_leadlag_max_leverage = 15
        config_manager.config.risk.max_orders_per_second = 5
        # 单测需验证回撤停机；勿受仓库 settings.yaml 中 drawdown_halt_trading=false 影响
        config_manager.config.risk.drawdown_halt_trading = True

    def test_normal_order_passes(self):
        """Test that a normal order within limits passes."""
        order = {
            'symbol': 'BTC/USDT',
            'side': 'buy',
            'type': 'market',
            'amount': 0.01,
            'price': 50000.0, # Notional = $500
            'leverage': 5  # Margin = $100 (< $500 limit)
        }
        self.assertTrue(self.risk_engine.check_order(order))

    def test_single_risk_is_compressed_not_rejected(self):
        """超单笔风险时应压缩保证金/名义，而不是直接拒绝。"""
        order = {
            'symbol': 'BTC/USDT',
            'side': 'buy',
            'type': 'market',
            'amount': 0.1, # Too large!
            'price': 50000.0, # Notional = $5000
            'leverage': 5  # Margin = $1000 (> $500 limit)
        }
        self.assertTrue(self.risk_engine.check_order(order))
        self.assertLessEqual(order['margin_amount'], 10.0 + 1e-9)
        self.assertLessEqual(order['notional_size'], 50.0 + 1e-9)

    def test_high_leverage_is_compressed_not_rejected(self):
        """高杠杆不应直接拒绝，而应按 margin/notional 约束压缩敞口。"""
        order = {
            'symbol': 'BTC/USDT',
            'side': 'buy',
            'type': 'market',
            'amount': 0.01,
            'price': 50000.0,
            'leverage': 131,
            'entry_context': {'symbol_limits': {'min_notional_usdt': 5.0}}
        }
        self.assertTrue(self.risk_engine.check_order(order))
        self.assertEqual(order['leverage'], 131.0)
        self.assertLessEqual(order['margin_amount'], 10.0 + 1e-9)
        self.assertLessEqual(order['notional_size'], 1310.0 + 1e-9)

    def test_reject_when_compressed_notional_below_exchange_minimum(self):
        order = {
            'symbol': 'BTC/USDT',
            'side': 'buy',
            'type': 'market',
            'amount': 0.01,
            'price': 50000.0,
            'leverage': 1,
            'entry_context': {'symbol_limits': {'min_notional_usdt': 2500.0}}
        }
        with self.assertRaises(RiskRejection) as context:
            self.risk_engine.check_order(order)
        self.assertIn("below exchange minimum", str(context.exception))

    def test_daily_drawdown_halt(self):
        """Test system halts when daily drawdown limit is breached."""
        # Simulate an 8.5% loss (drops below 9200)
        self.risk_engine.update_balance(9150.0)
        
        self.assertTrue(self.risk_engine.is_halted)
        self.assertIn("DAILY DRAWDOWN LIMIT REACHED", self.risk_engine.halt_reason)
        
        # New opening order should be rejected
        order_open = {
            'symbol': 'ETH/USDT',
            'side': 'buy',
            'amount': 1,
            'price': 2000.0,
            'leverage': 1
        }
        with self.assertRaises(RiskRejection) as context:
            self.risk_engine.check_order(order_open)
        self.assertIn("System halted", str(context.exception))
        
        # Reduce-only order (closing position) should STILL pass even when halted
        order_close = {
            'symbol': 'BTC/USDT',
            'side': 'sell',
            'amount': 0.05,
            'price': 50000.0,
            'leverage': 1,
            'reduce_only': True
        }
        self.assertTrue(self.risk_engine.check_order(order_close))

    def test_hard_drawdown_halt(self):
        """Test critical halt when hard drawdown limit is breached."""
        # Simulate a 16% loss (drops below 8500)
        self.risk_engine.update_balance(8400.0)
        
        self.assertTrue(self.risk_engine.is_halted)
        self.assertIn("HARD DRAWDOWN LIMIT REACHED", self.risk_engine.halt_reason)

    def test_frequency_interception(self):
        """Test order frequency limit (e.g., 5 orders per second)."""
        order = {
            'symbol': 'BTC/USDT',
            'side': 'buy',
            'amount': 0.001,
            'price': 50000.0,
            'leverage': 1
        }
        
        # Send 5 orders rapidly (should pass)
        for _ in range(5):
            self.assertTrue(self.risk_engine.check_order(order))
            
        # 6th order should hit the limit (max 5 ops/sec)
        with self.assertRaises(RiskRejection) as context:
            self.risk_engine.check_order(order)
        self.assertIn("Order frequency exceeded", str(context.exception))
        
        # Wait 1 second
        time.sleep(1.1)
        
        # Should be able to order again
        self.assertTrue(self.risk_engine.check_order(order))

    def test_leadlag_silo_margin_cap(self):
        config_manager.config.risk.sniper_leadlag_max_single_risk = 0.02
        order = {
            "symbol": "PEPE/USDT",
            "side": "buy",
            "type": "market",
            "amount": 10.01,
            "price": 100.0,
            "leverage": 5,
            "entry_context": {"position_silo": "SNIPER_LEADLAG"},
        }
        with self.assertRaises(RiskRejection) as context:
            self.risk_engine.check_order(order)
        self.assertIn("LeadLag silo margin", str(context.exception))

    def test_leadlag_silo_leverage_cap(self):
        config_manager.config.risk.sniper_leadlag_max_leverage = 12
        order = {
            "symbol": "DOGE/USDT",
            "side": "buy",
            "type": "market",
            "amount": 1.0,
            "price": 1.0,
            "leverage": 15,
            "entry_context": {"position_silo": "SNIPER_LEADLAG"},
        }
        with self.assertRaises(RiskRejection) as context:
            self.risk_engine.check_order(order)
        self.assertIn("LeadLag silo leverage", str(context.exception))

if __name__ == '__main__':
    unittest.main()

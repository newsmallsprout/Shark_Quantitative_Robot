"""Taker/Maker 分支与名义手续费。"""
import unittest

from src.core.paper_engine import PaperTradingEngine


class TestFeeModel(unittest.TestCase):
    def test_market_uses_taker(self):
        eng = PaperTradingEngine(initial_balance=10_000.0)
        eng.taker_fee = 0.0005
        eng.maker_fee = 0.0001
        eng.latest_prices["X/USDT"] = 100.0
        eng.orderbooks_cache["X/USDT"] = {
            "asks": [["100.0", "1000"]],
            "bids": [["99.9", "1000"]],
        }
        bal0 = eng.initial_balance
        eng.execute_order("X/USDT", "buy", 1.0, None, leverage=1)
        fee_expect = 100.0 * 1.0 * 0.0005
        self.assertAlmostEqual(bal0 - eng.initial_balance, fee_expect, places=8)

    def test_limit_aggressive_uses_taker_not_maker(self):
        eng = PaperTradingEngine(initial_balance=10_000.0)
        eng.taker_fee = 0.0005
        eng.maker_fee = 0.0001
        bal0 = eng.initial_balance
        eng.execute_order(
            "X/USDT",
            "buy",
            1.0,
            100.0,
            post_only=False,
            leverage=1,
            entry_context={},
        )
        fee_expect = 100.0 * 1.0 * 0.0005
        self.assertAlmostEqual(bal0 - eng.initial_balance, fee_expect, places=8)

    def test_maker_filled_leg_uses_maker(self):
        eng = PaperTradingEngine(initial_balance=10_000.0)
        eng.taker_fee = 0.0005
        eng.maker_fee = 0.0001
        bal0 = eng.initial_balance
        eng.execute_order(
            "X/USDT",
            "buy",
            1.0,
            100.0,
            post_only=False,
            leverage=1,
            entry_context={"maker_filled": True},
        )
        fee_expect = 100.0 * 1.0 * 0.0001
        self.assertAlmostEqual(bal0 - eng.initial_balance, fee_expect, places=8)


if __name__ == "__main__":
    unittest.main()

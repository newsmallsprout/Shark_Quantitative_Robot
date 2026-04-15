"""纸面预估平仓净利：手续费与毛利关系。"""
import unittest

from src.core.paper_engine import PaperTradingEngine


class TestFlatNetEstimate(unittest.TestCase):
    def test_long_small_move_net_negative_after_fees(self):
        pe = PaperTradingEngine(initial_balance=10_000.0)
        pe.taker_fee = 0.0005
        pe.maker_fee = 0.0002
        pos = {
            "side": "long",
            "entry_price": 100.0,
            "size": 1.0,
            "accumulated_fees": 100.0 * 1.0 * 0.0005,
        }
        exit_px = 100.04
        est = pe.estimate_flat_net_pnl("T/USDT", pos, exit_px)
        gross = 0.04
        fee_close = exit_px * 1.0 * 0.0005
        want = gross - pos["accumulated_fees"] - fee_close
        self.assertAlmostEqual(est, want, places=6)
        self.assertLess(est, 0.0)

    def test_short_favorable_still_net_negative_if_tiny(self):
        pe = PaperTradingEngine(initial_balance=10_000.0)
        pe.taker_fee = 0.0005
        pos = {
            "side": "short",
            "entry_price": 100.0,
            "size": 1.0,
            "accumulated_fees": 100.0 * 1.0 * 0.0005,
        }
        exit_px = 99.96
        est = pe.estimate_flat_net_pnl("T/USDT", pos, exit_px)
        gross = 100.0 - 99.96
        fee_close = 99.96 * 0.0005
        self.assertAlmostEqual(est, gross - pos["accumulated_fees"] - fee_close, places=5)


if __name__ == "__main__":
    unittest.main()

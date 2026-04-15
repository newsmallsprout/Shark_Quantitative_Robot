"""权益分档保证金区间与狂鲨净利缩放。"""
import unittest

from src.core.equity_sizing import (
    margin_bounds_for_equity,
    margin_cap_fraction,
    margin_target_fraction,
    shark_net_tp_sl_usdt,
)


class TestEquitySizing(unittest.TestCase):
    def test_margin_tiers(self):
        self.assertEqual(margin_bounds_for_equity(100.0), (0.10, 0.20))
        self.assertEqual(margin_cap_fraction(100.0), 0.20)
        self.assertAlmostEqual(margin_target_fraction(100.0), 0.15, places=6)

        lo1k, hi1k = margin_bounds_for_equity(1000.0)
        self.assertAlmostEqual(hi1k, 0.10, places=6)
        self.assertAlmostEqual(lo1k, 0.05, places=6)

        lo10k, hi10k = margin_bounds_for_equity(10000.0)
        self.assertAlmostEqual(lo10k, 0.03, places=6)
        self.assertAlmostEqual(hi10k, 0.05, places=6)

    def test_shark_tp_sl_scale(self):
        t, r = shark_net_tp_sl_usdt(100.0, 0.01, 0.005)
        self.assertAlmostEqual(t, 1.0, places=6)
        self.assertAlmostEqual(r, 0.5, places=6)
        t2, r2 = shark_net_tp_sl_usdt(1000.0, 0.01, 0.005)
        self.assertAlmostEqual(t2, 10.0, places=6)
        self.assertAlmostEqual(r2, 5.0, places=6)


if __name__ == "__main__":
    unittest.main()

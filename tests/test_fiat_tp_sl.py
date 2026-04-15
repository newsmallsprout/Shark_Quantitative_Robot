import unittest

from src.core.fiat_tp_sl import compute_tp_sl_prices_net_usdt, estimate_fees_usdt


class TestFiatTpSl(unittest.TestCase):
    def test_long_zero_fees_simple(self):
        tp, sl = compute_tp_sl_prices_net_usdt(
            "long",
            100.0,
            1.0,
            1.0,
            target_net_usdt=20.0,
            risk_net_usdt=10.0,
            fee_open_usdt=0.0,
            fee_close_tp_usdt=0.0,
            fee_close_sl_usdt=0.0,
        )
        self.assertAlmostEqual(tp, 120.0, places=6)
        self.assertAlmostEqual(sl, 90.0, places=6)

    def test_long_with_fees(self):
        tp, sl = compute_tp_sl_prices_net_usdt(
            "long",
            50.0,
            2.0,
            0.5,
            target_net_usdt=20.0,
            risk_net_usdt=10.0,
            fee_open_usdt=2.0,
            fee_close_tp_usdt=1.0,
            fee_close_sl_usdt=1.0,
        )
        q = 2.0 * 0.5
        self.assertAlmostEqual(tp, 50.0 + (20 + 2 + 1) / q, places=6)
        self.assertAlmostEqual(sl, 50.0 - (10 - 2 - 1) / q, places=6)

    def test_short_zero_fees(self):
        tp, sl = compute_tp_sl_prices_net_usdt(
            "short",
            100.0,
            1.0,
            1.0,
            target_net_usdt=20.0,
            risk_net_usdt=10.0,
            fee_open_usdt=0.0,
            fee_close_tp_usdt=0.0,
            fee_close_sl_usdt=0.0,
        )
        self.assertAlmostEqual(tp, 80.0, places=6)
        self.assertAlmostEqual(sl, 110.0, places=6)

    def test_estimate_fees(self):
        fo, fctp, fcsl = estimate_fees_usdt(
            1000.0,
            1010.0,
            990.0,
            taker_rate=0.0005,
            maker_rate=0.0002,
            tp_as_maker=True,
            sl_as_taker=True,
        )
        self.assertAlmostEqual(fo, 0.5, places=8)
        self.assertAlmostEqual(fctp, 1010 * 0.0002, places=8)
        self.assertAlmostEqual(fcsl, 990 * 0.0005, places=8)


if __name__ == "__main__":
    unittest.main()

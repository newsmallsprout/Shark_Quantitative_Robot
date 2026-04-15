import unittest

from src.core.assassin_cost import (
    assassin_hurdle_rate,
    long_entry_net_floor_ok,
    long_hard_tp_price,
    reversion_space_fraction_long,
)


class _FakePE:
    taker_fee = 0.0005
    maker_fee = 0.0002

    def _best_bid_ask(self, symbol: str):
        return (100.0, 100.2)


class TestAssassinCost(unittest.TestCase):
    def test_hurdle_includes_fees_and_spread(self):
        pe = _FakePE()
        h = assassin_hurdle_rate("X/USDT", pe)
        mid = 100.1
        spread = (100.2 - 100.0) / mid
        want = 0.0005 + 0.0002 + spread
        self.assertAlmostEqual(h, want, places=8)

    def test_net_space_gate(self):
        last = 99.0
        vwap = 100.0
        space = reversion_space_fraction_long(last, vwap)
        self.assertAlmostEqual(space, (100 - 99) / 99, places=8)
        hurdle = 0.001
        ok, why = long_entry_net_floor_ok(last, vwap, hurdle, 2.5, 0.0, 0.0005)
        self.assertTrue(ok, why)
        ok2, _ = long_entry_net_floor_ok(last, vwap, hurdle, 50.0, 0.0, 0.0005)
        self.assertFalse(ok2)

    def test_tp_probe_blocks_when_at_or_above_vwap(self):
        last = 99.99
        vwap = 100.0
        hurdle = 0.0001
        tn = 0.0021
        tp = long_hard_tp_price(last, hurdle, tn)
        self.assertGreaterEqual(tp, vwap)
        ok, why = long_entry_net_floor_ok(last, vwap, hurdle, 1.0, 0.0, tn)
        self.assertFalse(ok)
        self.assertEqual(why, "tp_ge_vwap")


if __name__ == "__main__":
    unittest.main()

import unittest

from src.core import l1_fast_loop
from src.core.assassin_gate import assassin_entry_blocked, clear_assassin_cooldown_for_tests
from src.core.config_manager import config_manager
from src.core.paper_engine import paper_engine


class TestAssassinVwap(unittest.TestCase):
    def test_rolling_vwap(self):
        sym = "TSTVWAP/ONLY"
        t0 = 2_000_000.0
        for i in range(5):
            l1_fast_loop.ingest_trades(
                sym,
                [
                    {
                        "create_time": t0 + i * 0.5,
                        "size": 1.0,
                        "side": "buy",
                        "price": 100.0 + i,
                    }
                ],
            )
        v = l1_fast_loop.rolling_trade_vwap(sym, window=60.0, now=t0 + 10)
        self.assertIsNotNone(v)
        self.assertGreater(v, 100.0)

    def test_sell_exhaustion_quiet_tail(self):
        sym = "TSTEXH/ONLY"
        t0 = 1_000_000.0
        for i in range(50):
            l1_fast_loop.ingest_trades(
                sym,
                [{"create_time": t0 + i * 0.05, "size": 2.0, "side": "sell", "price": 50.0}],
            )
        now = t0 + 10.0
        ok = l1_fast_loop.taker_sell_exhausted(
            sym, burst_sec=3.0, baseline_sec=10.0, ratio=0.1, min_baseline_vol=1.0, now=now
        )
        self.assertTrue(ok)


class TestAssassinPaper(unittest.TestCase):
    def setUp(self):
        self.ac = config_manager.config.assassin_micro
        self._saved = self.ac.model_dump()

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(self.ac, k, v)
        clear_assassin_cooldown_for_tests("AP/USDT")
        paper_engine.positions.pop("AP/USDT", None)
        paper_engine._maker_resting.pop("AP/USDT", None)

    def test_brackets_and_entry_mutex(self):
        sym = "AP/USDT"
        self.ac.use_cost_aware = False
        self.ac.cooldown_sec = 10.0
        clear_assassin_cooldown_for_tests(sym)
        paper_engine.positions.pop(sym, None)
        paper_engine._maker_resting.pop(sym, None)
        paper_engine.latest_prices[sym] = 100.0
        paper_engine.orderbooks_cache[sym] = {
            "bids": [["99.9", "100"]],
            "asks": [["100.1", "100"]],
        }
        paper_engine.execute_order(
            sym,
            "buy",
            1.0,
            100.0,
            leverage=10,
            entry_context={
                "assassin_managed": True,
                "assassin_target_vwap": 100.5,
                "assassin_tp_path_fraction": 0.8,
                "assassin_sl_bps": 25.0,
            },
        )
        self.assertTrue(assassin_entry_blocked(sym))
        rest = paper_engine._maker_resting.get(sym, [])
        self.assertEqual(len(rest), 2)
        tp = next(x for x in rest if x.get("bracket_role") == "tp")
        entry = 100.0
        want_tp = entry + 0.8 * (100.5 - entry)
        self.assertAlmostEqual(float(tp["price"]), want_tp, places=6)

        paper_engine.execute_order(sym, "sell", 1.0, None, reduce_only=True)
        self.assertFalse(assassin_entry_blocked(sym))

    def test_cost_aware_tp_from_hurdle_and_net(self):
        sym = "AP2/USDT"
        self.ac.use_cost_aware = True
        clear_assassin_cooldown_for_tests(sym)
        paper_engine.positions.pop(sym, None)
        paper_engine._maker_resting.pop(sym, None)
        paper_engine.taker_fee = 0.0005
        paper_engine.maker_fee = 0.0002
        paper_engine.latest_prices[sym] = 100.0
        paper_engine.orderbooks_cache[sym] = {"bids": [["100.0", "10"]], "asks": [["100.0", "10"]]}
        hurdle = 0.0005 + 0.0002 + 0.0
        tn = 0.0005
        paper_engine.execute_order(
            sym,
            "buy",
            1.0,
            100.0,
            leverage=10,
            entry_context={
                "assassin_managed": True,
                "assassin_cost_aware": True,
                "assassin_target_vwap": 100.5,
                "assassin_target_net_frac": tn,
                "assassin_sl_bps": 25.0,
            },
        )
        rest = paper_engine._maker_resting.get(sym, [])
        self.assertEqual(len(rest), 2)
        tp = next(x for x in rest if x.get("bracket_role") == "tp")
        want_tp = 100.0 * (1.0 + hurdle + tn)
        self.assertAlmostEqual(float(tp["price"]), want_tp, places=8)
        paper_engine.execute_order(sym, "sell", 1.0, None, reduce_only=True)


if __name__ == "__main__":
    unittest.main()

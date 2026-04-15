"""Slingshot brackets on paper engine (OCO TP + stop leg)."""

import unittest

from src.core.config_manager import config_manager
from src.core.paper_engine import PaperTradingEngine


class TestSlingshotPaper(unittest.TestCase):
    def setUp(self):
        self.sc = config_manager.config.slingshot
        self._saved = self.sc.model_dump()
        self.sc.tp_bps = 20.0
        self.sc.sl_bps = 75.0
        self.eng = PaperTradingEngine()

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(self.sc, k, v)

    def test_slingshot_brackets_on_open(self):
        sym = "SS/USDT"
        self.eng.latest_prices[sym] = 50.0
        self.eng.orderbooks_cache[sym] = {"bids": [["49.9", "10"]], "asks": [["50.1", "10"]]}
        self.eng.execute_order(
            sym,
            "buy",
            2.0,
            50.0,
            leverage=10,
            entry_context={"slingshot_managed": True},
        )
        rest = self.eng._maker_resting.get(sym, [])
        self.assertEqual(len(rest), 2)
        tp = next(x for x in rest if x.get("bracket_role") == "tp")
        self.assertAlmostEqual(float(tp["price"]), 50.0 * 1.002, places=6)


if __name__ == "__main__":
    unittest.main()

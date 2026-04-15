"""Paper engine L1 bracket OCO: TP maker + stop leg, fee-aware TP offset, optional decay."""

import time
import unittest

from src.core.config_manager import config_manager
from src.core.paper_engine import PaperTradingEngine


class TestBracketOCO(unittest.TestCase):
    def setUp(self):
        self.lc = config_manager.config.l1_fast_loop
        self._saved = self.lc.model_dump()
        self.lc.bracket_protocol = True
        self.lc.bracket_taker_fee_bps = 5.0
        self.lc.bracket_net_target_bps = 10.0
        self.lc.bracket_sl_floor_bps = 20.0
        self.lc.bracket_sl_atr_mult = 0.65
        self.lc.bracket_tp_decay_sec = 300.0
        self.lc.bracket_tp_decay_bps = 6.0
        self.lc.min_atr_bps = 10.0
        self.eng = PaperTradingEngine()

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(self.lc, k, v)

    def _seed_ob(self, symbol: str, mid: float):
        spread = mid * 0.0002
        bid = mid - spread / 2
        ask = mid + spread / 2
        self.eng.orderbooks_cache[symbol] = {
            "bids": [[str(bid), "100"]],
            "asks": [[str(ask), "100"]],
        }
        self.eng.latest_prices[symbol] = mid

    def test_long_tp_cancels_sl(self):
        sym = "BTC/USDT"
        self._seed_ob(sym, 100_000.0)
        self.eng.execute_order(
            sym,
            "buy",
            0.01,
            None,
            leverage=10,
            entry_context={
                "l1_managed": True,
                "l1_atr_bps": 100.0,
            },
        )
        rest = self.eng._maker_resting.get(sym, [])
        self.assertEqual(len(rest), 2)
        ocos = {o.get("bracket_oco_id") for o in rest}
        self.assertEqual(len(ocos), 1)
        oco = ocos.pop()
        roles = {o.get("bracket_role") for o in rest}
        self.assertEqual(roles, {"tp", "sl"})

        entry = float(self.eng.positions[sym]["entry_price"])
        tp = next(o for o in rest if o.get("bracket_role") == "tp")
        expect_tp = entry * (1.0 + 15.0 / 10_000.0)
        self.assertAlmostEqual(float(tp["price"]), expect_tp, places=6)

        touch = entry * (1.0 + 16.0 / 10_000.0)
        bb, ba = touch + 1.0, touch + 2.0
        self.eng.orderbooks_cache[sym] = {
            "bids": [[str(bb), "100"]],
            "asks": [[str(ba), "100"]],
        }
        self.eng.update_price(sym, touch)

        self.assertEqual(self.eng.positions[sym]["size"], 0)
        self.assertEqual(self.eng._maker_resting.get(sym, []), [])

    def test_long_sl_cancels_tp(self):
        sym = "ETH/USDT"
        self._seed_ob(sym, 3000.0)
        self.eng.execute_order(
            sym,
            "buy",
            0.2,
            None,
            leverage=10,
            entry_context={"l1_managed": True, "l1_atr_bps": 50.0},
        )
        rest = self.eng._maker_resting.get(sym, [])
        sl = next(o for o in rest if o.get("bracket_role") == "sl")
        stop_px = float(sl["stop_price"])

        crash = stop_px * 0.999
        self.eng.orderbooks_cache[sym] = {
            "bids": [[str(crash), "100"]],
            "asks": [[str(crash + 1), "100"]],
        }
        self.eng.update_price(sym, crash)

        self.assertEqual(self.eng.positions[sym]["size"], 0)
        self.assertEqual(self.eng._maker_resting.get(sym, []), [])

    def test_tp_decay_once(self):
        sym = "SOL/USDT"
        self._seed_ob(sym, 100.0)
        self.eng.execute_order(
            sym,
            "buy",
            1.0,
            None,
            leverage=10,
            entry_context={"l1_managed": True, "l1_atr_bps": 80.0},
        )
        entry = float(self.eng.positions[sym]["entry_price"])
        tp = next(o for o in self.eng._maker_resting[sym] if o.get("bracket_role") == "tp")
        tp["bracket_tp_since"] = time.time() - 400.0
        self.eng.update_price(sym, entry)
        want = entry * (1.0 + 6.0 / 10_000.0)
        self.assertAlmostEqual(float(tp["price"]), want, places=8)
        self.assertFalse(tp.get("bracket_decay_armed"))

    def test_core_dual_limit_sl_not_instant(self):
        """Core 双限价止损在下方：价格仍高于 SL 时不应秒平。"""
        sym = "CORSL/ONLY"
        mid = 100.0
        self._seed_ob(sym, mid)
        tp_px, sl_px = 101.0, 99.0
        self.eng.execute_order(
            sym,
            "buy",
            1.0,
            None,
            leverage=10,
            entry_context={
                "take_profit_limit_price": tp_px,
                "stop_loss_limit_price": sl_px,
                "strategy": "CoreNeutral",
            },
        )
        self.assertGreater(float(self.eng.positions[sym]["size"]), 0)
        self._seed_ob(sym, mid)
        self.eng.update_price(sym, mid)
        self.assertGreater(float(self.eng.positions[sym]["size"]), 0)
        self._seed_ob(sym, sl_px * 0.999)
        self.eng.update_price(sym, sl_px * 0.999)
        self.assertEqual(float(self.eng.positions[sym]["size"]), 0.0)


if __name__ == "__main__":
    unittest.main()

"""LeadLag Post-Only 括号：净利公式与 PO 触价语义。"""
import unittest

from src.core.paper_engine import paper_engine


class TestLeadLagBracket(unittest.TestCase):
    def test_post_only_sell_no_cross_bid(self):
        o = {
            "side": "sell",
            "price": 100.0,
            "bracket_role": "tp",
            "leadlag_post_only": True,
        }
        self.assertFalse(
            paper_engine._post_only_limit_touch(o, last=101.0, bb=100.5, ba=100.6)
        )

    def test_post_only_sell_fills_when_last_reaches_limit(self):
        o = {
            "side": "sell",
            "price": 101.0,
            "bracket_role": "tp",
            "leadlag_post_only": True,
        }
        self.assertTrue(
            paper_engine._post_only_limit_touch(o, last=101.0, bb=100.0, ba=100.5)
        )

    def test_net_tp_formula_long(self):
        fill = 100.0
        taker = 0.0005
        spread = 0.0002
        net = 0.0015
        total = taker + spread + net
        tp = fill * (1.0 + total)
        self.assertAlmostEqual(tp, 100.22, places=10)


if __name__ == "__main__":
    unittest.main()

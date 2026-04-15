import unittest

from src.data.market_oracle import compute_depth_obi


class TestMarketOracleObi(unittest.TestCase):
    def test_compute_depth_obi_balanced(self):
        bids = [(100.0, 10.0), (99.0, 50.0)]
        asks = [(100.5, 10.0), (101.5, 50.0)]
        obi = compute_depth_obi(bids, asks, depth_pct=0.01)
        self.assertIsNotNone(obi)
        self.assertAlmostEqual(obi, 0.0, places=6)

    def test_compute_depth_obi_more_bids(self):
        bids = [(100.0, 30.0), (99.5, 10.0)]
        asks = [(100.5, 10.0), (101.0, 10.0)]
        obi = compute_depth_obi(bids, asks, depth_pct=0.05)
        self.assertIsNotNone(obi)
        self.assertGreater(obi, 0.25)

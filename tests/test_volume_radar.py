import time
import unittest
from collections import deque

from src.core.volume_radar import VolumeRadar, _hurdle_from_ticker_row


class TestVolumeRadarHurdle(unittest.TestCase):
    def test_hurdle_includes_spread(self):
        row = {"highest_bid": "100.0", "lowest_ask": "100.5"}
        h = _hurdle_from_ticker_row(row, 0.0005, 0.0002)
        mid = 100.25
        sp = 0.5 / mid
        self.assertAlmostEqual(h, 0.0005 + 0.0002 + sp, places=6)

    def test_hurdle_no_book(self):
        row = {"last": "50"}
        h = _hurdle_from_ticker_row(row, 0.001, 0.0)
        self.assertAlmostEqual(h, 0.001, places=6)


class TestVelocityRatio(unittest.TestCase):
    def test_ratio_10x_when_window_matches(self):
        r = VolumeRadar()
        sym = "ZZ/USDT"
        now = time.time()
        r._hist[sym] = deque([(now - 250.0, 2800.0)])
        v_now = 2900.0
        ratio, span = r._velocity_ratio(sym, v_now, now, 180.0)
        self.assertGreaterEqual(span, 180.0)
        self.assertIsNotNone(ratio)
        avg5 = v_now / 288.0
        want = 100.0 / avg5
        self.assertAlmostEqual(ratio, want, places=3)


if __name__ == "__main__":
    unittest.main()

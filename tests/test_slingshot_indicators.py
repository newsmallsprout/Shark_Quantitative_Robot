import unittest

from src.core.slingshot_indicators import bollinger_bands, rsi_sma


class TestSlingshotIndicators(unittest.TestCase):
    def test_bollinger_3sigma(self):
        closes = [100.0 + i * 0.1 for i in range(25)]
        bb = bollinger_bands(closes, 20, 3.0)
        self.assertIsNotNone(bb)
        mu, up, lo = bb
        self.assertGreater(up, mu)
        self.assertLess(lo, mu)

    def test_rsi_low_after_drop(self):
        closes = [100.0, 100.0, 100.0, 100.0, 92.0]
        r = rsi_sma(closes, 3)
        self.assertIsNotNone(r)
        self.assertLess(r, 30.0)


if __name__ == "__main__":
    unittest.main()

import json
import time
import unittest

from execution.plan_gate import PlanGate


class FakeRedis:
    def __init__(self, values):
        self.values = values

    def get(self, key):
        value = self.values.get(key)
        if value is None:
            return None
        return json.dumps(value)

    def scan_iter(self, match=None, count=None):
        return iter(self.values.keys())


def make_plan(**overrides):
    plan = {
        "symbol": "BTC/USDT",
        "valid_until": time.time() + 60,
        "state": "LIVE",
        "news_risk_level": 0,
        "bias": "long",
        "range_low": 95.0,
        "range_high": 105.0,
        "entry_zone_low": 98.0,
        "entry_zone_high": 101.0,
        "stop_loss": 94.0,
        "take_profit": [104.0],
        "macro_regime": "trend_up",
    }
    plan.update(overrides)
    return plan


class PlanGateTest(unittest.TestCase):
    def gate_for(self, symbol, plan):
        return PlanGate(FakeRedis({f"shark:plan:{symbol}": plan}))

    def test_rejects_side_that_conflicts_with_single_direction_bias(self):
        gate = self.gate_for("BTC/USDT", make_plan(bias="long"))

        allowed, reason = gate.can_open("BTC/USDT", "short", 100.0)

        self.assertFalse(allowed)
        self.assertIn("方向", reason)

    def test_rejects_single_direction_entry_outside_entry_zone(self):
        gate = self.gate_for("BTC/USDT", make_plan(bias="long"))

        allowed, reason = gate.can_open("BTC/USDT", "long", 103.0)

        self.assertTrue(allowed)
        self.assertEqual(reason, "OK")

    def test_allows_both_direction_entry_inside_range_even_outside_matching_entry_zone(self):
        plan = make_plan(
            bias="both",
            range_low=90.0,
            range_high=110.0,
            long_entry_low=91.0,
            long_entry_high=95.0,
            short_entry_low=105.0,
            short_entry_high=109.0,
            long_stop_loss=88.0,
            short_stop_loss=112.0,
        )
        gate = self.gate_for("BTC/USDT", plan)

        allowed, reason = gate.can_open("BTC/USDT", "long", 100.0)

        self.assertTrue(allowed)
        self.assertEqual(reason, "OK")


if __name__ == "__main__":
    unittest.main()

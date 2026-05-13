import json
import os
import unittest

from execution.order_command import build_order_command, build_rl_order_command


class OrderCommandTest(unittest.TestCase):
    def setUp(self):
        self._old_order_token = os.environ.get("SHARK_ORDER_TOKEN")
        os.environ["SHARK_ORDER_TOKEN"] = "secret-token"

    def tearDown(self):
        if self._old_order_token is None:
            os.environ.pop("SHARK_ORDER_TOKEN", None)
        else:
            os.environ["SHARK_ORDER_TOKEN"] = self._old_order_token

    def test_build_open_order_command_includes_required_live_fields_and_token(self):
        raw = build_order_command(
            symbol="BTC/USDT",
            side="long",
            action="open",
            mode="live",
            size=2,
            leverage=50,
            stop_loss=95.0,
            take_profit=[104.0, 108.0],
        )

        cmd = json.loads(raw)

        self.assertEqual(cmd["symbol"], "BTC/USDT")
        self.assertEqual(cmd["side"], "long")
        self.assertEqual(cmd["action"], "open")
        self.assertEqual(cmd["mode"], "live")
        self.assertEqual(cmd["size"], 2)
        self.assertEqual(cmd["leverage"], 50)
        self.assertEqual(cmd["stop_loss"], 95.0)
        self.assertEqual(cmd["take_profit"], 104.0)
        self.assertEqual(cmd["take_profit_levels"], [104.0, 108.0])
        self.assertEqual(cmd["source"], "strategy")
        self.assertEqual(cmd["token"], "secret-token")

    def test_build_order_command_rejects_incomplete_live_open(self):
        with self.assertRaises(ValueError):
            build_order_command(
                symbol="BTC/USDT",
                side="long",
                action="open",
                mode="live",
                size=0,
                leverage=50,
            )

    def test_build_rl_order_command_requires_explicit_size_and_leverage(self):
        with self.assertRaises(ValueError):
            build_rl_order_command({"symbol": "BTC/USDT", "side": "long"}, mode="paper")


if __name__ == "__main__":
    unittest.main()

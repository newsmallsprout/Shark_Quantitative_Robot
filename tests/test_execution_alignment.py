import unittest
from unittest.mock import MagicMock, patch

import main
from main import StrategyRunner


class ExecutionAlignmentTest(unittest.TestCase):
    def test_plan_position_size_is_the_margin_source(self):
        runner = StrategyRunner(initial_balance=1000.0)
        plan = {"position_size_pct": 0.02}
        cfg = {"margin_pct": 0.10}
        regime_cfg = {"margin_mult": 3.0}

        margin = runner._margin_from_plan(plan, cfg, regime_cfg, change_abs=12.0)

        self.assertEqual(margin, 20.0)

    def test_plan_margin_can_only_be_reduced_by_safety_cap(self):
        runner = StrategyRunner(initial_balance=1000.0)
        plan = {"position_size_pct": 0.05}
        cfg = {"max_plan_margin_pct": 0.02}

        margin = runner._margin_from_plan(plan, cfg, {}, change_abs=0.0)

        self.assertEqual(margin, 20.0)

    def test_plan_margin_uses_equity_not_shrinking_free_cash(self):
        runner = StrategyRunner(initial_balance=200.0)
        runner.balance = 60.0
        runner.static_equity = 200.0
        plan = {"position_size_pct": 0.015}
        cfg = {"min_plan_margin_pct": 0.03}

        margin = runner._margin_from_plan(plan, cfg, {}, change_abs=0.0)

        self.assertEqual(margin, 6.0)

    def test_websocket_snapshot_includes_paper_trading_status(self):
        old_paper = main._state.get("paper_trading")
        old_paper_obj = main._state.get("paper")
        try:
            main._state["paper_trading"] = True
            main._state.pop("paper", None)

            snapshot = main._state_for_websocket()

            self.assertEqual(snapshot["paper"], {"active": True, "trading_enabled": True})
        finally:
            main._state["paper_trading"] = old_paper
            if old_paper_obj is None:
                main._state.pop("paper", None)
            else:
                main._state["paper"] = old_paper_obj

    def test_plan_first_mode_does_not_wait_for_kline_warmup(self):
        runner = StrategyRunner(initial_balance=1000.0)

        self.assertTrue(runner._warmup_allows_open(has_kline=False, has_detector=False))

    def test_paper_mode_autostarts_by_default_after_restart(self):
        with patch.dict("os.environ", {"SHARK_MODE": "paper"}, clear=False):
            self.assertTrue(main._default_paper_trading_enabled())

    def test_live_mode_never_autostarts_trading(self):
        with patch.dict("os.environ", {"SHARK_MODE": "live"}, clear=False):
            self.assertFalse(main._default_paper_trading_enabled())

    def test_repeated_stop_losses_do_not_block_next_open(self):
        runner = StrategyRunner(initial_balance=200.0)

        with patch.object(runner, "_request_symbol_replan") as replan:
            for _ in range(main._FUSE_SL_STREAK_LIMIT):
                runner._apply_stop_loss_fuse("BTC/USDT", "止损")

        self.assertEqual(runner._fuse_sl_streak.get("BTC/USDT"), 0)
        replan.assert_called_once()

    def test_repeated_stop_losses_request_symbol_replan_on_third_loss(self):
        runner = StrategyRunner(initial_balance=200.0)
        fake_redis = MagicMock()

        with patch("redis.from_url", return_value=fake_redis):
            for _ in range(main._FUSE_SL_STREAK_LIMIT):
                runner._apply_stop_loss_fuse("BTC/USDT", "止损")

        fake_redis.delete.assert_called_with("shark:plan:BTC/USDT")
        self.assertTrue(fake_redis.publish.called)
        channel, payload = fake_redis.publish.call_args.args
        self.assertEqual(channel, "shark:plan:replan")
        self.assertIn("BTC/USDT", payload)

    def test_both_plan_slow_grind_up_prefers_long_not_midpoint_short(self):
        runner = StrategyRunner(initial_balance=200.0)
        plan = {
            "bias": "both",
            "macro_regime": "slow_grind_up",
            "range_low": 80000,
            "range_high": 82200,
            "long_stop_loss": 79500,
            "long_take_profit": [81500],
            "short_stop_loss": 82800,
            "short_take_profit": [80500],
        }

        side, signal_src, stop_loss, take_profit = runner._side_from_plan(plan, 81800)

        self.assertEqual(side, "long")
        self.assertIn("顺势", signal_src)
        self.assertEqual(stop_loss, 79500)
        self.assertEqual(take_profit, [81500])

    def test_both_plan_breakout_down_prefers_short_not_midpoint_long(self):
        runner = StrategyRunner(initial_balance=200.0)
        plan = {
            "bias": "both",
            "macro_regime": "breakout_down",
            "range_low": 90,
            "range_high": 110,
            "long_stop_loss": 88,
            "long_take_profit": [103],
            "short_stop_loss": 112,
            "short_take_profit": [94],
        }

        side, signal_src, stop_loss, take_profit = runner._side_from_plan(plan, 92)

        self.assertEqual(side, "short")
        self.assertIn("顺势", signal_src)
        self.assertEqual(stop_loss, 112)
        self.assertEqual(take_profit, [94])

    def test_switching_to_live_discards_paper_positions_and_history(self):
        runner = StrategyRunner(initial_balance=200.0)
        runner.positions = {"BTC/USDT": {"margin": 6.0}}
        runner._trade_history = [{"symbol": "BTC/USDT"}]
        main._state["position_list"] = [{"symbol": "BTC/USDT"}]
        main._state["trade_history"] = [{"symbol": "BTC/USDT"}]
        main._state["positions"] = 1

        fake_engine = MagicMock()
        fake_engine.get_balance.return_value = 123.0

        with patch("main.create_live_engine", return_value=fake_engine), patch("redis.from_url") as redis_from_url:
            result = runner.switch_mode("live")

        self.assertNotIn("error", result)
        self.assertEqual(runner.positions, {})
        self.assertEqual(runner._trade_history, [])
        self.assertEqual(main._state["position_list"], [])
        self.assertEqual(main._state["trade_history"], [])
        self.assertEqual(main._state["positions"], 0)
        redis_from_url.return_value.delete.assert_called_with("shark:trade_history")


if __name__ == "__main__":
    unittest.main()

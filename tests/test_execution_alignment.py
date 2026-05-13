import unittest

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


if __name__ == "__main__":
    unittest.main()

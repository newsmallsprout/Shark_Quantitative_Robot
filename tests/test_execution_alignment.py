import unittest

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


if __name__ == "__main__":
    unittest.main()

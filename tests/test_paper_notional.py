"""纸面名义价值 / 合约乘数：手续费与 PnL 按 Notional=Qty×CS×Price。"""
import unittest
from unittest.mock import MagicMock, patch

from src.core.paper_engine import PaperTradingEngine


class TestPaperNotional(unittest.TestCase):
    def test_fee_scales_with_quanto(self):
        pe = PaperTradingEngine(initial_balance=100_000.0)
        pe.taker_fee = 0.0005
        sym = "FAKE/USDT"
        pe.latest_prices[sym] = 60_000.0
        cs = 0.0001
        contracts = 1.0
        entry = 60_000.0
        notional = contracts * cs * entry
        self.assertAlmostEqual(notional, 6.0, places=8)
        fee = notional * pe.taker_fee
        self.assertAlmostEqual(fee, 0.003, places=8)

    def test_unrealized_pnl_uses_base_not_leverage(self):
        pe = PaperTradingEngine(initial_balance=10_000.0)
        sym = "X/USDT"
        pe.latest_prices[sym] = 100.0
        pe.positions[sym] = {
            "side": "long",
            "size": 10.0,
            "entry_price": 99.0,
            "contract_size": 0.5,
            "leverage": 100,
            "margin_mode": "cross",
            "unrealized_pnl": 0.0,
            "entry_context": {},
            "accumulated_fees": 0.0,
        }
        pe._calculate_pnl()
        pos = pe.positions[sym]
        want = 10.0 * 0.5 * (100.0 - 99.0)
        self.assertAlmostEqual(pos["unrealized_pnl"], want, places=8)
        self.assertAlmostEqual(want, 5.0, places=8)

    def test_contracts_for_target_usdt_notional(self):
        pe = PaperTradingEngine(initial_balance=10_000.0)
        sym = "MEME/USDT"
        # 无网关时 default_contract_size=1 → 与旧式 qty=notional/price 一致
        q = pe.contracts_for_target_usdt_notional(sym, 100.0, 1000.0)
        self.assertAlmostEqual(q, 10.0, places=8)

        mock_ex = MagicMock()
        mock_ex.contract_specs_cache = {sym: {"quanto_multiplier": 100.0}}
        mock_ctx = MagicMock()
        mock_ctx.get_exchange.return_value = mock_ex
        with patch("src.core.globals.bot_context", mock_ctx):
            q2 = pe.contracts_for_target_usdt_notional(sym, 1.0, 1000.0)
            self.assertAlmostEqual(q2, 10.0, places=8)

    def test_estimate_net_with_contract_size(self):
        pe = PaperTradingEngine(initial_balance=10_000.0)
        pe.taker_fee = 0.0005
        pos = {
            "side": "long",
            "entry_price": 60_000.0,
            "size": 1.0,
            "contract_size": 1.0,
            "accumulated_fees": 60_000.0 * 1.0 * 0.0005,
        }
        exit_px = 60_030.0
        gross = 1.0 * 1.0 * 30.0
        fee_c = 60_030.0 * 1.0 * 0.0005
        est = pe.estimate_flat_net_pnl("X/USDT", pos, exit_px)
        self.assertAlmostEqual(est, gross - pos["accumulated_fees"] - fee_c, places=6)


if __name__ == "__main__":
    unittest.main()

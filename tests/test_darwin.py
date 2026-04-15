import asyncio
import os
import tempfile

from src.core.config_manager import ConfigManager, GlobalConfig, DarwinConfig, RiskConfig
from src.darwin.autopsy import build_trade_autopsy
from src.darwin.researcher import DarwinResearcher


def test_build_trade_autopsy_schema():
    snap = build_trade_autopsy(
        symbol="PEPE/USDT",
        side="long",
        entry_price=1.0,
        exit_price=1.1,
        closed_size=100.0,
        leverage=10.0,
        margin_mode="isolated",
        realized_pnl_gross=10.0,
        fees_on_trade=0.5,
        entry_context={"ai_score": 72.0, "obi": 0.12},
        max_favorable_unrealized=12.0,
        max_adverse_unrealized=-3.0,
        opened_at=1.0,
        exit_reason="opposite_fill",
        trading_mode_at_exit="ATTACK",
    )
    assert snap["schema"] == "darwin.trade_autopsy.v2"
    assert snap["pnl"]["realized_net"] == 9.5
    assert snap["exit"]["reason"] == "opposite_fill"


def test_build_trade_autopsy_l1_micro_extracted():
    snap = build_trade_autopsy(
        symbol="SOL/USDT",
        side="long",
        entry_price=1.0,
        exit_price=0.95,
        closed_size=10.0,
        leverage=10.0,
        margin_mode="cross",
        realized_pnl_gross=-0.5,
        fees_on_trade=0.1,
        entry_context={
            "l1_managed": True,
            "l1_signal_micro": {"cvd_10s": 120.0, "obi_top5": 0.1},
        },
        max_favorable_unrealized=1.0,
        max_adverse_unrealized=-2.0,
        opened_at=1.0,
        exit_reason="l1_cvd_stop",
        trading_mode_at_exit="ATTACK",
    )
    assert snap["l1_at_signal"]["cvd_10s"] == 120.0
    assert "l1_signal_micro" not in snap["entry_snapshot"]


def test_apply_darwin_llm_result_symbol_and_risk():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "settings.yaml")
        cm = ConfigManager.__new__(ConfigManager)
        cm._config_path = path
        cm.config = GlobalConfig(
            risk=RiskConfig(berserker_obi_threshold=0.85),
            darwin=DarwinConfig(symbol_patches={}),
        )
        ok = cm.apply_darwin_llm_result(
            {
                "patches": {
                    "risk": {"berserker_obi_threshold": 0.88},
                    "symbols": {"PEPE_USDT": {"berserker_obi_threshold": 0.91, "leverage_cap": 40}},
                }
            }
        )
        assert ok is True
        assert cm.config.risk.berserker_obi_threshold == 0.88
        p = cm.config.darwin.symbol_patches.get("PEPE/USDT")
        assert p is not None
        assert p.berserker_obi_threshold == 0.91
        assert p.max_leverage == 40


def test_darwin_researcher_mock_does_not_crash():
    async def _run():
        r = DarwinResearcher()
        await r.run({"schema": "darwin.trade_autopsy.v2", "symbol": "BTC/USDT", "closed_at": 1.0})

    asyncio.run(_run())


def test_apply_darwin_l1_fast_loop_patch():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "settings.yaml")
        cm = ConfigManager.__new__(ConfigManager)
        cm._config_path = path
        cm.config = GlobalConfig()
        base = cm.config.l1_fast_loop.cvd_burst_mult
        ok = cm.apply_darwin_llm_result(
            {
                "patches": {
                    "l1_fast_loop": {"cvd_burst_mult": base + 0.5, "enabled": True},
                }
            }
        )
        assert ok is True
        assert cm.config.l1_fast_loop.cvd_burst_mult == base + 0.5
        assert cm.config.l1_fast_loop.enabled is False


def test_darwin_batch_summarize():
    from src.darwin.evolution import summarize_batch

    batch = [
        {
            "symbol": "A/USDT",
            "pnl": {"realized_net": -1.0},
            "exit": {"reason": "stop"},
            "l1_at_signal": {"x": 1},
        },
        {
            "symbol": "A/USDT",
            "pnl": {"realized_net": 2.0},
            "exit": {"reason": "tp"},
        },
    ]
    s = summarize_batch(batch)
    assert s["n"] == 2
    assert s["l1_trades_in_batch"] == 1

from collections import Counter
from types import SimpleNamespace

from src.ai import l2_command


def test_rank_universe_respects_anchors_and_cap():
    rows = [
        {
            "contract": "AAA_USDT",
            "volume_24h_quote": 9e6,
            "change_percentage": "1",
            "funding_rate": "0.0001",
        },
        {
            "contract": "BTC_USDT",
            "volume_24h_quote": 5e6,
            "change_percentage": "2",
            "funding_rate": "0.0001",
        },
        {
            "contract": "ZZZ_USDT",
            "volume_24h_quote": 8e6,
            "change_percentage": "10",
            "funding_rate": "0.0001",
        },
    ]
    syms, stats = l2_command.rank_universe_symbols(
        rows,
        min_quote_vol=1e6,
        top_n=10,
        cap=3,
        anchors=["BTC/USDT"],
    )
    assert syms[0] == "BTC/USDT"
    assert len(syms) <= 3
    assert stats["candidates"] == 3


def test_rank_universe_hl_vol_boost_changes_order():
    """Same volume: wider 24h range → higher score when hl_vol_weight > 0."""
    base = {
        "contract": "AAA_USDT",
        "volume_24h_quote": 5e6,
        "change_percentage": "1",
        "funding_rate": "0",
        "mark_price": "100",
        "high_24h": "101",
        "low_24h": "99",
    }
    wide = {
        **base,
        "contract": "BBB_USDT",
        "high_24h": "110",
        "low_24h": "90",
    }
    rows = [base, wide]
    syms_flat, _ = l2_command.rank_universe_symbols(
        rows,
        min_quote_vol=1e6,
        top_n=10,
        cap=4,
        anchors=[],
        hl_vol_weight=0.0,
    )
    syms_boost, stats = l2_command.rank_universe_symbols(
        rows,
        min_quote_vol=1e6,
        top_n=10,
        cap=4,
        anchors=[],
        hl_vol_weight=2.0,
        hl_vol_bps_divisor=100.0,
        hl_vol_score_cap=10.0,
    )
    assert syms_flat[0] == "AAA/USDT"
    assert syms_boost[0] == "BBB/USDT"
    assert stats["top_preview"][0].get("hl_bps", 0) > 0


def test_rules_l1_tuning_chaotic():
    l1 = SimpleNamespace(cvd_burst_mult=2.0, cvd_stop_mult=2.0, min_atr_bps=10.0)
    c = Counter({"CHAOTIC": 8, "OSCILLATING": 2})
    out = l2_command.rules_l1_tuning(c, [50.0] * 10, l1)
    assert out["cvd_burst_mult"] > l1.cvd_burst_mult
    assert out["position_scale"] < 1.0


def test_merge_l1_tuning_llm_overrides():
    rules = {"halt_trading": False, "position_scale": 0.5, "cvd_burst_mult": 2.5}
    llm = {"halt_trading": True, "cvd_burst_mult": None, "position_scale": 0.3}
    m = l2_command.merge_l1_tuning(rules, llm)
    assert m["halt_trading"] is True
    assert m["position_scale"] == 0.3
    assert m["cvd_burst_mult"] == 2.5

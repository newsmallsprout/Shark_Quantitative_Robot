import pytest

from src.ai.regime import MarketRegime
from src.core.config_manager import PredatorMatrixWeights
from src.strategy.predator_matrix import (
    combine_tech_scores,
    funding_obi_divergence_points,
    liquidity_sweep_long,
    volume_profile_poc_vah_val,
    weights_for_regime,
)


def test_volume_profile_basic():
    candles = []
    p = 100.0
    for i in range(30):
        candles.append(
            {
                "time": i,
                "open": p,
                "high": p + 1,
                "low": p - 1,
                "close": p,
                "volume": 10.0 + i,
            }
        )
        p += 0.2
    out = volume_profile_poc_vah_val(candles, bins=16, value_area_pct=0.7)
    assert out is not None
    poc, vah, val, bw = out
    assert val < poc < vah
    assert bw > 0


def test_liquidity_sweep_requires_reclaim_and_obi():
    candles = []
    for i in range(20):
        candles.append(
            {"time": i, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 5}
        )
    candles.append({"time": 20, "open": 100, "high": 100.5, "low": 94, "close": 97, "volume": 5})
    candles.append({"time": 21, "open": 97, "high": 99, "low": 96, "close": 98.5, "volume": 5})
    assert not liquidity_sweep_long(candles, obi=-0.9, lookback=8, obi_floor=-0.35)
    candles[-1] = {"time": 21, "open": 97, "high": 100, "low": 93.0, "close": 99.5, "volume": 5}
    assert liquidity_sweep_long(candles, obi=-0.2, lookback=8, obi_floor=-0.35)


def test_funding_divergence():
    b, e = funding_obi_divergence_points(
        -0.0005, 0.2, -0.0003, 0.0003, 0.12, 15.0
    )
    assert b == pytest.approx(15.0)
    assert e == 0.0


def test_weights_chaotic():
    rw = {"CHAOTIC": PredatorMatrixWeights(ai=0.15, tech=0.15, obi=0.7)}
    a, t, o = weights_for_regime(MarketRegime.CHAOTIC, rw)
    assert a == pytest.approx(0.15)
    assert o == pytest.approx(0.7)


def test_combine_tech():
    b, r = combine_tech_scores(60.0, 40.0, 70.0, 30.0, 0.5)
    assert b == pytest.approx(65.0)
    assert r == pytest.approx(35.0)

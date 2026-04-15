import pytest

from src.utils.atr import compute_atr_from_candles, true_range


def test_true_range_with_prev_close():
    assert true_range(110.0, 100.0, 105.0) == pytest.approx(10.0)


def test_atr_constant_range():
    candles = []
    c = 100.0
    for i in range(20):
        candles.append({"time": i, "open": c, "high": c + 2.0, "low": c - 1.0, "close": c})
        c += 0.1
    atr = compute_atr_from_candles(candles, period=14)
    assert atr > 0


def test_atr_insufficient_data():
    assert compute_atr_from_candles([], period=14) == 0.0
    assert compute_atr_from_candles([{"time": 1, "high": 1, "low": 1, "close": 1}], period=14) == 0.0

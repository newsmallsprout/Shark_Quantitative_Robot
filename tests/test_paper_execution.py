"""纸面撮合：VWAP / 无盘口 fallback / Maker 触价"""
from src.core.paper_engine import PaperTradingEngine, _paper_engine_settings


def test_market_buy_vwap_is_best_ask_when_depth_enough():
    eng = PaperTradingEngine(initial_balance=100_000.0)
    eng.update_orderbook(
        "SOL/USDT",
        bids=[[100.0, 1000.0]],
        asks=[[100.2, 1000.0], [100.4, 1000.0]],
    )
    eng.latest_prices["SOL/USDT"] = 100.1
    r = eng.execute_order("SOL/USDT", "buy", 1.0, price=None, leverage=10)
    assert r.get("status") in ("open", "closed")
    assert abs(float(r.get("price", 0)) - 100.2) < 1e-9


def test_fallback_settings_readable():
    fb, ex = _paper_engine_settings()
    assert fb >= 0
    assert ex >= 0


def test_maker_buy_fills_when_best_ask_crosses_limit_without_last_move():
    eng = PaperTradingEngine(initial_balance=100_000.0)
    eng.update_price("BTC/USDT", 102_000.0)
    r = eng.execute_order(
        "BTC/USDT",
        "buy",
        0.01,
        100_000.0,
        post_only=True,
        leverage=10,
        entry_context={"resting_quote": True},
    )
    assert r.get("status") == "resting"
    eng.update_orderbook(
        "BTC/USDT",
        bids=[[99_900.0, 1.0]],
        asks=[[99_950.0, 1.0]],
    )
    pos = eng.positions.get("BTC/USDT")
    assert pos and pos["size"] > 0
    assert pos["side"] == "long"

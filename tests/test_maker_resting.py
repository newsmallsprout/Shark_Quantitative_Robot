from src.core.paper_engine import PaperTradingEngine


def test_resting_maker_buy_fills_on_price_touch():
    eng = PaperTradingEngine(initial_balance=100_000.0)
    eng.update_price("BTC/USDT", 100_000.0)
    r = eng.execute_order(
        "BTC/USDT",
        "buy",
        0.01,
        99_000.0,
        post_only=True,
        leverage=10,
        entry_context={"resting_quote": True, "scheme": "test"},
    )
    assert r.get("status") == "resting"
    assert eng.positions.get("BTC/USDT") is None or eng.positions["BTC/USDT"]["size"] == 0

    eng.update_price("BTC/USDT", 98_500.0)
    pos = eng.positions.get("BTC/USDT")
    assert pos and pos["size"] > 0
    assert pos["side"] == "long"


def test_cancel_open_makers():
    eng = PaperTradingEngine(initial_balance=100_000.0)
    eng.execute_order(
        "ETH/USDT",
        "sell",
        1.0,
        3500.0,
        post_only=True,
        entry_context={"resting_quote": True},
    )
    assert len(eng._maker_resting.get("ETH/USDT", [])) == 1
    n = eng.cancel_open_makers("ETH/USDT")
    assert n == 1
    assert eng._maker_resting.get("ETH/USDT", []) == []

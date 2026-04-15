"""纸面预检：合约张数 / 杠杆 / 风控阶梯 / 保证金（须 enforce_exchange_physics + 缓存）。"""
from src.core.config_manager import config_manager
from src.core.globals import bot_context
from src.core.paper_engine import PaperTradingEngine


class _MockGateway:
    def __init__(self):
        self.contract_specs_cache = {
            "ZZ/USDT": {
                "quanto_multiplier": 1.0,
                "order_size_min": 1.0,
                "order_size_max": 1_000_000.0,
                "market_order_size_max": 1_000_000.0,
                "leverage_min": 1.0,
                "leverage_max": 125.0,
                "enable_decimal": False,
            }
        }
        self.risk_limit_tiers_cache = {
            "ZZ/USDT": [
                {"tier": 1, "risk_limit": "100000", "leverage_max": "100"},
                {"tier": 2, "risk_limit": "500000", "leverage_max": "50"},
            ]
        }


def _restore_exchange(prev):
    bot_context.exchange = prev


def test_preflight_unknown_symbol_when_enforced():
    prev = bot_context.exchange
    bot_context.exchange = _MockGateway()
    prev_enf = config_manager.config.paper_engine.enforce_exchange_physics
    config_manager.config.paper_engine.enforce_exchange_physics = True
    try:
        eng = PaperTradingEngine(initial_balance=1_000_000.0)
        eng.latest_prices["NOPE/USDT"] = 100.0
        eng.orderbooks_cache["NOPE/USDT"] = {
            "bids": [[99.0, 1_000.0]],
            "asks": [[101.0, 1_000.0]],
        }
        r = eng.execute_order("NOPE/USDT", "buy", 1.0, None, leverage=10)
        assert r.get("status") == "rejected"
        assert r.get("label") == "CONTRACT_NOT_FOUND"
    finally:
        config_manager.config.paper_engine.enforce_exchange_physics = prev_enf
        _restore_exchange(prev)


def test_preflight_order_below_min_contracts():
    prev = bot_context.exchange
    bot_context.exchange = _MockGateway()
    prev_enf = config_manager.config.paper_engine.enforce_exchange_physics
    config_manager.config.paper_engine.enforce_exchange_physics = True
    try:
        eng = PaperTradingEngine(initial_balance=1_000_000.0)
        eng.latest_prices["ZZ/USDT"] = 1.0
        eng.orderbooks_cache["ZZ/USDT"] = {
            "bids": [[0.99, 1_000_000.0]],
            "asks": [[1.01, 1_000_000.0]],
        }
        r2 = eng.execute_order("ZZ/USDT", "buy", 0.5, None, leverage=10)
        assert r2.get("status") == "rejected"
        assert r2.get("label") == "ORDER_SIZE_TOO_SMALL"
    finally:
        config_manager.config.paper_engine.enforce_exchange_physics = prev_enf
        _restore_exchange(prev)


def test_preflight_leverage_exceeds_risk_tier():
    prev = bot_context.exchange
    bot_context.exchange = _MockGateway()
    prev_enf = config_manager.config.paper_engine.enforce_exchange_physics
    config_manager.config.paper_engine.enforce_exchange_physics = True
    try:
        eng = PaperTradingEngine(initial_balance=1e12)
        eng.latest_prices["ZZ/USDT"] = 1.0
        eng.orderbooks_cache["ZZ/USDT"] = {
            "bids": [[0.99, 1e9]],
            "asks": [[1.01, 1e9]],
        }
        r = eng.execute_order("ZZ/USDT", "buy", 200_000.0, None, leverage=100)
        assert r.get("status") == "rejected"
        assert r.get("label") == "RISK_LIMIT_TIER"
    finally:
        config_manager.config.paper_engine.enforce_exchange_physics = prev_enf
        _restore_exchange(prev)


def test_preflight_insufficient_margin():
    prev = bot_context.exchange
    bot_context.exchange = _MockGateway()
    prev_enf = config_manager.config.paper_engine.enforce_exchange_physics
    config_manager.config.paper_engine.enforce_exchange_physics = True
    try:
        eng = PaperTradingEngine(initial_balance=100.0)
        eng.latest_prices["ZZ/USDT"] = 1.0
        eng.orderbooks_cache["ZZ/USDT"] = {
            "bids": [[0.99, 1e9]],
            "asks": [[1.01, 1e9]],
        }
        r = eng.execute_order("ZZ/USDT", "buy", 10_000.0, None, leverage=10)
        assert r.get("status") == "rejected"
        assert r.get("label") == "INSUFFICIENT_MARGIN"
    finally:
        config_manager.config.paper_engine.enforce_exchange_physics = prev_enf
        _restore_exchange(prev)

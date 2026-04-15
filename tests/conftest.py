"""单测默认关闭「开仓必须带止盈止损限价」，避免大量纸面用例被拒。"""
import pytest


@pytest.fixture(autouse=True)
def _paper_optional_entry_tp_sl():
    from src.core.config_manager import config_manager

    pe = config_manager.config.paper_engine
    prev = bool(getattr(pe, "require_entry_tp_sl_limits", False))
    pe.require_entry_tp_sl_limits = False
    yield
    pe.require_entry_tp_sl_limits = prev

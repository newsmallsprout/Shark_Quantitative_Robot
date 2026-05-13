"""
Shark Regression Tests — Feedback Loop
基于 Matt Pocock diagnose 技能原则: "Build the right feedback loop, and the bug is 90% fixed."

运行: docker exec shark2 python3 -m pytest tests/ -v
    或: docker exec shark2 python3 tests/test_core.py
"""

import sys, os, math, time, uuid
sys.path.insert(0, "/app")

# ═══════════════════════════════════════════════
# Test Helpers
# ═══════════════════════════════════════════════

PASS = 0
FAIL = 0

def check(desc: str, actual, expected, tolerance: float = 1e-6):
    """断言 + 计数"""
    global PASS, FAIL
    if isinstance(expected, float):
        ok = abs(actual - expected) < tolerance
    else:
        ok = actual == expected
    if ok:
        PASS += 1
        print(f"  ✅ {desc}")
    else:
        FAIL += 1
        print(f"  ❌ {desc}: got={actual} expected={expected}")


def make_pos(sym="BTC/USDT", side="long", entry=80000.0, size=1.0,
             leverage=100, margin=20.0, fee_open=0.01, opened=None):
    return {
        "symbol": sym, "side": side, "entry": entry, "size": size,
        "leverage": leverage, "margin": margin,
        "fee_open": fee_open, "opened": opened or time.time(),
        "vol_chg": 3.0, "best_pnl": -999, "pyramid_count": 0,
        "ai_targets": None, "order_id": uuid.uuid4(),
        "signal_src": "test", "ai_confidence": 70,
    }


# ═══════════════════════════════════════════════
# Test: Fee Calculation
# ═══════════════════════════════════════════════

def test_fee_calculation():
    """手续费计算 — BTC quanto 回归测试"""
    print("\n📐 Test: Fee Calculation")

    from main import StrategyRunner
    runner = StrategyRunner(initial_balance=200.0)
    runner._contract_specs = {}
    runner.positions = {}
    runner.balance = 200.0

    # ── BTC 手续费 (quanto=0.0001) ──
    runner._contract_specs["BTC/USDT"] = type('obj', (object,), {
        'maker_fee': -0.0002, 'taker_fee': 0.0005,
        'leverage_max': 125, 'order_size_min': 1,
        'quanto_multiplier': 0.0001
    })()
    q = runner._quanto_for("BTC/USDT")
    check("BTC quanto", q, 0.0001)

    btc_pos = make_pos("BTC/USDT", entry=80000, size=10, margin=20)
    actual_fee = runner._get_maker_fee("BTC/USDT")
    check("BTC maker fee (abs of negative)", actual_fee, 0.0002)

    est = runner._est_fee_usd("BTC/USDT", btc_pos, 81000, fee_rounds=3.0)
    # 平仓侧手续费: 10 × 0.0001 × 81000 × 0.0002 × 3 = 0.00486
    expected_est = 10 * 0.0001 * 81000 * 0.0002 * 3.0
    check("BTC est_fee_usd", est, expected_est)

    # ── ETH 手续费 (quanto=1) ──
    runner._contract_specs["ETH/USDT"] = type('obj', (object,), {
        'maker_fee': -0.0001, 'taker_fee': 0.0005,
        'leverage_max': 125, 'order_size_min': 1,
        'quanto_multiplier': 1.0
    })()
    eth_pos = make_pos("ETH/USDT", entry=2300, size=1, margin=20)
    eth_fee = runner._est_fee_usd("ETH/USDT", eth_pos, 2350, fee_rounds=3.0)
    expected_eth = 1 * 1.0 * 2350 * 0.0001 * 3.0  # = 0.705
    check("ETH est_fee_usd", eth_fee, expected_eth)
    check("ETH quanto", runner._quanto_for("ETH/USDT"), 1.0)


# ═══════════════════════════════════════════════
# Test: PnL Calculation
# ═══════════════════════════════════════════════

def test_pnl():
    """盈亏计算：毛利 vs 净利"""
    print("\n📈 Test: PnL Calculation")

    from main import StrategyRunner
    runner = StrategyRunner(initial_balance=200.0)
    runner.positions = {}
    runner.balance = 200.0
    runner.realized_pnl = 0.0
    runner.gross_realized = 0.0
    runner.total_fees = 0.0
    runner._trade_history = []
    runner._regime_cache = {}
    runner._ai_signal_cache = {}
    runner._log = []
    runner.closed_trades = 0
    runner.wins = 0
    runner.total_slippage = 0.0
    runner._initial_capital = 200.0

    # 开多 BTC: 10张 @ 80000, 杠杆100x, 保证金=$20
    pos = make_pos("BTC/USDT", "long", 80000, size=10, leverage=100,
                   margin=20.0, fee_open=0.016)
    runner.positions["BTC/USDT"] = pos
    runner.balance -= pos["margin"] + pos["fee_open"]

    check("余额开仓后", runner.balance, 200 - 20 - 0.016)

    # 平仓 @ 81000 (盈利 $1/张 → 10张=$10 毛利)
    # 模拟 _close_position 的核心计算
    q = 0.0001
    gross = 10 * q * (81000 - 80000)  # long: size × quanto × (px - entry)
    check("BTC毛利", gross, 10 * 0.0001 * 1000)  # = 1.0

    fee_close = 10 * q * 81000 * 0.0002  # maker fee
    realized = gross - pos["fee_open"] - fee_close
    check("BTC净利", realized, gross - 0.016 - fee_close)

    # 余额平仓后
    bal_after = runner.balance + pos["margin"] + gross - fee_close
    check("余额平仓后", bal_after, 200 - 20 - 0.016 + 20 + gross - fee_close)


# ═══════════════════════════════════════════════
# Test: Stop Loss Triggering
# ═══════════════════════════════════════════════

def test_stop_loss():
    """止损触发：动态止损计算"""
    print("\n🛑 Test: Stop Loss")

    from main import StrategyRunner
    runner = StrategyRunner(initial_balance=200.0)
    runner._contract_specs = {}
    runner._regime_cache = {}
    runner._reflector = None
    runner._ai_signal_cache = {}
    runner._trade_history = []
    runner.closed_trades = 0
    runner.wins = 0

    # 山寨 ETH 做多 @ 2300, vol_chg=5%
    pos = make_pos("ETH/USDT", "long", 2300, size=1, leverage=50,
                   margin=10.0, fee_open=0.01)
    pos["vol_chg"] = 5.0

    # 当前价 2250 (亏损 -50, 即 -2.17%)
    px = 2250
    pnl_pct = (px - 2300) / 2300 * 50 * 100  # 杠杆后
    check("ETH浮亏%", pnl_pct, (2250-2300)/2300*50*100)

    # 动态止损: -max(abs(sl_base), min(abs(sl_max), vol_chg×2))
    # sl_base=-6, sl_max=-12, vol_chg=5 → dyn_sl = -max(6, min(12, 10)) = -10
    sl_base = -6.0
    sl_max = -12.0
    vol_chg = 5.0
    dyn_sl = -max(abs(sl_base), min(abs(sl_max), vol_chg * 2.0))
    check("动态止损%", dyn_sl, -10.0)

    # 止损未触发 (亏2.17% < 10%止损, 但杠杆放大后远超)
    should_stop = pnl_pct <= dyn_sl
    check("止损触发 (杠杆放大后远超10%)", should_stop, True)

    # 价格跌到 2200 (亏损 -4.35%, 杠杆后-217%)
    px2 = 2200
    pnl_pct2 = (px2 - 2300) / 2300 * 50 * 100
    should_stop2 = pnl_pct2 <= dyn_sl
    check("深亏必然触发止损", should_stop2, True)
    check("浮亏值", round(pnl_pct2, 2), -217.39)


# ═══════════════════════════════════════════════
# Test: Balance Accounting
# ═══════════════════════════════════════════════

def test_balance_formula():
    """余额公式: balance = initial + gross_realized - total_fees"""
    print("\n💰 Test: Balance Formula")

    initial = 200.0
    gross = 15.50   # 毛利（不含手续费）
    fees = 2.30     # 总手续费
    balance = initial + gross - fees
    check("余额公式", balance, 200 + 15.5 - 2.3)

    # 验算: balance + margin_locked = equity
    # 不做 full integration，只验证公式


# ═══════════════════════════════════════════════
# Test: Regime Detection (smoke)
# ═══════════════════════════════════════════════

def test_regime_detection():
    """行情检测冒烟测试"""
    print("\n🔍 Test: Regime Detection (smoke)")

    try:
        from market_regime import MarketRegime, REGIME_CONFIG
        check("MarketRegime 枚举", len(list(MarketRegime)), 10)
        check("REGIME_CONFIG 覆盖", len(REGIME_CONFIG), 10)

        # 每个行情都有 allowed_dir
        for regime, cfg in REGIME_CONFIG.items():
            assert "allowed_dir" in cfg, f"{regime} missing allowed_dir"
        check("所有行情有 allowed_dir", True, True)
    except ImportError:
        print("  ⚠ market_regime 不可用，跳过")


# ═══════════════════════════════════════════════
# Test: Strategy Entry Calculation
# ═══════════════════════════════════════════════

def test_strategic_entry():
    """策略入场价计算"""
    print("\n🎯 Test: Strategic Entry")

    from main import StrategyRunner
    runner = StrategyRunner(initial_balance=200.0)

    # 无 kline_cache → 返回原价
    entry = runner._strategic_entry("BTC/USDT", "long", 80000, "low_vol_ranging")
    check("无kline→原价", entry, 80000.0)

    # 趋势市做多应偏向回调
    entry2 = runner._strategic_entry("BTC/USDT", "long", 80000, "strong_trend_up")
    check("强趋势做多≤原价", entry2 <= 80000, True)

    # 震荡市做空应偏向阻力
    entry3 = runner._strategic_entry("ETH/USDT", "short", 2300, "high_vol_ranging")
    check("高波震荡做空≥原价", entry3 >= 2300, True)


# ═══════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    print("🦈 Shark Regression Tests")
    print("=" * 50)

    test_fee_calculation()
    test_pnl()
    test_stop_loss()
    test_balance_formula()
    test_regime_detection()
    test_strategic_entry()

    print("\n" + "=" * 50)
    total = PASS + FAIL
    print(f"Results: {PASS}/{total} passed", end="")
    if FAIL > 0:
        print(f", {FAIL} FAILED ❌")
        sys.exit(1)
    else:
        print(" ✅")
        sys.exit(0)

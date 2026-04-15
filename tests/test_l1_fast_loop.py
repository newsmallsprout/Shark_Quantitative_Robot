import time

from src.core import l1_fast_loop


def test_parse_gate_trade_buy_sell():
    ts, q = l1_fast_loop.parse_gate_trade(
        {"create_time": 1700000000.0, "size": 2.0, "side": "buy"}
    )
    assert q > 0
    ts2, q2 = l1_fast_loop.parse_gate_trade(
        {"create_time": 1700000000.0, "size": 2.0, "side": "sell"}
    )
    assert q2 < 0


def test_cvd_rolling_window():
    sym = "TEST/USDT"
    now = time.time()
    l1_fast_loop.ingest_trades(
        sym,
        [
            {"create_time": now - 5, "size": 10.0, "side": "buy", "contract": "TEST_USDT"},
            {"create_time": now - 5, "size": 4.0, "side": "sell", "contract": "TEST_USDT"},
        ],
    )
    c10, c60, base = l1_fast_loop.cvd_metrics(sym, now)
    assert c10 == 6.0
    assert c60 == 6.0
    assert base > 0


def test_atr_1m_bps_requires_bars():
    sym = "TEST2/USDT"
    t0 = 3600.0
    for i in range(4):
        l1_fast_loop.on_ticker_price(sym, t0 + i * 60, 100.0 + i * 0.5)
    bps = l1_fast_loop.atr_1m_bps(sym)
    assert bps >= 0.0


def test_apply_l1_tuning():
    l1_fast_loop.apply_l1_tuning({"cvd_burst_mult": 5.0, "halt_trading": False})
    assert l1_fast_loop._runtime.get("cvd_burst_mult") == 5.0
    l1_fast_loop._runtime["cvd_burst_mult"] = None


def test_footprint_snapshot_from_trades():
    sym = "FP/USDT"
    now = time.time()
    bucket = int(now // 60) * 60
    l1_fast_loop.ingest_trades(
        sym,
        [
            {
                "create_time": bucket + 5,
                "size": 2.0,
                "side": "buy",
                "price": "100.5",
                "contract": "FP_USDT",
            },
            {
                "create_time": bucket + 8,
                "size": 1.0,
                "side": "sell",
                "price": "100.5",
                "contract": "FP_USDT",
            },
        ],
    )
    snap = l1_fast_loop.footprint_snapshot(sym, max_bars=10, max_levels_per_bar=20)
    assert snap["schema"] == "shark.footprint.v1"
    assert snap["symbol"] == sym
    assert "cvd" in snap and "bars" in snap
    assert snap["cvd"]["window_sec_10"] == 10
    last_bar = snap["bars"][-1]
    assert last_bar["t"] == bucket
    assert any(abs(float(x["price"]) - 100.5) < 1e-6 for x in last_bar["levels"])


def test_footprint_ws_delta_maybe_throttle_same_bucket():
    sym = "WSDEL/USDT"
    l1_fast_loop._fp_ws_last_tail_t.clear()
    l1_fast_loop._fp_ws_last_push_mono.clear()
    now = time.time()
    bucket = int(now // 60) * 60
    l1_fast_loop.ingest_trades(
        sym,
        [
            {
                "create_time": bucket + 5,
                "size": 1.0,
                "side": "buy",
                "price": "10",
                "contract": "WSDEL_USDT",
            },
        ],
    )
    d0 = l1_fast_loop.footprint_ws_delta_maybe(sym)
    assert d0 is not None
    assert d0["schema"] == "shark.footprint.delta.v1"
    assert d0["tail"]["t"] == bucket
    assert "bar_closed" not in d0
    d1 = l1_fast_loop.footprint_ws_delta_maybe(sym)
    assert d1 is None


def test_footprint_ws_delta_maybe_bar_closed_on_bucket_roll():
    sym = "WSDEL2/USDT"
    l1_fast_loop._fp_ws_last_tail_t.clear()
    l1_fast_loop._fp_ws_last_push_mono.clear()
    b0 = 1_800_000
    b1 = b0 + 60
    l1_fast_loop.ingest_trades(
        sym,
        [
            {
                "create_time": b0 + 5,
                "size": 1.0,
                "side": "buy",
                "price": "20",
                "contract": "WSDEL2_USDT",
            },
        ],
    )
    first = l1_fast_loop.footprint_ws_delta_maybe(sym)
    assert first and first["tail"]["t"] == b0
    l1_fast_loop.ingest_trades(
        sym,
        [
            {
                "create_time": b1 + 5,
                "size": 2.0,
                "side": "sell",
                "price": "21",
                "contract": "WSDEL2_USDT",
            },
        ],
    )
    rolled = l1_fast_loop.footprint_ws_delta_maybe(sym)
    assert rolled and rolled.get("bar_closed")
    assert rolled["bar_closed"]["t"] == b0
    assert rolled["tail"]["t"] == b1

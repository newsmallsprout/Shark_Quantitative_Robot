#!/usr/bin/env python3
"""Shark 2.0 回测 v2 — 预计算滚动窗口，采样加速"""

import asyncio, time, sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aiohttp
from collections import defaultdict

from main import StrategyRunner, fetch_contract_specs, TAKER_FEE, SLIPPAGE_MAX

BACKTEST_DAYS = 3
CANDLE_INTERVAL = "1m"
TOP_SYMBOLS_N = 10
SAMPLE_INTERVAL = 15     # 每15分钟采样一次（模拟实盘tick频率）
LOOKBACK_24H = 1440      # 24h = 1440 根1m K线

async def fetch_candles(symbol: str, limit: int = 4320) -> list:
    gate_sym = symbol.replace("/USDT", "_USDT")
    url = "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
    all_rows = []
    per_page = 1000
    pages = (limit + per_page - 1) // per_page
    last_ts = None
    for page in range(pages):
        params = {"contract": gate_sym, "interval": CANDLE_INTERVAL,
                  "limit": min(per_page, limit - page * per_page)}
        if last_ts:
            params["to"] = last_ts
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise ValueError(f"HTTP {resp.status}")
                data = json.loads(text)
        if not data or not isinstance(data, list):
            break
        all_rows = data + all_rows
        last_ts = int(data[0]["t"]) - 1
        await asyncio.sleep(0.15)
    candles = []
    for row in all_rows:
        candles.append({
            "time": int(row["t"]),
            "close": float(row["c"]),
            "volume": float(row.get("sum", 0)),
            "price": float(row["c"]),
        })
    return candles

async def run_backtest():
    print("=" * 60)
    print("  Shark 2.0 三天回测 v2（15min 采样）")
    print("=" * 60)

    # 1. 合约规格
    print("\n📡 获取合约规格...")
    specs = await fetch_contract_specs()
    print(f"   {len(specs)} 个合约")

    # 2. 标的
    from main import fetch_top_symbols
    symbols = await fetch_top_symbols(n=TOP_SYMBOLS_N)
    print(f"\n📡 标的: {symbols}")

    # 3. 拉K线
    print("\n📡 拉取K线...")
    all_candles = {}
    for sym in symbols:
        try:
            candles = await fetch_candles(sym, limit=4320)
            if len(candles) > LOOKBACK_24H:
                all_candles[sym] = candles
                print(f"   {sym}: {len(candles)} 根")
        except Exception as e:
            print(f"   {sym}: ❌ {e}")

    if len(all_candles) < 3:
        print("❌ 数据不足")
        return

    # 4. 预计算每个币对的滚动24h指标（一次性算完）
    print("\n⚙️  预计算 24h 指标...")
    metrics = {}  # sym -> list of {time, price, vol_24h, chg_24h}
    for sym, candles in all_candles.items():
        m = []
        # 滑动窗口累加 volume
        vol_sum = sum(c["volume"] for c in candles[:LOOKBACK_24H])
        for i in range(LOOKBACK_24H, len(candles)):
            c = candles[i]
            prev = candles[i - LOOKBACK_24H]
            vol_sum += c["volume"] - prev["volume"]
            chg_24h = (c["price"] - prev["price"]) / prev["price"] * 100 if prev["price"] > 0 else 0
            m.append({
                "time": c["time"],
                "price": c["price"],
                "vol_24h": vol_sum,
                "chg_24h": chg_24h,
            })
        metrics[sym] = m
    print(f"   完成，有效数据点: {sum(len(m) for m in metrics.values())}")

    # 5. 构建统一时间轴（采样）
    all_times = sorted(set(m["time"] for ml in metrics.values() for m in ml))
    sampled_times = all_times[::SAMPLE_INTERVAL]
    print(f"   原始 {len(all_times)} ticks → 采样 {len(sampled_times)} ticks (每{SAMPLE_INTERVAL}min)")

    # 6. 初始化策略
    runner = StrategyRunner(initial_balance=100.0)
    runner.update_contracts(specs)

    start_t = sampled_times[0]
    end_t = sampled_times[-1]
    print(f"\n⏱️  回测: {time.strftime('%m/%d %H:%M', time.localtime(start_t))} ~ {time.strftime('%m/%d %H:%M', time.localtime(end_t))}")
    print(f"   开始...")

    equity_curve = []
    last_print = 0

    for i, ts in enumerate(sampled_times):
        prices, volumes, changes = {}, {}, {}
        for sym, ml in metrics.items():
            for m in ml:
                if m["time"] == ts:
                    prices[sym] = m["price"]
                    volumes[sym] = m["vol_24h"]
                    changes[sym] = m["chg_24h"]
                    break

        if prices:
            await runner.tick(prices, volumes, changes, {})
            equity_curve.append(runner.equity)

        pct = (i + 1) / len(sampled_times) * 100
        if pct - last_print >= 10:
            print(f"   {pct:.0f}% 权益=${runner.equity:.2f} 仓位={len(runner.positions)} 交易={runner.trades}")
            last_print = int(pct / 10) * 10

    # 7. 报告
    print("\n" + "=" * 60)
    print("  回测结果")
    print("=" * 60)

    final_eq = runner.equity
    ret = (final_eq - 100) / 100 * 100
    peak = 100
    max_dd = 0
    for eq in equity_curve:
        if eq > peak: peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd: max_dd = dd

    closed = runner.closed_trades
    wins = runner.wins
    wr = wins / max(closed, 1) * 100

    w_sum = l_sum = w_n = l_n = 0.0
    for t in runner._trade_history:
        if t["realized_pnl"] > 0:
            w_sum += t["realized_pnl"]; w_n += 1
        else:
            l_sum += abs(t["realized_pnl"]); l_n += 1
    avg_w = w_sum / max(w_n, 1)
    avg_l = l_sum / max(l_n, 1)
    pf = (avg_w * w_n) / max(avg_l * l_n, 0.0001)

    print(f"""
📊 账户
  初始:      $100.00
  最终:      ${final_eq:.2f} ({ret:+.2f}%)
  最大回撤:  {max_dd:.2f}%

📈 交易
  开仓: {runner.trades}  平仓: {closed}
  胜率: {wr:.1f}% ({wins}/{closed})
  均盈: ${avg_w:.4f}  均亏: ${avg_l:.4f}
  盈亏比: {pf:.2f}

💰 成本
  手续费: ${runner.total_fees:.4f}
  滑点:   ${runner.total_slippage:.4f}
""")

    # 按币对
    by_sym = defaultdict(lambda: {"t": 0, "w": 0, "pnl": 0.0})
    for t in runner._trade_history:
        s = t["symbol"]
        by_sym[s]["t"] += 1
        if t["realized_pnl"] > 0: by_sym[s]["w"] += 1
        by_sym[s]["pnl"] += t["realized_pnl"]

    print("📋 各币对:")
    for sym, st in sorted(by_sym.items(), key=lambda x: x[1]["pnl"]):
        swr = st["w"] / max(st["t"], 1) * 100
        print(f"  {sym:15s} {st['t']:3d}笔  胜率{swr:5.1f}%  {st['pnl']:+.4f}")

if __name__ == "__main__":
    asyncio.run(run_backtest())

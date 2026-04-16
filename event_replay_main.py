#!/usr/bin/env python3
"""
全真事件驱动回放：ReplayHarnessGateway 将历史 1m K 线拆成微观虚拟 Tick，
以生产路径调用 StrategyEngine.process_ws_tick（BetaNeutralHF / PaperEngine / RiskEngine）。

默认使用「微观布朗 + 正弦」插值（每根 K 至少 30 个价位点，且触及 High/Low），
替代简单 OHLC 折线；可选 --legacy-ohlc 恢复旧路径。

不包含任何独立开平仓公式；策略与纸面撮合逻辑全部在生产代码内完成。

用法（项目根）:
  SHARK_CONFIG_PATH=config/settings.yaml SKIP_LICENSE_CHECK=1 \\
    python3 event_replay_main.py --days 7 --insecure

可选:
  --symbols BTC/USDT,ETH/USDT
  --micro-min-ticks 30
  --micro-seed 42
  --legacy-ohlc              仅 OHLC 折线（旧行为）
  --no-spread-in-bar         整根 K 线共用一个时间戳（不推荐）
  --out-csv backtest_micro_trades.csv   微观成交账本 CSV（默认写出）
  --out-json data/replay/report.json
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(str(ROOT))
os.environ.setdefault("SHARK_CONFIG_PATH", str(ROOT / "config" / "settings.yaml"))
# 须在导入 StrategyEngine（会触发许可证）之前；发行版 COMMERCIAL_DISTRIBUTION=True 时无效
os.environ.setdefault("SKIP_LICENSE_CHECK", "1")

from src.core import event_replay_time as replay_time  # noqa: E402
from src.core.config_manager import config_manager  # noqa: E402
from src.core.globals import bot_context  # noqa: E402
from src.core.l1_fast_loop import reset_buffers_for_replay  # noqa: E402
from src.core.paper_engine import paper_engine  # noqa: E402
from src.core.risk_engine import risk_engine  # noqa: E402
from src.core.state_machine import StateMachine  # noqa: E402
from src.exchange.replay_gateway import (  # noqa: E402
    ReplayHarnessGateway,
    ohlc_price_path_from_bar,
)
from src.strategy.engine import StrategyEngine  # noqa: E402
from src.utils.logger import log, setup_logger  # noqa: E402


class _ReplayLicenseStub:
    """回放进程避免拉 cryptography；许可证仍由环境 SKIP_LICENSE_CHECK 控制。"""

    def validate(self) -> bool:
        return True


def _resolve_symbols() -> List[str]:
    cfg = config_manager.get_config()
    anchor = str(getattr(cfg.beta_neutral_hf, "anchor_symbol", "BTC/USDT") or "BTC/USDT").strip()
    alts = list(getattr(cfg.beta_neutral_hf, "symbols", None) or [])
    out: List[str] = []
    seen = set()
    for s in [anchor, *alts]:
        t = str(s).strip()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _align_bars(bars_by_symbol: Dict[str, List[Dict[str, Any]]]) -> Dict[str, List[Dict[str, Any]]]:
    n = min(len(v) for v in bars_by_symbol.values())
    return {k: v[-n:] for k, v in bars_by_symbol.items()}


def _dedupe_consecutive_prices(pts: List[float], *, eps: float = 1e-12) -> List[float]:
    out: List[float] = []
    for x in pts:
        fx = float(x)
        if not out or abs(fx - float(out[-1])) > eps:
            out.append(fx)
    return out


def micro_price_path_from_bar(
    bar: Dict[str, Any],
    *,
    min_ticks: int = 30,
    rng: random.Random,
) -> List[float]:
    """
    单根 1m K 的微观价格路径：正弦 + 离散布朗桥 + 强制触达 High/Low，至少 min_ticks 个「有效跳动」
    （返回序列含 Open 为首元素；网关对 path[1:] 注入，故总注入数 >= min_ticks）。

    volume 越大，微观步数与波动幅度略增（仍 capped），模拟更「吵」的分钟。
    """
    o = float(bar.get("o", bar.get("open", 0)) or 0)
    h = float(bar.get("h", bar.get("high", 0)) or 0)
    l = float(bar.get("l", bar.get("low", 0)) or 0)
    c = float(bar.get("c", bar.get("close", 0)) or 0)
    vol = float(bar.get("v", bar.get("volume", 0)) or 0)
    hi0, lo0 = max(h, l), min(h, l)
    if hi0 <= lo0 + 1e-14:
        wiggle = max(abs(float(o)) * 1e-6, 1e-6)
        hi = float(o) + wiggle
        lo = float(o) - wiggle
    else:
        hi, lo = hi0, lo0
    span = max(hi - lo, max(abs(o), 1e-12) * 1e-10)

    extra = int(min(120, math.log1p(max(vol, 0.0) + 1.0) * 14.0))
    n = max(int(min_ticks), 30, min(300, 30 + extra))
    if n < 2:
        n = 2

    vol_scale = 1.0 + min(2.5, math.log1p(max(vol, 0.0) + 1.0) * 0.04)
    if (hi - o) >= (o - lo):
        seq_vals = [o, hi, lo, c]
    else:
        seq_vals = [o, lo, hi, c]
    knot_u = [0.0, 0.28, 0.62, 1.0]

    def _piecewise_spine(u: float) -> float:
        u = min(1.0, max(0.0, u))
        k = 0
        while k + 1 < len(knot_u) and u > knot_u[k + 1]:
            k += 1
        k = min(k, len(knot_u) - 2)
        t0, t1 = knot_u[k], knot_u[k + 1]
        w = 0.0 if t1 <= t0 else (u - t0) / (t1 - t0)
        return float(seq_vals[k]) * (1.0 - w) + float(seq_vals[k + 1]) * w

    # 布朗桥：端点钉在 0，中间随机游走
    eps = [rng.gauss(0.0, 1.0) for _ in range(n)]
    prefix = [0.0]
    for i in range(1, n):
        prefix.append(prefix[-1] + eps[i])
    bridge = [0.0] * n
    denom = max(n - 1, 1)
    for i in range(n):
        u = i / denom
        bridge[i] = prefix[i] - u * prefix[-1]

    sigma = span * 0.07 * vol_scale / math.sqrt(max(n, 2))
    sine_phase = rng.uniform(0.0, 2.0 * math.pi)
    sine_k = 3.0 + 4.0 * rng.random()

    raw: List[float] = []
    for i in range(n):
        u = i / denom
        spine = _piecewise_spine(u)
        envelope = 0.25 + 0.75 * math.sin(math.pi * u)
        sine = span * 0.11 * vol_scale * envelope * math.sin(2.0 * math.pi * sine_k * u + sine_phase)
        rw = sigma * bridge[i]
        px = spine + sine + rw
        px = max(lo, min(hi, px))
        raw.append(px)

    raw[0] = o
    raw[-1] = c
    ih = max(1, min(n - 2, n // 4))
    il = max(1, min(n - 2, (3 * n) // 4))
    if il == ih:
        il = min(n - 2, ih + 1)
    raw[ih] = hi
    raw[il] = lo

    out = _dedupe_consecutive_prices(raw, eps=1e-14)
    if len(out) < 2:
        if abs(o - c) > 1e-12:
            out = [float(o), float(c)]
        elif hi > lo + 1e-12:
            out = [float(lo), float(hi)]
        else:
            tick = max(abs(float(o)) * 1e-9, 1e-12)
            out = [float(o), float(min(hi, max(lo, o + tick)))]

    _pad_guard = 0
    while len(out) - 1 < int(min_ticks) and len(out) < 500:
        _pad_guard += 1
        if _pad_guard > 4000:
            break
        if len(out) < 2:
            if abs(o - c) > 1e-12:
                out = [float(o), float(c)]
            elif hi > lo + 1e-12:
                out = [float(lo), float(hi)]
            else:
                tick = max(abs(float(o)) * 1e-9, 1e-12)
                out = [float(o), float(min(hi, max(lo, o + tick)))]
            continue
        upper = len(out) - 2
        if upper < 0:
            break
        idx = rng.randint(0, upper)
        a, b = float(out[idx]), float(out[idx + 1])
        mid = 0.5 * (a + b) + span * 2e-4 * rng.gauss(0.0, 1.0)
        mid = max(lo, min(hi, mid))
        if abs(mid - a) <= 1e-14 and abs(mid - b) <= 1e-14:
            bump = max(span * 1e-6, abs(a) * 1e-10, 1e-12)
            mid = min(hi, max(lo, 0.5 * (a + b) + bump * (1.0 if rng.random() > 0.5 else -1.0)))
        if abs(mid - a) <= 1e-14 and abs(mid - b) <= 1e-14:
            mid = min(hi, max(lo, a + max(hi - lo, abs(a) * 1e-10) * 1e-5))
        out.insert(idx + 1, mid)
        out = _dedupe_consecutive_prices(out, eps=1e-14)
    return out


def make_micro_price_path_fn(min_ticks: int, seed: int) -> Callable[[Dict[str, Any]], List[float]]:
    def _fn(bar: Dict[str, Any]) -> List[float]:
        t = int(bar.get("t", 0) or 0)
        salt = (int(seed) * 1_000_003 + t * 97 + int(float(bar.get("o", 0) or 0) * 1e6)) & 0x7FFFFFFF
        rng = random.Random(salt)
        return micro_price_path_from_bar(bar, min_ticks=min_ticks, rng=rng)

    return _fn


def _install_backtest_ledger() -> Tuple[Any, List[Dict[str, Any]]]:
    """
    动态挂载 paper_engine.backtest_ledger，并包装 execute_order：
    非 rejected/resting 的成交记一行；fee_leg 用 accumulated_fee_paid 差分；平仓带净盈亏字段（若引擎返回）。
    """
    ledger: List[Dict[str, Any]] = []
    paper_engine.backtest_ledger = ledger
    orig = paper_engine.execute_order

    def wrapped(*args: Any, **kwargs: Any) -> dict:
        fee0 = float(paper_engine.accumulated_fee_paid)
        res = orig(*args, **kwargs)
        fee1 = float(paper_engine.accumulated_fee_paid)
        fee_leg = fee1 - fee0
        st = str(res.get("status", "") or "")
        if st in ("rejected", "resting"):
            return res
        symbol = str(args[0]) if len(args) > 0 else str(kwargs.get("symbol", ""))
        side = str(args[1]) if len(args) > 1 else str(kwargs.get("side", ""))
        amount = float(args[2]) if len(args) > 2 else float(kwargs.get("amount", 0) or 0)
        reduce_only = bool(kwargs.get("reduce_only", False))
        if len(args) > 4:
            reduce_only = bool(args[4])
        row: Dict[str, Any] = {
            "ledger_seq": len(ledger),
            "ts_virtual": float(time.time()),
            "symbol": symbol,
            "side": side,
            "reduce_only": reduce_only,
            "status": st,
            "amount": amount,
            "filled": float(res.get("filled", amount) or 0.0),
            "avg_price": float(res.get("avg_price", res.get("price", 0.0)) or 0.0),
            "fee_leg_usdt": float(fee_leg),
            "gross_realized_pnl_usdt": res.get("gross_realized_pnl_usdt", ""),
            "fee_open_total_usdt": res.get("fee_open_total_usdt", ""),
            "fee_close_leg_usdt": res.get("fee_close_leg_usdt", ""),
            "final_trade_pnl_net_usdt": res.get("final_trade_pnl_net_usdt", res.get("realized_net_usdt", "")),
            "order_id": str(res.get("id", "")),
        }
        ledger.append(row)
        return res

    paper_engine.execute_order = wrapped  # type: ignore[method-assign]
    return orig, ledger


def _restore_execute_order(orig: Any) -> None:
    paper_engine.execute_order = orig  # type: ignore[method-assign]


def _write_ledger_csv(path: str, ledger: List[Dict[str, Any]]) -> Dict[str, Any]:
    fields = [
        "ledger_seq",
        "ts_virtual",
        "symbol",
        "side",
        "reduce_only",
        "status",
        "amount",
        "filled",
        "avg_price",
        "fee_leg_usdt",
        "gross_realized_pnl_usdt",
        "fee_open_total_usdt",
        "fee_close_leg_usdt",
        "final_trade_pnl_net_usdt",
        "order_id",
    ]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in ledger:
            w.writerow({k: row.get(k, "") for k in fields})
    total_fees = sum(float(r.get("fee_leg_usdt") or 0) for r in ledger)
    nets: List[float] = []
    for r in ledger:
        v = r.get("final_trade_pnl_net_usdt", "")
        if v != "" and v is not None:
            try:
                nets.append(float(v))
            except (TypeError, ValueError):
                pass
    net_sum = sum(nets)
    summary = {
        "ledger_rows": len(ledger),
        "sum_fee_leg_usdt": round(total_fees, 8),
        "sum_final_trade_pnl_net_usdt": round(net_sum, 8),
    }
    return summary


async def _run(args: argparse.Namespace) -> int:
    setup_logger()
    config_manager.load_config()
    cfg = config_manager.get_config()
    paper_engine.apply_config_fees()

    if bool(args.insecure):
        os.environ["BACKTEST_INSECURE_SSL"] = "1"

    if str(args.symbols).strip():
        symbols = [s.strip() for s in str(args.symbols).split(",") if s.strip()]
    else:
        symbols = _resolve_symbols()

    initial = float(getattr(cfg.paper_engine, "initial_balance_usdt", 10_000.0) or 10_000.0)
    reset_buffers_for_replay()
    paper_engine.reset_for_event_replay(initial)
    risk_engine.reset_for_event_replay(initial)

    orig_execute, ledger = _install_backtest_ledger()

    sm = StateMachine(_ReplayLicenseStub())
    sm.set_trading_mode_from_ui(str(getattr(args, "trading_mode", "NEUTRAL") or "NEUTRAL"))

    exchange = ReplayHarnessGateway(
        api_key=str(cfg.exchange.api_key or ""),
        api_secret=str(cfg.exchange.api_secret or ""),
        on_tick=None,
        on_orderbook=None,
        testnet=bool(cfg.exchange.sandbox_mode),
        use_paper_trading=True,
        on_trade=None,
    )
    engine = StrategyEngine(exchange, sm)
    exchange.on_tick = engine.process_ws_tick
    exchange.on_orderbook = engine.process_ws_orderbook
    exchange.on_trade = engine.process_ws_trade
    bot_context.set_components(exchange, engine, sm)

    if bool(args.legacy_ohlc):
        price_path_fn: Callable[[Dict[str, Any]], List[float]] = ohlc_price_path_from_bar
    else:
        price_path_fn = make_micro_price_path_fn(int(args.micro_min_ticks), int(args.micro_seed))

    await exchange.start_rest_session()
    wall_start = time.perf_counter()
    stats: Dict[str, Any] = {"ticks": 0, "bars": 0, "symbols": symbols, "capped": False}
    try:
        if not bool(args.skip_full_physics_sync):
            await exchange.sync_usdt_futures_physics_matrix()
        await exchange.subscribe_market_data(symbols)
        risk_engine.update_balance(initial)

        bars_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
        for sym in symbols:
            log.info(f"[Replay] fetching 1m candles {sym} days={args.days} …")
            bars_by_symbol[sym] = await exchange.fetch_1m_candles_paginated(sym, int(args.days))
            if len(bars_by_symbol[sym]) < 20:
                log.error(f"[Replay] insufficient bars for {sym}: {len(bars_by_symbol[sym])}")
                return 2
        aligned = _align_bars(bars_by_symbol)

        replay_time.activate()
        replay_start = time.perf_counter()
        try:
            await engine.start_replay_workers()
            n_bars = min(len(aligned[s]) for s in aligned) if aligned else 0
            log.info(
                f"[Replay] aligned 1m bars={n_bars} symbols={len(symbols)} "
                f"(progress every {int(args.progress_every_bars)} bar(s), 0=quiet)"
            )
            stats = await exchange.replay_bars_ohlc_path(
                bars_by_symbol=aligned,
                price_path_fn=price_path_fn,
                tick_pause_sec=float(args.tick_pause),
                max_ticks=int(args.max_ticks) if int(args.max_ticks) > 0 else 0,
                spread_ticks_in_bar=not bool(args.no_spread_in_bar),
                progress_every_bars=int(args.progress_every_bars),
            )
            await asyncio.sleep(float(args.drain_sec))
        finally:
            replay_time.deactivate()
            await engine.stop_replay_workers()
        stats["replay_loop_sec"] = round(time.perf_counter() - replay_start, 4)
    finally:
        _restore_execute_order(orig_execute)
        await exchange.close_rest_session()

    elapsed = time.perf_counter() - wall_start
    snap = paper_engine.financial_snapshot()
    positions = paper_engine.get_positions()
    csv_summary: Dict[str, Any] = {}
    out_csv = "" if bool(getattr(args, "no_csv", False)) else str(args.out_csv or "").strip()
    if out_csv:
        csv_summary = _write_ledger_csv(out_csv, ledger)

    report = {
        "schema": "shark.event_replay_report.v1",
        "generated_at": time.time(),
        "elapsed_wall_sec": round(elapsed, 3),
        "virtual_ticks": stats,
        "micro_mode": "legacy_ohlc" if bool(args.legacy_ohlc) else "brownian_sine_micro",
        "ledger_csv": out_csv or None,
        "ledger_summary": csv_summary,
        "paper_financial": snap,
        "positions": positions,
        "risk_realized_pnl": float(risk_engine.realized_pnl or 0.0),
        "risk_equity": float(risk_engine.total_equity or 0.0),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    out = str(args.out_json or "").strip()
    if out:
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        log.info(f"[Replay] wrote {out}")
    if out_csv:
        log.info(f"[Replay] ledger CSV: {out_csv} rows={csv_summary.get('ledger_rows', 0)}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="事件驱动全真回放（生产 StrategyEngine + PaperEngine）")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--symbols", default="", help="逗号分隔；默认锚+beta_neutral_hf.symbols")
    ap.add_argument("--insecure", action="store_true")
    ap.add_argument("--tick-pause", type=float, default=0.0, help="每虚拟 tick sleep 秒数（默认 0 全速）")
    ap.add_argument("--max-ticks", type=int, default=0, help=">0 时最多推送多少虚拟 tick")
    ap.add_argument("--drain-sec", type=float, default=0.35, help="结束后等待信号队列消化")
    ap.add_argument("--trading-mode", default="NEUTRAL", choices=("NEUTRAL", "ATTACK", "BERSERKER", "AUTO"))
    ap.add_argument("--out-json", default="", help="回放报告 JSON 路径")
    ap.add_argument(
        "--out-csv",
        default="backtest_micro_trades.csv",
        help="Paper 成交账本 CSV 路径",
    )
    ap.add_argument("--no-csv", action="store_true", help="不写 CSV 账本")
    ap.add_argument(
        "--progress-every-bars",
        type=int,
        default=200,
        help="每处理多少根对齐 K 线打一条进度日志（0=关闭）；含 ticks/s 与粗略 ETA",
    )
    ap.add_argument("--micro-min-ticks", type=int, default=30, help="每根 1m K 至少注入多少微观 tick（path[1:] 长度下限）")
    ap.add_argument("--micro-seed", type=int, default=42, help="微观路径随机种子（与 bar 时间混合）")
    ap.add_argument("--legacy-ohlc", action="store_true", help="使用旧 OHLC 折线路径而非微观生成器")
    ap.add_argument(
        "--no-spread-in-bar",
        action="store_true",
        help="不在分钟内铺开 ts_ms（整 bar 同一时间戳）",
    )
    ap.add_argument(
        "--skip-full-physics-sync",
        action="store_true",
        help="跳过全市场 sync_usdt_futures_physics_matrix（仅依赖各 symbol 的 quanto 预热；本地缺依赖时可加快启动）",
    )
    args = ap.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()

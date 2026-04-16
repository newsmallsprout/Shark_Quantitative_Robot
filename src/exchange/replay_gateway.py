"""
ReplayHarnessGateway：继承 GateFuturesGateway，关闭实盘 WS，由历史 K 线拆解的
OHLC 虚拟 Tick 驱动 `StrategyEngine.process_ws_tick`（与生产路径一致）。

不在此模块实现任何开平仓公式；仅注入行情与最小合成盘口，供 paper_engine 撮合。
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from src.core import event_replay_time as replay_time
from src.core.paper_engine import paper_engine
from src.core.position_exit_monitor import position_exit_monitor
from src.exchange.gate_gateway import GATE_MAINNET_REST, GateFuturesGateway
from src.utils.logger import log

BAR_SECONDS = 60
MAX_CANDLES_CAP = 2000
GATE_RECENT_1M_POINTS = 10000


def _dedupe_consecutive_prices(pts: List[float], *, eps: float = 1e-14) -> List[float]:
    out: List[float] = []
    for x in pts:
        if not out or abs(float(x) - float(out[-1])) > eps:
            out.append(float(x))
    return out


def replay_price_path_for_bar(o: float, h: float, l: float, c: float, whipsaw_hl_pairs: int = 0) -> List[float]:
    """单根 1m OHLC 折线路径（与 backtest_runner.price_path_for_bar 语义一致）。"""
    o, h, l, c = float(o), float(h), float(l), float(c)
    hi, lo = max(h, l), min(h, l)
    w = max(0, int(whipsaw_hl_pairs))
    if h - o >= o - l:
        seq: List[float] = [o, h, l]
        cur = seq[-1]
        for _ in range(w):
            if abs(hi - cur) > 1e-18:
                seq.append(hi)
                cur = hi
            if abs(lo - cur) > 1e-18:
                seq.append(lo)
                cur = lo
        seq.append(c)
        return _dedupe_consecutive_prices(seq)
    seq = [o, l, h]
    cur = seq[-1]
    for _ in range(w):
        if abs(lo - cur) > 1e-18:
            seq.append(lo)
            cur = lo
        if abs(hi - cur) > 1e-18:
            seq.append(hi)
            cur = hi
    seq.append(c)
    return _dedupe_consecutive_prices(seq)


def ohlc_price_path_from_bar(bar: Dict[str, Any]) -> List[float]:
    """兼容旧接口：单 bar → OHLC 折线（供 price_path_fn(bar) 使用）。"""
    o = float(bar.get("o", 0) or 0)
    h = float(bar.get("h", 0) or 0)
    l = float(bar.get("l", 0) or 0)
    c = float(bar.get("c", 0) or 0)
    return replay_price_path_for_bar(o, h, l, c, 0)


def _parse_candle_chunk(chunk: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not isinstance(chunk, list):
        return rows
    for row in chunk:
        if isinstance(row, dict):
            t = int(row.get("t", 0) or 0)
            o = float(row.get("o", 0) or 0)
            h = float(row.get("h", 0) or 0)
            l = float(row.get("l", 0) or 0)
            c = float(row.get("c", 0) or 0)
            raw_v = row.get("v", row.get("sum", row.get("volume", 0)))
            v = float(raw_v or 0)
        elif isinstance(row, (list, tuple)) and len(row) >= 5:
            t = int(row[0])
            o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
            v = float(row[5]) if len(row) >= 6 else 0.0
        else:
            continue
        if t <= 0 or h <= 0 or l <= 0:
            continue
        rows.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": v})
    rows.sort(key=lambda x: x["t"])
    return rows


def _max_limit_for_to(server_now: int, to_ts: int) -> int:
    floor_ts = int(server_now) - GATE_RECENT_1M_POINTS * BAR_SECONDS
    span = int(to_ts) - floor_ts
    if span < BAR_SECONDS:
        return 0
    return min(MAX_CANDLES_CAP, max(1, span // BAR_SECONDS))


def _synthetic_spread(mid: float) -> Tuple[float, float]:
    eps = max(abs(float(mid)) * 1e-6, 1e-8)
    return mid - eps, mid + eps


class ReplayHarnessGateway(GateFuturesGateway):
    """
    纸面 + 主网 REST（合约元数据），不连接行情 WS；由 `inject_ticker` / `replay_bars` 灌 Tick。
    """

    async def start_rest_session(self) -> None:
        """允许 BACKTEST_INSECURE_SSL=1 时跳过 TLS 校验（与 backtest_runner 一致）。"""
        if self.session:
            return
        insecure = os.environ.get("BACKTEST_INSECURE_SSL", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        if insecure:
            conn = aiohttp.TCPConnector(ssl=False)
        else:
            conn = aiohttp.TCPConnector(ssl=True)
        self.session = aiohttp.ClientSession(connector=conn)

    async def _replay_fetch_json(self, url: str) -> Any:
        await self.start_rest_session()
        async with self.session.get(url, timeout=120) as resp:
            return await resp.json()

    async def _replay_gate_server_time_sec(self) -> int:
        data = await self._replay_fetch_json(f"{GATE_MAINNET_REST}/spot/time")
        ms = int((data or {}).get("server_time", 0) or 0)
        if ms > 10_000_000_000:
            return ms // 1000
        return ms

    async def fetch_1m_candles_paginated(self, symbol: str, days: int) -> List[Dict[str, Any]]:
        """Gate 公开 1m，分页逻辑与 backtest_runner 对齐（异步）。"""
        contract = symbol.replace("/", "_").replace(":", "_")
        server_now = await self._replay_gate_server_time_sec()
        start_target = int(server_now) - int(days * 86400)
        floor_ts = int(server_now) - GATE_RECENT_1M_POINTS * BAR_SECONDS
        to_ts = int(server_now)
        by_t: Dict[int, Dict[str, Any]] = {}
        prev_oldest: Optional[int] = None

        for _ in range(2000):
            lim = _max_limit_for_to(server_now, to_ts)
            if lim <= 0:
                break
            url = (
                f"{GATE_MAINNET_REST}/futures/usdt/candlesticks?"
                f"contract={contract}&interval=1m&limit={lim}&to={to_ts}"
            )
            chunk = await self._replay_fetch_json(url)
            rows = _parse_candle_chunk(chunk)
            if not rows:
                break
            oldest = int(rows[0]["t"])
            if prev_oldest is not None and oldest >= prev_oldest:
                break
            prev_oldest = oldest
            for r in rows:
                if r["t"] >= start_target:
                    by_t[r["t"]] = r
            if oldest <= start_target:
                break
            to_ts = oldest - BAR_SECONDS
            if to_ts <= floor_ts:
                break

        merged = [by_t[k] for k in sorted(by_t.keys())]
        return [r for r in merged if r["t"] < server_now]

    async def start_ws(self) -> None:
        self.running = True
        await self.start_rest_session()
        log.info("[ReplayHarness] Live WS suppressed — inject_ticker / replay_bars drives the engine.")

    async def stop_ws(self) -> None:
        self.running = False
        log.info("[ReplayHarness] stop_ws (no live socket).")

    def seed_synthetic_orderbook(self, symbol: str, mid: float) -> None:
        bb, ba = _synthetic_spread(mid)
        paper_engine.orderbooks_cache[symbol] = {
            "bids": [[bb, 1e9]],
            "asks": [[ba, 1e9]],
        }

    async def inject_ticker(
        self,
        symbol: str,
        last: float,
        *,
        ts_ms: int,
    ) -> None:
        """单步：对齐虚拟时间 → L1 → 纸面定价 → 合成盘口 → 引擎 process_ws_tick。"""
        from src.core import l1_fast_loop

        ts_sec = float(ts_ms) / 1000.0
        replay_time.set_virtual(ts_sec)
        px = float(last)
        if px > 0:
            l1_fast_loop.on_ticker_price(symbol, ts_sec, px)
        if self.use_paper_trading and px > 0:
            paper_engine.update_price(symbol, px)
            self.seed_synthetic_orderbook(symbol, px)
            await position_exit_monitor.on_ticker(self, symbol, px)
        vol = float((self.contract_specs_cache.get(symbol) or {}).get("24h_volume", 0) or 0.0)
        ticker = {
            "symbol": symbol,
            "last": px,
            "mark_price": px,
            "volume": vol,
            "timestamp": int(ts_ms),
        }
        self.latest_tick_by_symbol[symbol] = ticker
        if self.on_tick:
            await self.on_tick(symbol, ticker)

    async def replay_bars_ohlc_path(
        self,
        *,
        bars_by_symbol: Dict[str, List[Dict[str, Any]]],
        price_path_fn,
        tick_pause_sec: float = 0.0,
        max_ticks: int = 0,
        spread_ticks_in_bar: bool = True,
        progress_every_bars: int = 0,
    ) -> Dict[str, Any]:
        """
        对每个时间索引 i，按 symbols 顺序推送 path。

        price_path_fn(bar: dict) -> List[float]，首元素通常为 Open；path[1:] 为逐笔注入价。
        spread_ticks_in_bar=True 时，将 tick 的 ts_ms 均匀铺在本分钟 [t, t+60s) 内，便于微观回放对齐虚拟时钟。
        """
        if not bars_by_symbol:
            return {"ticks": 0, "bars": 0, "symbols": []}
        syms = list(bars_by_symbol.keys())
        n = min(len(bars_by_symbol[s]) for s in syms)
        ticks = 0
        cap = int(max_ticks) if int(max_ticks) > 0 else 0
        prog = max(0, int(progress_every_bars))
        loop_t0 = time.perf_counter()
        log.info(
            f"[Replay] tick pump start: {n} bars × {len(syms)} symbols "
            f"(micro path ≈ tens of ticks/bar/symbol → expect millions of process_ws_tick calls)"
        )
        for i in range(n):
            if prog and (i % prog == 0 or i == n - 1):
                dt = time.perf_counter() - loop_t0
                pct = 100.0 * i / max(n, 1)
                tps = ticks / dt if dt > 0 else 0.0
                eta_s = (n - i) * (dt / i) if i > 0 else None
                eta_txt = f" ETA~{eta_s:.0f}s" if eta_s is not None else ""
                log.info(
                    f"[Replay] progress bar_index={i}/{n} ({pct:.1f}%), virtual_ticks={ticks}, "
                    f"~{tps:.0f} ticks/s{eta_txt}"
                )
            for sym in syms:
                bar = bars_by_symbol[sym][i]
                ts = int(bar.get("t", bar.get("time", 0) or 0))
                ts_ms_base = ts * 1000
                path = price_path_fn(bar)
                if not path or len(path) < 2:
                    continue
                sub = path[1:]
                m = len(sub)
                for j, px in enumerate(sub):
                    if spread_ticks_in_bar and m > 0:
                        ts_ms = ts_ms_base + int((j + 1) / m * (BAR_SECONDS * 1000 - 1))
                    else:
                        ts_ms = ts_ms_base
                    await self.inject_ticker(sym, float(px), ts_ms=ts_ms)
                    ticks += 1
                    if cap and ticks >= cap:
                        return {"ticks": ticks, "bars": i + 1, "symbols": syms, "capped": True}
                    if tick_pause_sec > 0:
                        await asyncio.sleep(float(tick_pause_sec))
        return {"ticks": ticks, "bars": n, "symbols": syms, "capped": False}

"""
全天候广域成交量雷达：REST 拉全市场 ticker → 5m 成交量加速度 vs 24h 均档 5m 量；
点差/Hurdle 过滤后动态追加 futures OB+trades 订阅；叙事 LLM 为可插拔闸门（默认关闭）。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

import aiohttp

from src.ai.l2_command import _contract_to_symbol, fetch_gate_usdt_tickers
from src.core.config_manager import config_manager
from src.core.paper_engine import paper_engine
from src.utils.logger import log

# 供仪表盘 WS 合并
_radar_snapshot: Dict[str, Any] = {
    "enabled": False,
    "last_scan_ts": 0.0,
    "prey": [],
    "scan_count": 0,
    "error": "",
}

Hist = Deque[Tuple[float, float]]  # (mono_ts, volume_24h_quote)


def get_volume_radar_payload() -> Dict[str, Any]:
    return dict(_radar_snapshot)


def _hurdle_from_ticker_row(row: Dict[str, Any], taker: float, maker: float) -> float:
    """Hurdle ≈ taker+maker + spread/mid（与 assassin_cost 一致，用 REST 买卖一）。"""
    try:
        bid = float(row.get("highest_bid") or row.get("bid") or 0)
        ask = float(row.get("lowest_ask") or row.get("ask") or 0)
    except (TypeError, ValueError):
        bid, ask = 0.0, 0.0
    spread_frac = 0.0
    if bid > 0 and ask > 0 and ask >= bid:
        mid = 0.5 * (bid + ask)
        if mid > 0:
            spread_frac = (ask - bid) / mid
    return max(0.0, float(taker) + float(maker) + spread_frac)


def _row_to_symbol(row: Dict[str, Any]) -> str:
    return _contract_to_symbol(str(row.get("contract", "")))


async def narrative_allow_entry(symbol: str, row: Dict[str, Any]) -> bool:
    """
    AI 叙事闸门占位：接入 Twitter/X 与 LLM 后在此实现一票否决。
    narrative_llm_enabled=false 时一律放行。
    """
    rc = config_manager.get_config().volume_radar
    if not getattr(rc, "narrative_llm_enabled", False):
        return True
    # TODO: 调用检索 + LLMFactory，输出 allow / veto
    log.info(f"[Radar] narrative check skipped (stub) for {symbol}")
    return True


class VolumeRadar:
    def __init__(self) -> None:
        self._hist: Dict[str, Hist] = {}
        self._prey_last: Dict[str, float] = {}
        self._subscribed_prey: Deque[str] = deque()

    def _trim(self, symbol: str, now: float, max_age: float) -> None:
        dq = self._hist.setdefault(symbol, deque())
        while dq and now - dq[0][0] > max_age:
            dq.popleft()

    def _velocity_ratio(
        self, symbol: str, vol_now: float, now: float, min_span: float
    ) -> Tuple[Optional[float], float]:
        dq = self._hist.setdefault(symbol, deque())
        self._trim(symbol, now, 400.0)
        dq.append((now, vol_now))
        if len(dq) < 2:
            return None, 0.0
        oldest = dq[0]
        span = now - oldest[0]
        if span < min_span:
            return None, span
        dv = vol_now - oldest[1]
        if dv < 0:
            dv = 0.0
        avg_5m = vol_now / 288.0
        if avg_5m <= 0:
            return None, span
        return dv / avg_5m, span

    async def _one_scan(self, exchange: Any, session: aiohttp.ClientSession) -> None:
        rc = config_manager.get_config().volume_radar
        if not rc.enabled:
            _radar_snapshot["enabled"] = False
            return

        now = time.time()
        rows = await fetch_gate_usdt_tickers(session)
        prey_out: List[Dict[str, Any]] = []
        taker = float(paper_engine.taker_fee)
        maker = float(paper_engine.maker_fee)

        for r in rows:
            if not isinstance(r, dict):
                continue
            sym = _row_to_symbol(r)
            if not sym.endswith("/USDT"):
                continue
            try:
                vq = float(r.get("volume_24h_quote", 0) or 0)
            except (TypeError, ValueError):
                vq = 0.0
            if vq < float(rc.min_quote_vol_24h):
                continue
            try:
                chg = float(r.get("change_percentage", 0) or 0)
            except (TypeError, ValueError):
                chg = 0.0

            ratio, span = self._velocity_ratio(sym, vq, now, float(rc.min_history_span_sec))
            if ratio is None:
                continue
            if ratio < float(rc.velocity_ratio_threshold):
                continue
            if abs(chg) < float(rc.min_change_pct_abs):
                continue

            hurdle = _hurdle_from_ticker_row(r, taker, maker)
            if hurdle > float(rc.max_hurdle_frac):
                log.debug(
                    f"[Radar] skip {sym}: hurdle={hurdle*10000:.1f}bps > max "
                    f"{float(rc.max_hurdle_frac)*10000:.1f}bps"
                )
                continue

            if now - self._prey_last.get(sym, 0) < float(rc.prey_cooldown_sec):
                continue

            if not await narrative_allow_entry(sym, r):
                log.info(f"[Radar] narrative veto {sym}")
                continue

            self._prey_last[sym] = now
            prey_out.append(
                {
                    "symbol": sym,
                    "velocity_ratio": round(ratio, 2),
                    "history_span_sec": round(span, 1),
                    "change_pct": round(chg, 3),
                    "volume_24h_quote": vq,
                    "hurdle_frac": round(hurdle, 6),
                    "hurdle_bps": round(hurdle * 10000, 2),
                }
            )

            if getattr(exchange, "subscribe_market_data", None) and rc.auto_subscribe_ws:
                fmt = sym.replace("/", "_")
                if fmt not in getattr(exchange, "subscribed_symbols", set()):
                    while len(self._subscribed_prey) >= int(rc.max_prey_extra_subs):
                        self._subscribed_prey.popleft()
                    self._subscribed_prey.append(sym)
                    try:
                        await exchange.subscribe_market_data([sym])
                        log.warning(
                            f"[Radar] PREY LOCK {sym} ratio={ratio:.1f}x | "
                            f"hurdle={hurdle*10000:.1f}bps | chg={chg:.2f}% → WS+OB+trades"
                        )
                    except Exception as e:
                        log.error(f"[Radar] subscribe failed {sym}: {e}")

        prey_out.sort(key=lambda x: -float(x.get("velocity_ratio", 0)))
        _radar_snapshot["enabled"] = True
        _radar_snapshot["last_scan_ts"] = now
        _radar_snapshot["prey"] = prey_out[: int(rc.max_prey_list_ui)]
        _radar_snapshot["scan_count"] = int(_radar_snapshot.get("scan_count", 0) or 0) + 1
        _radar_snapshot["error"] = ""

    async def run(self, exchange: Any) -> None:
        log.info("[Radar] Volume radar task started")
        connector = aiohttp.TCPConnector(ssl=True)
        async with aiohttp.ClientSession(connector=connector) as session:
            while getattr(exchange, "running", True):
                rc = config_manager.get_config().volume_radar
                if not rc.enabled:
                    _radar_snapshot["enabled"] = False
                    await asyncio.sleep(2.0)
                    continue
                try:
                    await self._one_scan(exchange, session)
                except Exception as e:
                    _radar_snapshot["error"] = str(e)[:200]
                    log.error(f"[Radar] scan error: {e}")
                await asyncio.sleep(float(rc.poll_interval_sec))


async def run_volume_radar_loop(exchange: Any) -> None:
    radar = VolumeRadar()
    await radar.run(exchange)

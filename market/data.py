"""合约规格 + 行情数据获取 + 交易对发现。"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Dict, List

import aiohttp

_log = logging.getLogger(__name__)

# 手续费 / 滑点 / 真实参数
TAKER_FEE = 0.0005
MAKER_FEE = 0.0002
SLIPPAGE_MAX = 0.0003
_FUSE_SL_STREAK_LIMIT = 3
TRADE_INTERVAL = 1
ALT_PLAN_TTL_SEC = 600
SL_PCT = -6.0
TIMEOUT_SEC = 300
MAX_TOTAL_EXPOSURE = 0.95


@dataclass
class ContractSpec:
    symbol: str
    leverage_max: int = 100
    order_size_min: float = 1
    quanto_multiplier: float = 1
    mark_price: float = 0
    funding_rate: float = 0
    funding_next_apply: float = 0
    taker_fee: float = 0.00075
    maker_fee: float = -0.0001


_contract_cache: Dict[str, ContractSpec] = {}


async def _wait_gate_rl(name: str = "gateio_rest", limit: int = 30, window_sec: int = 1) -> None:
    from persistence.redis_rate_limit import fixed_window_allow
    br = None  # will be set by main()
    while True:
        if br and br.redis:
            if await fixed_window_allow(br.redis, name=name, limit=limit, window_sec=window_sec):
                return
        else:
            return
        await asyncio.sleep(0.05)


def set_storage_bridge(br):
    global _storage_bridge
    _storage_bridge = br


async def fetch_contract_specs() -> Dict[str, ContractSpec]:
    await _wait_gate_rl()
    url = "https://api.gateio.ws/api/v4/futures/usdt/contracts"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
    specs = {}
    for c in data:
        sym = str(c.get("name", "")).replace("_USDT", "/USDT")
        if not sym or "/USDT" not in sym:
            continue
        specs[sym] = ContractSpec(
            symbol=sym,
            leverage_max=min(int(c.get("leverage_max", 100) or 100), 125),
            order_size_min=float(c.get("order_size_min", 1) or 1),
            quanto_multiplier=float(c.get("quanto_multiplier", 1) or 1),
            mark_price=float(c.get("mark_price", 0) or 0),
            funding_rate=float(c.get("funding_rate", 0) or 0),
            funding_next_apply=float(c.get("funding_next_apply", 0) or 0),
            taker_fee=float(c.get("taker_fee_rate", 0.00075) or 0.00075),
            maker_fee=float(c.get("maker_fee_rate", -0.0001) or -0.0001),
        )
    return specs


async def fetch_top_symbols(n: int = 30, min_vol: float = 30000) -> List[str]:
    await _wait_gate_rl()
    url = "https://api.gateio.ws/api/v4/futures/usdt/tickers"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
    scored = []
    for t in data:
        try:
            vol = float(t.get("volume_24h_quote", 0) or 0)
            chg = abs(float(t.get("change_percentage", 0) or 0))
            sym = str(t.get("contract", "") or "")
            if vol < min_vol or not sym.endswith("_USDT"):
                continue
            score = vol * (1 + chg)
            scored.append((sym.replace("_USDT", "/USDT"), score, vol, chg))
        except Exception:
            continue
    scored.sort(key=lambda x: x[1], reverse=True)
    return [s[0] for s in scored[:n]]


def rank_hot_volatile_symbols(tickers: list, n: int = 18, min_vol: float = 500_000,
                              min_change: float = 8.0) -> List[str]:
    from strategy.dual import is_stable
    scored = []
    for t in tickers or []:
        try:
            vol = float(t.get("volume_24h_quote", 0) or 0)
            chg = abs(float(t.get("change_percentage", 0) or 0))
            sym = str(t.get("contract", "") or "")
            if vol < min_vol or chg < min_change or not sym.endswith("_USDT"):
                continue
            symbol = sym.replace("_USDT", "/USDT")
            if is_stable(symbol):
                continue
            scored.append((symbol, chg, vol))
        except Exception:
            continue
    scored.sort(key=lambda x: x[1], reverse=True)
    return [s[0] for s in scored[:n]]


async def fetch_hot_volatile_symbols(n: int = 18) -> List[str]:
    await _wait_gate_rl()
    url = "https://api.gateio.ws/api/v4/futures/usdt/tickers"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
    return rank_hot_volatile_symbols(data, n=n)


@dataclass
class LiveTicker:
    symbol: str; price: float = 0; volume_24h: float = 0; change_pct: float = 0
    funding_rate: float = 0; mark_price: float = 0


class MarketDataFeed:
    def __init__(self): self._cache: Dict[str, LiveTicker] = {}

    async def refresh(self, symbols: List[str]):
        if not isinstance(symbols, (list, tuple)):
            symbols = []
        url = "https://api.gateio.ws/api/v4/futures/usdt/tickers"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        _log.warning("MarketDataFeed: tickers HTTP %s", resp.status)
                        return
                    data = await resp.json(content_type=None)
        except asyncio.TimeoutError:
            _log.warning("MarketDataFeed: tickers timeout")
            return
        except (aiohttp.ClientError, ValueError, TypeError, json.JSONDecodeError) as e:
            _log.warning("MarketDataFeed: %s", e)
            return
        if not isinstance(data, list):
            return
        tickers = {}
        for t in data:
            if not isinstance(t, dict):
                continue
            sym = str(t.get("contract", "")).replace("_USDT", "/USDT")
            if sym in symbols:
                tickers[sym] = LiveTicker(
                    symbol=sym,
                    price=float(t.get("last", 0) or 0),
                    volume_24h=float(t.get("volume_24h_quote", 0) or 0),
                    change_pct=float(t.get("change_percentage", 0) or 0),
                    funding_rate=float(t.get("funding_rate", 0) or 0),
                    mark_price=float(t.get("mark_price", 0) or 0),
                )
        self._cache = tickers

    def get_prices(self) -> Dict[str, float]:
        return {s: t.price for s, t in self._cache.items()}

    def get_changes(self) -> Dict[str, float]:
        return {s: t.change_pct for s, t in self._cache.items()}

    def get_funding_rates(self) -> Dict[str, float]:
        return {s: t.funding_rate for s, t in self._cache.items()}

    def get_mark_prices(self) -> Dict[str, float]:
        return {s: t.mark_price for s, t in self._cache.items()}

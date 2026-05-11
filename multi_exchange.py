#!/usr/bin/env python3
"""
多交易所实时价格聚合器
聚合 Binance / Bybit / OKX / Gate.io 合约价格，输出加权共识价
解决单交易所信号偏差问题
"""

import asyncio, aiohttp, time, json
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from collections import defaultdict

_log = logging.getLogger(__name__)


@dataclass
class ExchangePrice:
    exchange: str
    symbol: str
    price: float
    funding_rate: float = 0.0
    volume_24h: float = 0.0
    change_24h: float = 0.0
    spread: float = 0.0  # bid-ask spread
    timestamp: float = field(default_factory=time.time)

    @property
    def weight(self) -> float:
        """交易所权重 = 成交量归一化"""
        return max(0.1, min(1.0, self.volume_24h / 1e8))


class MultiExchangeFeed:
    """多交易所实时价格聚合"""

    # 各交易所合约API
    ENDPOINTS = {
        "binance": "https://fapi.binance.com/fapi/v1",
        "bybit": "https://api.bybit.com/v5/market",
        "okx": "https://www.okx.com/api/v5/public",
        "gate": "https://api.gateio.ws/api/v4/futures/usdt",
    }

    # 币种映射：统一名 → 各交易所合约名
    SYMBOL_MAP = {
        "binance": {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "DOGE": "DOGEUSDT",
                     "XRP": "XRPUSDT", "SUI": "SUIUSDT", "PEPE": "1000PEPEUSDT",
                     "TON": "TONUSDT", "ARB": "ARBUSDT"},
        "bybit": {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "DOGE": "DOGEUSDT",
                   "XRP": "XRPUSDT", "SUI": "SUIUSDT", "PEPE": "PEPEUSDT",
                   "TON": "TONUSDT", "ARB": "ARBUSDT"},
        "okx": {"BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP", "SOL": "SOL-USDT-SWAP",
                 "DOGE": "DOGE-USDT-SWAP", "XRP": "XRP-USDT-SWAP", "SUI": "SUI-USDT-SWAP",
                 "PEPE": "PEPE-USDT-SWAP", "TON": "TON-USDT-SWAP", "ARB": "ARB-USDT-SWAP"},
        "gate": {"BTC": "BTC_USDT", "ETH": "ETH_USDT", "SOL": "SOL_USDT", "DOGE": "DOGE_USDT",
                  "XRP": "XRP_USDT", "SUI": "SUI_USDT", "PEPE": "PEPE_USDT",
                  "TON": "TON_USDT", "ARB": "ARB_USDT"},
    }

    BASE_SYMBOLS = ["BTC", "ETH", "SOL", "DOGE", "XRP", "SUI", "PEPE", "TON", "ARB"]

    def __init__(self):
        self._prices: Dict[str, Dict[str, ExchangePrice]] = defaultdict(dict)
        self._consensus: Dict[str, float] = {}
        self._divergence: Dict[str, float] = {}  # 交易所间价差(%), 高 = 套利机会/方向不确定
        self._funding_avg: Dict[str, float] = {}
        self._volume_total: Dict[str, float] = {}
        self._last_update: float = 0

    async def _fetch_binance(self, session) -> dict:
        """Binance ticker"""
        try:
            async with session.get(f"{self.ENDPOINTS['binance']}/ticker/bookTicker",
                                   timeout=aiohttp.ClientTimeout(total=5)) as r:
                data = await r.json()
            result = {}
            for base, sym in self.SYMBOL_MAP["binance"].items():
                for item in data:
                    if item["symbol"] == sym:
                        bid = float(item["bidPrice"])
                        ask = float(item["askPrice"])
                        result[base] = {
                            "price": (bid + ask) / 2,
                            "spread": (ask - bid) / ((bid + ask) / 2) * 100,
                        }
                        break
            return result
        except Exception as e:
            _log.debug("multi_exchange binance: %s", e)
            return {}

    async def _fetch_bybit(self, session) -> dict:
        """Bybit ticker"""
        try:
            async with session.get(f"{self.ENDPOINTS['bybit']}/tickers?category=linear",
                                   timeout=aiohttp.ClientTimeout(total=5)) as r:
                data = await r.json()
            result = {}
            items = data.get("result", {}).get("list", [])
            for base, sym in self.SYMBOL_MAP["bybit"].items():
                for item in items:
                    if item["symbol"] == sym:
                        result[base] = {
                            "price": float(item["lastPrice"]),
                            "funding": float(item.get("fundingRate", "0")) * 100,
                            "volume": float(item.get("volume24h", "0")),
                            "change": float(item.get("price24hPcnt", "0")) * 100,
                        }
                        break
            return result
        except Exception as e:
            _log.debug("multi_exchange bybit: %s", e)
            return {}

    async def _fetch_okx(self, session) -> dict:
        """OKX ticker"""
        try:
            async with session.get(f"{self.ENDPOINTS['okx']}/mark-price?instType=SWAP",
                                   timeout=aiohttp.ClientTimeout(total=5)) as r:
                data = await r.json()
            result = {}
            items = data.get("data", [])
            for base, sym in self.SYMBOL_MAP["okx"].items():
                for item in items:
                    if item["instId"] == sym:
                        result[base] = {"price": float(item["markPrice"])}
                        break
            return result
        except Exception as e:
            _log.debug("multi_exchange okx: %s", e)
            return {}

    async def _fetch_gate(self, session) -> dict:
        """Gate.io ticker"""
        try:
            async with session.get(f"{self.ENDPOINTS['gate']}/tickers",
                                   timeout=aiohttp.ClientTimeout(total=5)) as r:
                data = await r.json()
            result = {}
            for base, sym in self.SYMBOL_MAP["gate"].items():
                for item in data:
                    if item.get("contract") == sym:
                        result[base] = {
                            "price": float(item["last"]),
                            "funding": float(item.get("funding_rate", "0")) * 100,
                            "volume": float(item.get("volume_24h_quote", "0")),
                            "change": float(item.get("change_percentage", "0")),
                        }
                        break
            return result
        except Exception as e:
            _log.debug("multi_exchange gate: %s", e)
            return {}

    async def refresh(self) -> None:
        """刷新所有交易所数据"""
        async with aiohttp.ClientSession() as session:
            tasks = [
                self._fetch_binance(session),
                self._fetch_bybit(session),
                self._fetch_okx(session),
                self._fetch_gate(session),
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        exchanges = ["binance", "bybit", "okx", "gate"]
        all_data = {}
        for ex, data in zip(exchanges, results):
            if isinstance(data, Exception):
                continue
            all_data[ex] = data

        # 聚合
        for base in self.BASE_SYMBOLS:
            prices = []
            volumes = []
            fundings = []

            for ex in exchanges:
                d = all_data.get(ex, {}).get(base)
                if not d or not d.get("price"):
                    continue
                px = d["price"]
                vol = d.get("volume", 0)
                fr = d.get("funding", 0)
                spread = d.get("spread", 0)

                prices.append(px)
                volumes.append(vol)
                fundings.append(fr)

                self._prices[base][ex] = ExchangePrice(
                    exchange=ex, symbol=base, price=px,
                    funding_rate=fr, volume_24h=vol,
                    change_24h=d.get("change", 0), spread=spread,
                )

            if prices:
                # 加权均价（按成交量）
                total_vol = sum(volumes) or 1
                self._consensus[base] = sum(p * v for p, v in zip(prices, volumes)) / total_vol
                self._volume_total[base] = total_vol
                self._funding_avg[base] = sum(fundings) / len(fundings) if fundings else 0

                # 交易所间最大价差
                max_px = max(prices)
                min_px = min(prices)
                self._divergence[base] = (max_px - min_px) / ((max_px + min_px) / 2) * 100

        self._last_update = time.time()

    # ── 查询接口 ──

    def consensus_price(self, symbol: str) -> float:
        """多交易所加权共识价"""
        base = symbol.split("/")[0]
        return self._consensus.get(base, 0)

    def exchange_divergence(self, symbol: str) -> float:
        """交易所间价差(%), >0.5% = 套利机会/要小心方向"""
        base = symbol.split("/")[0]
        return self._divergence.get(base, 0)

    def avg_funding(self, symbol: str) -> float:
        """多交易所平均资金费率"""
        base = symbol.split("/")[0]
        return self._funding_avg.get(base, 0)

    def prices_all_exchanges(self, symbol: str) -> dict:
        """获取某币种所有交易所的报价"""
        base = symbol.split("/")[0]
        return {ex: ep.price for ex, ep in self._prices.get(base, {}).items()}

    def direction_signal(self, symbol: str) -> dict:
        """
        多交易所方向信号
        综合各交易所价差、费率、成交量判断方向
        """
        base = symbol.split("/")[0]
        prices = self.prices_all_exchanges(symbol)
        divergence = self.exchange_divergence(symbol)
        funding = self.avg_funding(symbol)
        consensus = self.consensus_price(symbol)

        # Gate.io价格偏离共识价 → 短期方向信号
        gate_px = self._prices.get(base, {}).get("gate")
        gate_premium = 0
        if gate_px and consensus:
            gate_premium = (gate_px.price - consensus) / consensus * 100

        return {
            "consensus": consensus,
            "divergence": divergence,
            "funding_avg": funding,
            "gate_premium": gate_premium,  # Gate相对共识溢价%
            "exchanges": len(prices),
            # 方向信号：Gate溢价+正费率 → 做多压力；折价+负费率 → 做空压力
            "bias": "long" if gate_premium > 0.1 and funding > 0 else
                    "short" if gate_premium < -0.1 and funding < 0 else "neutral",
            "confidence": min(abs(gate_premium) * 10 + abs(funding) * 50, 80),
        }


# 全局单例
_multi_feed: MultiExchangeFeed = None

def get_multi_feed() -> MultiExchangeFeed:
    return _multi_feed

async def init_multi_feed() -> MultiExchangeFeed:
    global _multi_feed
    _multi_feed = MultiExchangeFeed()
    await _multi_feed.refresh()
    return _multi_feed

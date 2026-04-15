"""
币安全市场 1s 快照流 !ticker@arr（USDⓈ-M 永续）。
单连接覆盖全市场 ticker，用于跨所领先信号（不替代 Gate 执行与深度）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, Dict, List, Optional

import aiohttp

from src.utils.logger import log

DEFAULT_FSTREAM_TICKER_ARR = "wss://fstream.binance.com/ws/!ticker@arr"

TickBatchHandler = Callable[[List[Dict[str, Any]]], Awaitable[None]]


def _parse_message(raw: str) -> List[Dict[str, Any]]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, list):
            return [x for x in inner if isinstance(x, dict)]
        if isinstance(inner, dict):
            return [inner]
    return []


class BinanceOracleTickerStream:
    def __init__(
        self,
        url: str = DEFAULT_FSTREAM_TICKER_ARR,
        on_batch: Optional[TickBatchHandler] = None,
    ) -> None:
        self.url = url
        self.on_batch = on_batch
        self._running = False

    def stop(self) -> None:
        self._running = False

    async def run_forever(self) -> None:
        self._running = True
        delay = 1.0
        connector = aiohttp.TCPConnector(ssl=True)
        while self._running:
            try:
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.ws_connect(self.url, heartbeat=60.0) as ws:
                        delay = 1.0
                        log.info(f"[BinanceOracle] connected {self.url}")
                        async for msg in ws:
                            if not self._running:
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                batch = _parse_message(msg.data)
                                if batch and self.on_batch:
                                    await self.on_batch(batch)
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSE,
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"[BinanceOracle] ws error: {e}; reconnect in {delay:.1f}s")
            if not self._running:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 1.8, 60.0)

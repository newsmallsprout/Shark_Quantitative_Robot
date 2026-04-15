"""
Gate 热门合约宇宙：REST 全市场 USDT 永续 tickers → 成交额×波动热度排序 →
定时写回 strategy.symbols 并 subscribe_market_data（专抓 RAVE 类高波动币）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Tuple

import aiohttp

from src.ai import l2_command
from src.core.config_manager import config_manager
from src.utils.logger import log

_snapshot: Dict[str, Any] = {
    "enabled": False,
    "last_refresh_ts": 0.0,
    "symbols": [],
    "stats": {},
    "error": "",
}

_l2_conflict_logged = False


def get_gate_hot_universe_payload() -> Dict[str, Any]:
    return dict(_snapshot)


async def _fetch_rank_apply(
    exchange: Any, session: aiohttp.ClientSession
) -> Tuple[bool, str]:
    """
    拉取 ticker、排序、写 config + 订阅。返回 (是否成功更新 symbols, 原因码)。
    空榜不覆盖现有 strategy.symbols。
    """
    global _l2_conflict_logged
    hu = config_manager.get_config().gate_hot_universe
    if not hu.enabled:
        return False, "disabled"
    cfg = config_manager.get_config()
    if bool(cfg.beta_neutral_hf.enabled) and (
        "beta_neutral_hf" in list(cfg.strategy.active_strategies or [])
    ):
        return False, "beta_neutral_owned_symbols"

    if not _l2_conflict_logged and config_manager.get_config().l2_command.enabled:
        _l2_conflict_logged = True
        log.warning(
            "[HotUniverse] gate_hot_universe 与 l2_command 同时开启时会争写 strategy.symbols，建议只开其一"
        )

    rows = await l2_command.fetch_gate_usdt_tickers(session)
    syms, stats = l2_command.rank_universe_symbols(
        rows,
        min_quote_vol=float(hu.min_quote_vol_24h),
        top_n=int(hu.top_n),
        cap=int(hu.symbols_cap),
        anchors=list(hu.anchor_symbols or []),
        change_pct_divisor=float(hu.change_pct_divisor),
        change_multiplier_cap=float(hu.change_score_cap),
        funding_scale=float(hu.funding_score_scale),
        funding_cap_mult=float(hu.funding_score_cap_mult),
    )
    if not syms:
        _snapshot["error"] = "empty symbol list after rank"
        return False, "empty"

    config_manager.config.strategy.symbols = list(syms)
    if hasattr(exchange, "subscribe_market_data"):
        await exchange.subscribe_market_data(list(syms))

    _snapshot["enabled"] = True
    _snapshot["last_refresh_ts"] = time.time()
    _snapshot["symbols"] = list(syms)
    _snapshot["stats"] = stats
    _snapshot["error"] = ""
    return True, "ok"


async def refresh_gate_hot_universe_once(exchange: Any, session: aiohttp.ClientSession) -> None:
    """供测试或外部在同 session 下手动触发一轮。"""
    hu = config_manager.get_config().gate_hot_universe
    if not hu.enabled:
        return
    ok, reason = await _fetch_rank_apply(exchange, session)
    if not ok and reason == "empty":
        log.warning("[HotUniverse] refresh: empty ranked list, keeping current symbols")
    elif ok:
        syms = list(_snapshot.get("symbols") or [])
        log.info(f"[HotUniverse] refresh → {len(syms)} symbols: {syms[:8]}…")


async def run_gate_hot_universe_loop(exchange: Any) -> None:
    log.info("[HotUniverse] task started")
    connector = aiohttp.TCPConnector(ssl=True)
    async with aiohttp.ClientSession(connector=connector) as session:
        first = True
        while getattr(exchange, "running", True):
            hu = config_manager.get_config().gate_hot_universe
            if not hu.enabled:
                _snapshot["enabled"] = False
                await asyncio.sleep(2.0)
                continue

            try:
                ok, reason = await _fetch_rank_apply(exchange, session)
                if ok:
                    stats = _snapshot.get("stats") or {}
                    preview = stats.get("top_preview") or []
                    syms = list(_snapshot.get("symbols") or [])
                    tag = "startup" if first else "periodic"
                    log.info(
                        f"[HotUniverse] {tag} {len(syms)} symbols | "
                        f"candidates={stats.get('candidates', 0)} | top={preview[:5]}"
                    )
                elif reason == "empty":
                    log.warning("[HotUniverse] rank empty, keeping prior symbols")
            except Exception as e:
                _snapshot["error"] = str(e)[:400]
                log.error(f"[HotUniverse] refresh failed: {e}")

            first = False
            await asyncio.sleep(max(5.0, float(hu.refresh_sec)))

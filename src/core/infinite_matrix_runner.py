"""
THE INFINITE MATRIX — 单腿逐仓：净利 > 微尘即市价平；平仓后同品种同向市价重载。
AI=震荡(STABLE/OSCILLATING/CHAOTIC)：高频刷单续杯。
AI=单边(TRENDING_UP/DOWN)：顺势腿挂 ROE 追踪、禁微利秒平；逆势腿禁续杯；亏损腿不主动平。

纸面引擎仍为「每 symbol 一条净仓」；同品种多空双开需 Hedge 仓位键（后续扩展）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

from src.ai.analyzer import ai_context
from src.core.config_manager import config_manager
from src.core.paper_engine import paper_engine
from src.utils.logger import log

_TRENDING_REGIMES = frozenset({"TRENDING_UP", "TRENDING_DOWN"})


def _matrix_or_regime(symbol: str) -> str:
    """优先 LLM 的 matrix_regime（TRENDING_*）；否则回落到 coarse regime 字符串。"""
    d = ai_context.get(symbol) or {}
    mr = str(d.get("matrix_regime") or d.get("trend_regime") or "").strip().upper().replace(" ", "_")
    if mr in _TRENDING_REGIMES:
        return mr
    r = d.get("regime")
    if hasattr(r, "name"):
        return str(getattr(r, "name", "")).strip().upper()
    return str(r or "STABLE").strip().upper().replace(" ", "_")


def _is_trending(symbol: str) -> bool:
    return _matrix_or_regime(symbol) in _TRENDING_REGIMES


def _position_side(pos: Dict[str, Any]) -> str:
    return str(pos.get("side", "long")).lower()


def _trend_aligned(symbol: str, pos: Dict[str, Any]) -> bool:
    r = _matrix_or_regime(symbol)
    ps = _position_side(pos)
    if r == "TRENDING_UP" and ps == "long":
        return True
    if r == "TRENDING_DOWN" and ps == "short":
        return True
    return False


def _trend_counter(symbol: str, pos: Dict[str, Any]) -> bool:
    r = _matrix_or_regime(symbol)
    ps = _position_side(pos)
    if r == "TRENDING_UP" and ps == "short":
        return True
    if r == "TRENDING_DOWN" and ps == "long":
        return True
    return False


def _gross_unrealized(pos: Dict[str, Any], symbol: str, last: float) -> float:
    entry = float(pos.get("entry_price", 0) or 0)
    contracts = float(pos.get("size", 0) or 0)
    if entry <= 0 or contracts <= 0 or last <= 0:
        return 0.0
    cs = float(paper_engine._position_contract_size(pos, symbol))
    if _position_side(pos) == "long":
        return contracts * cs * (last - entry)
    return contracts * cs * (entry - last)


def _dummy_tp_sl_ctx(side_open: str, entry_px: float) -> Dict[str, float]:
    """满足纸面 require_entry_tp_sl 时的占位宽 TP/SL（策略不依赖其成交）。"""
    ep = float(entry_px)
    if str(side_open).lower() == "buy":
        return {"take_profit_limit_price": ep * 1.5, "stop_loss_limit_price": ep * 0.5}
    return {"take_profit_limit_price": ep * 0.5, "stop_loss_limit_price": ep * 1.5}


class InfiniteMatrixRunner:
    def __init__(self) -> None:
        self._locks: Dict[str, asyncio.Lock] = {}
        self._last_reload_ts: Dict[str, float] = {}

    def _lock(self, symbol: str) -> asyncio.Lock:
        if symbol not in self._locks:
            self._locks[symbol] = asyncio.Lock()
        return self._locks[symbol]

    def _reload_ok(self, symbol: str, min_interval: float) -> bool:
        now = time.time()
        t0 = float(self._last_reload_ts.get(symbol, 0.0) or 0.0)
        if now - t0 < max(0.0, float(min_interval)):
            return False
        self._last_reload_ts[symbol] = now
        return True

    async def on_ticker(self, exchange: Any, symbol: str, last: float) -> None:
        cfg = config_manager.get_config().infinite_matrix
        if not cfg.enabled:
            return
        if last <= 0:
            return

        async with self._lock(symbol):
            pos = paper_engine.positions.get(symbol)
            if not pos or float(pos.get("size", 0) or 0) <= 0:
                return
            ect0 = dict(pos.get("entry_context") or {})
            if not ect0.get("infinite_matrix_ultra"):
                return

            trending = _is_trending(symbol)
            aligned = _trend_aligned(symbol, pos)
            counter = _trend_counter(symbol, pos)
            gross = _gross_unrealized(pos, symbol, last)

            if trending and aligned and gross > 0:
                paper_engine.attach_high_conviction_trailing(
                    symbol,
                    float(cfg.trend_trailing_activation_roe),
                    float(cfg.trend_trailing_callback_roe),
                    extra_ctx={"infinite_matrix_trend_ride": True},
                )
                return

            if not trending:
                paper_engine.clear_high_conviction_trailing(symbol)

            if trending and aligned:
                paper_engine.clear_high_conviction_trailing(symbol)
                return

            net_est = paper_engine.estimate_flat_net_pnl(symbol, pos, last)
            eps = max(1e-6, float(cfg.min_net_close_usdt))
            if net_est <= eps:
                return

            if trending and gross < 0:
                return

            sz = float(pos["size"])
            lev = int(pos.get("leverage", 10) or 10)
            ps = _position_side(pos)
            close_side = "sell" if ps == "long" else "buy"
            snap_ect = dict(pos.get("entry_context") or {})

            res = paper_engine.execute_order(
                symbol,
                close_side,
                sz,
                None,
                reduce_only=True,
                leverage=max(1, lev),
                margin_mode="isolated",
                berserker=False,
                post_only=False,
                entry_context={**snap_ect, "exit_reason": "infinite_matrix_micro_take"},
                exit_reason="infinite_matrix_micro_take",
            )
            st = str((res or {}).get("status", "") or "").lower()
            if st in ("rejected", "error", "failed"):
                log.debug(f"[InfiniteMatrix] close skip {symbol} res={res!r}")
                return

            pos2 = paper_engine.positions.get(symbol)
            if pos2 and float(pos2.get("size", 0) or 0) > 1e-12:
                return

            if not bool(cfg.reload_enabled):
                return
            if trending and counter:
                log.info(f"[InfiniteMatrix] no reload (counter-trend) {symbol} regime={_matrix_or_regime(symbol)}")
                return

            if not self._reload_ok(symbol, float(cfg.reload_debounce_sec or 0.05)):
                return

            open_side = "buy" if ps == "long" else "sell"
            ctx: Dict[str, Any] = {
                "infinite_matrix_ultra": True,
                "infinite_matrix_reload": True,
                "effective_leverage": float(lev),
            }
            if bool(cfg.inject_dummy_tp_sl_for_paper):
                ctx.update(_dummy_tp_sl_ctx(open_side, last))

            r2 = paper_engine.execute_order(
                symbol,
                open_side,
                sz,
                None,
                reduce_only=False,
                leverage=max(1, lev),
                margin_mode="isolated",
                berserker=False,
                post_only=False,
                entry_context=ctx,
            )
            st2 = str((r2 or {}).get("status", "") or "").lower()
            if st2 in ("rejected", "error", "failed"):
                log.warning(f"[InfiniteMatrix] reload failed {symbol} {r2!r}")
            else:
                log.info(
                    f"[InfiniteMatrix] micro_take+reload {symbol} side={ps} net_est={net_est:.4f} "
                    f"trending={trending} counter={counter}"
                )


infinite_matrix_runner = InfiniteMatrixRunner()

__all__ = ["infinite_matrix_runner", "InfiniteMatrixRunner"]

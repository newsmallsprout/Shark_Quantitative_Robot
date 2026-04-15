"""
币安天眼 (!ticker@arr) → 符号映射 → Gate 点差/Hurdle 闸 → 信号入队（Taker IOC + 纸面 Maker 止盈）。

实盘：Gateway 对市价单使用 tif=ioc；entry_context.client_oid → REST text 字段。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

import aiohttp

from src.core.events import SignalEvent
from src.core.config_manager import config_manager
from src.core.paper_engine import paper_engine
from src.core.symbol_mapper import bn_usdm_to_gate
from src.exchange.binance_oracle_ws import (
    DEFAULT_FSTREAM_TICKER_ARR,
    BinanceOracleTickerStream,
)
from src.utils.logger import log

GATE_CONTRACTS_URL = "https://api.gateio.ws/api/v4/futures/usdt/contracts"

_leadlag_snapshot: Dict[str, Any] = {
    "enabled": False,
    "last_batch_ts": 0.0,
    "last_signal_ts": 0.0,
    "last_signal": None,
    "tickers_seen": 0,
    "error": "",
}


def get_binance_leadlag_payload() -> Dict[str, Any]:
    return dict(_leadlag_snapshot)


def _hurdle_gate(
    bid: float, ask: float, taker: float, maker: float
) -> float:
    spread_frac = 0.0
    if bid > 0 and ask > 0 and ask >= bid:
        mid = 0.5 * (bid + ask)
        if mid > 0:
            spread_frac = (ask - bid) / mid
    return max(0.0, float(taker) + float(maker) + spread_frac)


class BinanceLeadLagOrchestrator:
    def __init__(self, exchange: Any, strategy_engine: Any) -> None:
        self.exchange = exchange
        self.engine = strategy_engine
        self._price_hist: Dict[str, Deque[Tuple[float, float]]] = {}
        self._cooldown: Dict[str, float] = {}
        self._global_signal_times: Deque[float] = deque()
        self._gate_contracts: Set[str] = set()
        self._stream: Optional[BinanceOracleTickerStream] = None
        self._contracts_ready = asyncio.Event()

    def _trim_hist(self, bn_sym: str, now: float, horizon: float) -> None:
        dq = self._price_hist.setdefault(bn_sym, deque())
        while dq and now - dq[0][0] > horizon:
            dq.popleft()

    def _move_over_horizon(
        self, bn_sym: str, price: float, now: float, horizon: float
    ) -> Optional[float]:
        self._trim_hist(bn_sym, now, horizon + 0.5)
        dq = self._price_hist.setdefault(bn_sym, deque())
        dq.append((now, price))
        ref = None
        for t, p in dq:
            if now - t >= horizon * 0.85:
                ref = p
        if ref is None or ref <= 0:
            return None
        return (price - ref) / ref

    async def _ensure_contracts(self, session: aiohttp.ClientSession) -> None:
        if self._gate_contracts:
            self._contracts_ready.set()
            return
        try:
            async with session.get(GATE_CONTRACTS_URL, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    _leadlag_snapshot["error"] = f"contracts http {resp.status}"
                    return
                rows = await resp.json()
            for r in rows or []:
                if isinstance(r, dict) and r.get("name"):
                    self._gate_contracts.add(str(r["name"]).upper())
            log.info(f"[LeadLag] cached {len(self._gate_contracts)} Gate USDT contracts")
        except Exception as e:
            _leadlag_snapshot["error"] = str(e)[:200]
            log.error(f"[LeadLag] contracts fetch failed: {e}")
        finally:
            self._contracts_ready.set()

    def _gate_has_contract(self, gate_sym: str) -> bool:
        if not self._gate_contracts:
            return True
        c = gate_sym.replace("/", "_").upper()
        return c in self._gate_contracts

    async def _on_batch(self, batch: List[Dict[str, Any]]) -> None:
        cfg = config_manager.get_config().binance_leadlag
        if not cfg.enabled:
            _leadlag_snapshot["enabled"] = False
            return

        await self._contracts_ready.wait()
        now = time.time()
        _leadlag_snapshot["enabled"] = True
        _leadlag_snapshot["last_batch_ts"] = now
        _leadlag_snapshot["tickers_seen"] = int(_leadlag_snapshot.get("tickers_seen", 0) or 0) + len(
            batch
        )

        horizon = float(cfg.move_lookback_sec)
        min_move = float(cfg.min_move_pct) / 100.0
        min_qv = float(cfg.min_quote_vol_24h_bn)
        max_h = float(cfg.max_hurdle_frac)
        taker = float(paper_engine.taker_fee)
        maker = float(paper_engine.maker_fee)
        cd = float(cfg.signal_cooldown_sec)
        max_per_min = int(cfg.max_signals_per_minute)

        for t in batch:
            if not isinstance(t, dict):
                continue
            bn = str(t.get("s") or "").upper()
            if not bn.endswith("USDT"):
                continue
            try:
                last = float(t.get("c") or 0)
                qv = float(t.get("q") or 0)
            except (TypeError, ValueError):
                continue
            if last <= 0 or qv < min_qv:
                continue

            move = self._move_over_horizon(bn, last, now, horizon)
            if move is None or abs(move) < min_move:
                continue

            side = "buy" if move > 0 else "sell"
            if side == "sell" and not cfg.enable_short_on_dump:
                continue

            gate_sym = bn_usdm_to_gate(bn)
            if not gate_sym or not self._gate_has_contract(gate_sym):
                continue

            if now - self._cooldown.get(gate_sym, 0) < cd:
                continue

            top = getattr(self.exchange, "latest_book_top", {}).get(gate_sym)
            bid = float((top or {}).get("bid") or 0)
            ask = float((top or {}).get("ask") or 0)
            if bid <= 0 or ask <= 0:
                if cfg.auto_subscribe_gate_ob and hasattr(
                    self.exchange, "subscribe_market_data"
                ):
                    try:
                        await self.exchange.subscribe_market_data([gate_sym])
                    except Exception as e:
                        log.debug(f"[LeadLag] subscribe {gate_sym}: {e}")
                continue

            h = _hurdle_gate(bid, ask, taker, maker)
            if h > max_h:
                continue

            # throttle global fire rate
            self._global_signal_times.append(now)
            while self._global_signal_times and now - self._global_signal_times[0] > 60.0:
                self._global_signal_times.popleft()
            if len(self._global_signal_times) > max_per_min:
                continue

            ref_px = ask if side == "buy" else bid
            notional = float(cfg.trade_notional_usd)
            lev = int(cfg.leverage)
            raw_amt = notional / ref_px
            amt = max(1.0, raw_amt)
            if getattr(self.exchange, "contract_specs_cache", None):
                spec = self.exchange.contract_specs_cache.get(gate_sym) or {}
                try:
                    qsz = float(spec.get("quanto_multiplier") or spec.get("order_size_min") or 0)
                except (TypeError, ValueError):
                    qsz = 0.0
                if qsz > 0:
                    amt = max(qsz, round(raw_amt / qsz) * qsz)

            prefix = str(cfg.client_oid_prefix or "SNIPER_LL")[:12]
            oid = f"{prefix}_{uuid.uuid4().hex[:10]}"

            ect: Dict[str, Any] = {
                "position_silo": "SNIPER_LEADLAG",
                "client_oid": oid[:28],
                "leadlag_managed": True,
                "leadlag_bracket_protocol": True,
                "leadlag_target_net_frac": float(cfg.bracket_target_net_frac),
                "leadlag_min_tp_bps": float(cfg.bracket_min_tp_bps),
                "leadlag_initial_sl_bps": float(cfg.initial_sl_bps),
                "bn_symbol": bn,
                "oracle_move_pct": round(move * 100.0, 4),
                "oracle_hurdle_frac": round(h, 6),
                "silo_tag": str(cfg.silo_tag or "SNIPER_LEADLAG_001"),
            }

            sig = SignalEvent(
                strategy_name="BinanceLeadLag",
                symbol=gate_sym,
                side=side,
                order_type="market",
                price=float(ref_px),
                amount=float(amt),
                leverage=lev,
                reduce_only=False,
                berserker=False,
                post_only=False,
                margin_mode="cross",
                entry_context=ect,
            )
            await self.engine.signal_queue.put(sig)
            self._cooldown[gate_sym] = now
            _leadlag_snapshot["last_signal_ts"] = now
            _leadlag_snapshot["last_signal"] = {
                "gate_symbol": gate_sym,
                "bn_symbol": bn,
                "side": side,
                "move_pct": round(move * 100.0, 3),
                "hurdle_bps": round(h * 10000.0, 2),
                "client_oid": ect["client_oid"],
            }
            log.warning(
                f"[LeadLag] SIGNAL {side} {gate_sym} bn={bn} move={move*100:.2f}% "
                f"hurdle={h*10000:.1f}bps oid={ect['client_oid']}"
            )

    async def run(self) -> None:
        connector = aiohttp.TCPConnector(ssl=True)
        async with aiohttp.ClientSession(connector=connector) as session:
            await self._ensure_contracts(session)

        while getattr(self.exchange, "running", True):
            cfg = config_manager.get_config().binance_leadlag
            if not cfg.enabled:
                _leadlag_snapshot["enabled"] = False
                await asyncio.sleep(2.0)
                continue

            _u = str(getattr(cfg, "ws_url", "") or "").strip()
            self._stream = BinanceOracleTickerStream(
                url=_u if _u else DEFAULT_FSTREAM_TICKER_ARR,
                on_batch=self._on_batch,
            )
            await self._stream.run_forever()


async def run_binance_leadlag_loop(exchange: Any, strategy_engine: Any) -> None:
    orch = BinanceLeadLagOrchestrator(exchange, strategy_engine)
    await orch.run()

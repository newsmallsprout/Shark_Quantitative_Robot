import asyncio
import json
import math
import time
from collections import Counter
from typing import Any, Dict, List, Optional

import aiohttp

from src.utils.logger import log
from src.ai.llm_factory import LLMFactory
from src.core.config_manager import config_manager
from src.ai.regime import MarketRegime
from src.core.ipc import ZMQPublisher
from src.runtime_obf import IPC_TOPIC_AI_SCORE, IPC_TOPIC_L1_TUNING, IPC_TOPIC_L2_SYMBOLS
from src.ai import l2_command


class AIContext:
    """
    Shared memory for AI Analysis results.
    Acts as the signal delivery mechanism between the asynchronous MarketAnalyzer
    and the synchronous/fast-path Strategy Engine.
    """

    def __init__(self):
        self._latest_analysis: Dict[str, Dict[str, Any]] = {}

    def update(self, symbol: str, analysis: Dict[str, Any]):
        """Update the latest analysis for a symbol."""
        self._latest_analysis[symbol] = analysis

    def get(self, symbol: str) -> Dict[str, Any]:
        """Retrieve the latest analysis. Non-blocking."""
        return self._latest_analysis.get(
            symbol,
            {
                "regime": MarketRegime.STABLE,
                "matrix_regime": "STABLE",
                "score": 50.0,
                "suggested_leverage_cap": 100,
                "tp_atr_multiplier": 2.0,
                "sl_atr_multiplier": 1.8,
                "reason": "Waiting for initial AI analysis...",
            },
        )


# Global singleton for shared memory
ai_context = AIContext()


class MarketAnalyzer:
    """
    Asynchronous background worker that periodically fetches market data,
    queries the configured LLM, and pushes structured results via ZMQ.
    Designed to be run in a fully isolated process.
    """

    def __init__(self, exchange=None, zmq_port: int = 5555):
        self.exchange = exchange
        self.running = False
        self.llm = None
        self.zmq_port = zmq_port
        self.publisher = ZMQPublisher(port=self.zmq_port)
        self._cycle_regime_counts: Counter = Counter()
        self._cycle_scores: List[float] = []
        self._last_l2_ts = 0.0
        self._l2_first_pending = True
        self._last_funding_rate: Dict[str, float] = {}
        self._init_llm()

    def _init_llm(self):
        self.llm = LLMFactory.create_from_darwin_config()
        log.info(f"MarketAnalyzer using LLM class: {type(self.llm).__name__}")

    async def start(self):
        self.running = True
        self.publisher.start()
        log.info("Market Analyzer (AI Worker) Started in isolated process")

        while self.running:
            try:
                self._cycle_regime_counts = Counter()
                self._cycle_scores = []
                await self.analyze_markets()
                await self._maybe_run_l2_cycle()
            except asyncio.CancelledError:
                log.info("Market Analyzer cancelled")
                break
            except Exception as e:
                log.error(f"Market Analyzer loop error: {e}")

            for _ in range(60):
                if not self.running:
                    break
                await asyncio.sleep(1)

    async def stop(self):
        self.running = False
        self.publisher.close()
        log.info("Market Analyzer Stopping...")

    async def _maybe_run_l2_cycle(self) -> None:
        l2 = config_manager.get_config().l2_command
        if not l2.enabled:
            return
        now = time.time()
        due = self._l2_first_pending or (now - self._last_l2_ts >= float(l2.interval_sec))
        if not due:
            return
        await self.run_l2_command_cycle()
        self._last_l2_ts = now
        self._l2_first_pending = False

    async def run_l2_command_cycle(self) -> None:
        """全市场扫描 + 可选写回 symbols + 经 ZMQ 发布 L2 品种表与 L1 调参载荷。"""
        l2 = config_manager.get_config().l2_command
        l1 = config_manager.get_config().l1_fast_loop
        gc = config_manager.get_config()

        universe_stats: Dict[str, Any] = {}
        symbols_out: List[str] = list(gc.strategy.symbols)

        try:
            async with aiohttp.ClientSession() as session:
                rows = await l2_command.fetch_gate_usdt_tickers(session)
            symbols_out, universe_stats = l2_command.rank_universe_symbols(
                rows,
                min_quote_vol=float(l2.min_quote_volume_24h),
                top_n=int(l2.universe_top_n),
                cap=int(l2.symbols_cap),
                anchors=list(l2.anchor_symbols),
            )
            log.info(
                f"[L2] Universe ranked symbols={len(symbols_out)} "
                f"candidates={universe_stats.get('candidates', 0)}"
            )
        except Exception as e:
            log.error(f"[L2] Universe fetch/rank failed: {e}")

        if l2.publish_symbols_zmq and symbols_out:
            self.publisher.publish(
                IPC_TOPIC_L2_SYMBOLS,
                {"symbols": symbols_out, "source": "l2_command"},
            )

        if l2.persist_symbols_to_yaml and symbols_out:
            try:
                config_manager.update_strategy_config(symbols=symbols_out)
                log.info("[L2] strategy.symbols persisted to settings.yaml")
            except Exception as e:
                log.error(f"[L2] Failed to persist symbols: {e}")

        rules: Dict[str, Any] = {}
        if l2.rules_l1_tuning:
            rules = l2_command.rules_l1_tuning(
                self._cycle_regime_counts, self._cycle_scores, l1
            )

        llm_part: Dict[str, Any] | None = None
        if l2.use_llm_for_l1_tuning:
            prompt = l2_command.build_l1_tuning_prompt(
                self._cycle_regime_counts,
                self._cycle_scores,
                universe_stats,
                l1,
            )
            llm_part = await self._call_llm_with_retry(prompt, max_retries=2)

        merged = l2_command.merge_l1_tuning(rules, llm_part)
        tuning_send: Dict[str, Any] = {}
        for k, v in merged.items():
            if k == "halt_trading":
                tuning_send[k] = bool(v)
            elif v is not None:
                tuning_send[k] = v

        if tuning_send:
            self.publisher.publish(IPC_TOPIC_L1_TUNING, tuning_send)
            log.info(f"[L2] Published {IPC_TOPIC_L1_TUNING} {tuning_send}")

    @staticmethod
    def _gate_contract(symbol: str) -> str:
        return str(symbol).replace("/", "_").replace(":", "_")

    async def _fetch_public_json(self, session: aiohttp.ClientSession, url: str) -> Any:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise ConnectionError(f"HTTP {resp.status}: {text[:240]}")
            return await resp.json()

    async def _collect_micro_inputs(
        self, session: aiohttp.ClientSession, symbol: str
    ) -> Dict[str, Any]:
        contract = self._gate_contract(symbol)
        base_url = getattr(getattr(self.exchange, "REST_URL", None), "rstrip", lambda *_: "https://api.gateio.ws/api/v4")("/")
        if not base_url:
            base_url = "https://api.gateio.ws/api/v4"

        trades_url = f"{base_url}/futures/usdt/trades?contract={contract}&limit=200"
        ob_url = f"{base_url}/futures/usdt/order_book?contract={contract}&limit=5&with_id=true"
        contract_url = f"{base_url}/futures/usdt/contracts/{contract}"

        trades, order_book, contract_info = await asyncio.gather(
            self._fetch_public_json(session, trades_url),
            self._fetch_public_json(session, ob_url),
            self._fetch_public_json(session, contract_url),
        )

        now_ms = time.time() * 1000.0
        prices: List[float] = []
        buy_vol = 0.0
        sell_vol = 0.0
        high_px = 0.0
        low_px = 0.0
        for row in trades or []:
            try:
                ts_ms = float(row.get("create_time_ms") or 0.0)
                if ts_ms <= 0:
                    ts_ms = float(row.get("create_time") or 0.0) * 1000.0
                if now_ms - ts_ms > 60_000:
                    continue
                px = float(row.get("price") or 0.0)
                sz = abs(float(row.get("size") or 0.0))
                if px <= 0:
                    continue
                prices.append(px)
                high_px = max(high_px, px)
                low_px = px if low_px <= 0 else min(low_px, px)
                if str(row.get("side") or "").lower() == "buy":
                    buy_vol += sz
                else:
                    sell_vol += sz
            except Exception:
                continue

        if len(prices) < 2:
            cache_last = float((getattr(self.exchange, "latest_tick_by_symbol", {}) or {}).get(symbol, {}).get("last", 0.0) or 0.0)
            if cache_last > 0:
                prices = [cache_last, cache_last]
                high_px = cache_last
                low_px = cache_last

        log_returns = []
        for i in range(1, len(prices)):
            p0 = prices[i - 1]
            p1 = prices[i]
            if p0 > 0 and p1 > 0:
                log_returns.append(math.log(p1 / p0))
        vol_1m_bps = (math.sqrt(sum(r * r for r in log_returns) / max(len(log_returns), 1)) * 1e4) if log_returns else 0.0
        mean_px = sum(prices) / max(len(prices), 1)
        atr_1m_bps = ((high_px - low_px) / mean_px * 1e4) if mean_px > 0 and high_px > 0 and low_px > 0 else vol_1m_bps

        def _level_size(level: Any) -> float:
            try:
                if isinstance(level, (list, tuple)):
                    return float(level[1]) if len(level) >= 2 else 0.0
                if isinstance(level, dict):
                    return float(
                        level.get("s")
                        or level.get("size")
                        or level.get("amount")
                        or level.get("quantity")
                        or 0.0
                    )
            except Exception:
                return 0.0
            return 0.0

        bids = order_book.get("bids") or []
        asks = order_book.get("asks") or []
        bid5 = sum(_level_size(level) for level in bids[:5])
        ask5 = sum(_level_size(level) for level in asks[:5])
        obi_denom = max(bid5 + ask5, 1e-9)
        obi_5 = (bid5 - ask5) / obi_denom

        funding_rate = float(contract_info.get("funding_rate") or 0.0)
        prev_funding = float(self._last_funding_rate.get(symbol, funding_rate) or funding_rate)
        self._last_funding_rate[symbol] = funding_rate
        funding_trend_bps = (funding_rate - prev_funding) * 1e4

        return {
            "symbol": symbol,
            "contract": contract,
            "volatility_1m_bps": float(vol_1m_bps),
            "atr_1m_bps": float(max(atr_1m_bps, 0.1)),
            "obi_5": float(obi_5),
            "funding_rate": float(funding_rate),
            "funding_trend_bps": float(funding_trend_bps),
            "tick_buy_volume_1m": float(buy_vol),
            "tick_sell_volume_1m": float(sell_vol),
            "price_high_1m": float(high_px),
            "price_low_1m": float(low_px),
            "samples_1m": int(len(prices)),
        }

    @staticmethod
    def _normalize_ai_payload(
        payload: Dict[str, Any],
        fallback_reason: str = "",
        micro: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        raw = dict(payload or {})
        regime_raw = str(raw.get("regime", "STABLE")).upper()
        regime = "VOLATILE" if regime_raw == "VOLATILE" else "STABLE"
        mr = str(raw.get("matrix_regime") or raw.get("trend_regime") or "").upper()
        if mr not in ("STABLE", "TRENDING_UP", "TRENDING_DOWN"):
            if regime_raw in ("TRENDING_UP", "TRENDING_DOWN"):
                mr = regime_raw
            elif regime == "VOLATILE" and isinstance(micro, dict):
                obi = float(micro.get("obi_5", 0.0) or 0.0)
                if obi > 0.22:
                    mr = "TRENDING_UP"
                elif obi < -0.22:
                    mr = "TRENDING_DOWN"
                else:
                    mr = "STABLE"
            else:
                mr = "STABLE"
        try:
            score = max(0.0, min(100.0, float(raw.get("score", 50.0) or 50.0)))
        except Exception:
            score = 50.0
        try:
            lev_cap = int(round(float(raw.get("suggested_leverage_cap", 100) or 100)))
        except Exception:
            lev_cap = 100
        lev_cap = max(10, min(100, lev_cap))
        try:
            tp_mult = float(raw.get("tp_atr_multiplier", 2.0) or 2.0)
        except Exception:
            tp_mult = 2.0
        try:
            sl_mult = float(raw.get("sl_atr_multiplier", 1.8) or 1.8)
        except Exception:
            sl_mult = 1.8
        tp_mult = max(0.5, min(2.0, tp_mult))
        sl_mult = max(1.0, min(3.0, sl_mult))
        if mr in ("TRENDING_UP", "TRENDING_DOWN"):
            regime = "VOLATILE"
        return {
            "regime": regime,
            "matrix_regime": mr,
            "score": score,
            "suggested_leverage_cap": lev_cap,
            "tp_atr_multiplier": tp_mult,
            "sl_atr_multiplier": sl_mult,
            "reason": str(raw.get("reason") or fallback_reason or "Normalized AI payload"),
        }

    async def analyze_markets(self):
        global_config = config_manager.get_config()
        symbols = global_config.strategy.symbols
        async with aiohttp.ClientSession() as session:
            for symbol in symbols:
                if not self.running:
                    break

                try:
                    micro = await self._collect_micro_inputs(session, symbol)
                    prompt = self._build_prompt(symbol, micro)
                    analysis = await self._call_llm_with_retry(prompt, micro=micro)
                    payload = {"symbol": symbol, **micro, **analysis}

                    regime_str = payload.get("regime", "STABLE")
                    score = float(payload.get("score", 50.0))

                    self._cycle_regime_counts[str(regime_str).upper()] += 1
                    self._cycle_scores.append(score)
                    self.publisher.publish(IPC_TOPIC_AI_SCORE, payload)
                    log.debug(
                        f"AI Published [{symbol}]: {regime_str} | Score: {score:.1f} | "
                        f"LevCap: {payload.get('suggested_leverage_cap')} | "
                        f"TPxATR: {payload.get('tp_atr_multiplier')} | SLxATR: {payload.get('sl_atr_multiplier')} | "
                        f"{payload.get('reason', '')}"
                    )

                except Exception as e:
                    log.warning(
                        f"Failed to analyze {symbol}: {type(e).__name__}: {e!r}"
                    )

                await asyncio.sleep(0.35)

    def _build_prompt(self, symbol: str, micro: Dict[str, Any]) -> str:
        micro_json = json.dumps(micro, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        return f"""
Analyze the following 1-minute microstructure snapshot for {symbol} on Gate.io perpetual futures.

Input JSON:
{micro_json}

Rules:
1. Treat high volatility, strong order-book imbalance, and accelerating funding as high-frequency risk signals.
2. Output STRICT JSON only.
3. suggested_leverage_cap must be an integer between 10 and 100.
4. tp_atr_multiplier must be a float between 0.5 and 2.0.
5. sl_atr_multiplier must be a float between 1.0 and 3.0.

Output EXACTLY these fields:
- "regime": string ("VOLATILE" or "STABLE") — legacy coarse bucket
- "matrix_regime": string, MUST be one of:
    "STABLE" (choppy / mean-reversion friendly),
    "TRENDING_UP" (sustained bid pressure / markup),
    "TRENDING_DOWN" (sustained offer pressure / markdown)
  Use tick volume imbalance + OBI in the input JSON.
- "score": float (0.0 to 100.0)
- "suggested_leverage_cap": integer (10-100)
- "tp_atr_multiplier": float (0.5-2.0)
- "sl_atr_multiplier": float (1.0-3.0)
- "reason": string
"""

    async def _call_llm_with_retry(
        self, prompt: str, max_retries: int = 3, *, micro: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Executes LLM call with exponential backoff and strict timeout."""
        for attempt in range(max_retries):
            try:
                raw = await asyncio.wait_for(self.llm.analyze(prompt), timeout=25.0)
                return self._normalize_ai_payload(raw, "LLM response normalized", micro=micro)
            except asyncio.TimeoutError:
                log.warning(f"LLM request timed out (attempt {attempt+1}/{max_retries})")
            except Exception as e:
                log.warning(
                    f"LLM request failed: {type(e).__name__}: {e!r} "
                    f"(attempt {attempt+1}/{max_retries})"
                )

            if attempt < max_retries - 1:
                await asyncio.sleep(2**attempt)

        return self._normalize_ai_payload({}, "LLM fallback defaults")

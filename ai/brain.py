"""
Shark 2.0 — AI Brain (LLM-powered market structure analysis & strategy decisions).

Integrates with any OpenAI-compatible API (DeepSeek, OpenAI, Ollama, local vLLM)
to analyze market microstructure, order-book imbalance, volatility, and multi-timeframe
K-line data, then outputs structured strategy decisions.

Fallback chain: LLM → Heuristics → Conservative Default.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

# ---------------------------------------------------------------------------
# – try importing the existing logger; if running standalone, fall back to print
# ---------------------------------------------------------------------------
try:
    from src.utils.logger import log
except ImportError:
    import logging

    log = logging.getLogger("brain")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_LLM_TIMEOUT_SEC = 30.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_CYCLE_SEC = 900  # 15 minutes
MAX_HISTORY_CANDLES = 120


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
class StrategyMode(str, Enum):
    """Which strategy the Brain recommends."""

    GRID_MAKER = "GRID_MAKER"
    MOMENTUM = "MOMENTUM"
    BREAKOUT = "BREAKOUT"
    MEAN_REVERT = "MEAN_REVERT"
    IDLE = "IDLE"


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"


@dataclass
class MarketSnapshot:
    """Structured market data fed into the LLM prompt."""

    symbol: str
    price: float
    timestamp: float = field(default_factory=time.time)
    # OHLCV candles (list of {open,high,low,close,volume})
    candles_1m: List[Dict[str, float]] = field(default_factory=list)
    candles_5m: List[Dict[str, float]] = field(default_factory=list)
    candles_15m: List[Dict[str, float]] = field(default_factory=list)
    candles_1h: List[Dict[str, float]] = field(default_factory=list)
    # Order book (top N levels)
    best_bid: float = 0.0
    best_ask: float = 0.0
    bid_volume_5: float = 0.0
    ask_volume_5: float = 0.0
    obi: float = 0.0  # Order Book Imbalance [-1, 1]
    spread_bps: float = 0.0
    # Volume metrics
    volume_24h: float = 0.0
    volume_ratio: float = 1.0  # recent vol / avg vol
    # Volatility
    atr_14: float = 0.0
    atr_14_bps: float = 0.0
    # Trend
    sma_20: float = 0.0
    sma_50: float = 0.0
    ema_12: float = 0.0
    ema_26: float = 0.0
    # Sentiment
    funding_rate: float = 0.0
    open_interest: float = 0.0


@dataclass
class StrategyDecision:
    """Structured output from the AI Brain."""

    symbol: str
    timestamp: float = field(default_factory=time.time)
    # Primary strategy recommendation
    recommended_mode: StrategyMode = StrategyMode.IDLE
    direction: Direction = Direction.NEUTRAL
    confidence: float = 0.0  # 0-100

    # Position sizing
    position_size_pct: float = 0.0  # % of available capital
    suggested_leverage: int = 1
    entry_price_zone: Tuple[float, float] = (0.0, 0.0)

    # Risk parameters
    take_profit_atr_mult: float = 2.0
    stop_loss_atr_mult: float = 1.5
    trailing_stop_atr_mult: float = 0.0

    # Market assessment
    regime: str = "STABLE"
    volatility_grade: str = "MEDIUM"  # LOW / MEDIUM / HIGH / EXTREME
    trend_strength: float = 0.0  # 0-100

    # LLM reasoning (for explainability)
    reasoning: str = ""
    source: str = "default"  # "llm" | "heuristic" | "conservative"


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are an elite quantitative trading AI specializing in crypto futures.
You analyze market microstructure, order-book dynamics, multi-timeframe price action,
volatility, and volume to produce precise, risk-aware strategy decisions.

Your output MUST be a single valid JSON object with the following schema:
{
  "recommended_mode": "GRID_MAKER" | "MOMENTUM" | "BREAKOUT" | "MEAN_REVERT" | "IDLE",
  "direction": "LONG" | "SHORT" | "NEUTRAL",
  "confidence": 0-100,
  "position_size_pct": 0.0-1.0,
  "suggested_leverage": 1-125,
  "entry_price_low": float,
  "entry_price_high": float,
  "take_profit_atr_mult": 1.0-5.0,
  "stop_loss_atr_mult": 1.0-5.0,
  "trailing_stop_atr_mult": 0.0-5.0,
  "regime": "STABLE" | "VOLATILE" | "TRENDING_UP" | "TRENDING_DOWN" | "CHAOTIC",
  "volatility_grade": "LOW" | "MEDIUM" | "HIGH" | "EXTREME",
  "trend_strength": 0-100,
  "reasoning": "concise explanation (max 200 chars)"
}

Rules:
- IDLE when no clear edge exists (confidence < 30).
- GRID_MAKER is best in low-volatility, range-bound markets.
- MOMENTUM is best with strong trends and high volume.
- BREAKOUT when price compresses near key levels with rising volume.
- MEAN_REVERT when price is stretched >2 ATR from mean in low-trend conditions.
- position_size_pct must never exceed 0.25 (25% of capital).
- suggested_leverage must scale inversely with volatility.
- Always prefer capital preservation over aggressive sizing.
"""


def build_market_prompt(snapshot: MarketSnapshot) -> str:
    """Build a dense, token-efficient market prompt from a snapshot."""

    def _candles_summary(candles: List[Dict[str, float]], n: int = 5) -> str:
        if not candles:
            return "no data"
        recent = candles[-n:]
        ohlc_parts = []
        for c in recent:
            ohlc_parts.append(
                f"O:{c.get('open',0):.4f} H:{c.get('high',0):.4f} "
                f"L:{c.get('low',0):.4f} C:{c.get('close',0):.4f} V:{c.get('volume',0):.1f}"
            )
        return " | ".join(ohlc_parts)

    lines = [
        f"Symbol: {snapshot.symbol}",
        f"Price: {snapshot.price:.6f}",
        f"Spread: {snapshot.spread_bps:.1f} bps  |  OBI: {snapshot.obi:.3f}",
        f"Volatility: ATR(14)={snapshot.atr_14:.6f} ({snapshot.atr_14_bps:.1f} bps)",
        f"SMA20={snapshot.sma_20:.6f}  SMA50={snapshot.sma_50:.6f}  "
        f"EMA12={snapshot.ema_12:.6f}  EMA26={snapshot.ema_26:.6f}",
        f"Volume 24h: {snapshot.volume_24h:.0f}  |  Vol Ratio: {snapshot.volume_ratio:.2f}",
        f"Funding Rate: {snapshot.funding_rate*100:.4f}%  |  OI: {snapshot.open_interest:.0f}",
        f"Best Bid: {snapshot.best_bid:.6f}  Best Ask: {snapshot.best_ask:.6f}",
        f"Bid Vol(5): {snapshot.bid_volume_5:.1f}  Ask Vol(5): {snapshot.ask_volume_5:.1f}",
        "",
        "--- Recent 1m candles (newest last) ---",
        _candles_summary(snapshot.candles_1m, 10),
        "",
        "--- Recent 5m candles ---",
        _candles_summary(snapshot.candles_5m, 5),
        "",
        "--- Recent 15m candles ---",
        _candles_summary(snapshot.candles_15m, 3),
        "",
        "--- Recent 1h candles ---",
        _candles_summary(snapshot.candles_1h, 3),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM client abstraction (OpenAI-compatible)
# ---------------------------------------------------------------------------
class LLMClient:
    """Thin wrapper around any OpenAI-compatible chat completions endpoint."""

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "https://api.deepseek.com/v1",
        model: str = "deepseek-chat",
        timeout: float = DEFAULT_LLM_TIMEOUT_SEC,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries

    async def _post(self, session: aiohttp.ClientSession, payload: dict) -> dict:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"
        async with session.post(
            url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=self.timeout)
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise ConnectionError(f"LLM API {resp.status}: {text[:300]}")
            return await resp.json()

    async def chat_json(self, system: str, user: str) -> Dict[str, Any]:
        """Send a chat request and parse JSON response."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.3,  # low temp for consistent trading decisions
        }

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    data = await self._post(session, payload)
                    choices = data.get("choices", [])
                    if not choices:
                        raise ValueError("LLM returned empty choices")
                    content = choices[0]["message"]["content"]
                    return json.loads(content)
            except (json.JSONDecodeError, ValueError) as e:
                last_error = e
                log.warning(f"LLM JSON parse failed (attempt {attempt+1}): {e}")
                await asyncio.sleep(1.0 * (attempt + 1))
            except ConnectionError as e:
                last_error = e
                log.warning(f"LLM connection error (attempt {attempt+1}): {e}")
                if attempt < self.max_retries:
                    await asyncio.sleep(2.0 * (attempt + 1))

        raise RuntimeError(f"LLM failed after {self.max_retries+1} attempts: {last_error}")


# ---------------------------------------------------------------------------
# Decision parser & validator
# ---------------------------------------------------------------------------
def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _safe_int(val: Any, default: int = 1) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def parse_decision(symbol: str, raw: Dict[str, Any]) -> StrategyDecision:
    """Parse and validate LLM JSON output into a StrategyDecision."""
    mode_raw = str(raw.get("recommended_mode", "IDLE")).upper()
    try:
        recommended_mode = StrategyMode(mode_raw)
    except ValueError:
        recommended_mode = StrategyMode.IDLE

    direction_raw = str(raw.get("direction", "NEUTRAL")).upper()
    try:
        direction = Direction(direction_raw)
    except ValueError:
        direction = Direction.NEUTRAL

    confidence = max(0.0, min(100.0, _safe_float(raw.get("confidence"), 0.0)))
    position_size_pct = max(0.0, min(0.25, _safe_float(raw.get("position_size_pct"), 0.0)))
    suggested_leverage = max(1, min(125, _safe_int(raw.get("suggested_leverage"), 1)))
    entry_low = _safe_float(raw.get("entry_price_low"), 0.0)
    entry_high = max(entry_low, _safe_float(raw.get("entry_price_high"), entry_low))

    tp_mult = max(1.0, min(5.0, _safe_float(raw.get("take_profit_atr_mult"), 2.0)))
    sl_mult = max(1.0, min(5.0, _safe_float(raw.get("stop_loss_atr_mult"), 1.5)))
    trail_mult = max(0.0, min(5.0, _safe_float(raw.get("trailing_stop_atr_mult"), 0.0)))

    regime = str(raw.get("regime", "STABLE"))[:20]
    vol_grade = str(raw.get("volatility_grade", "MEDIUM"))[:10]
    trend_strength = max(0.0, min(100.0, _safe_float(raw.get("trend_strength"), 0.0)))
    reasoning = str(raw.get("reasoning", ""))[:300]

    # If confidence is too low, override to IDLE
    if confidence < 30.0:
        recommended_mode = StrategyMode.IDLE
        position_size_pct = 0.0
        direction = Direction.NEUTRAL

    return StrategyDecision(
        symbol=symbol,
        recommended_mode=recommended_mode,
        direction=direction,
        confidence=confidence,
        position_size_pct=position_size_pct,
        suggested_leverage=suggested_leverage,
        entry_price_zone=(entry_low, entry_high),
        take_profit_atr_mult=tp_mult,
        stop_loss_atr_mult=sl_mult,
        trailing_stop_atr_mult=trail_mult,
        regime=regime,
        volatility_grade=vol_grade,
        trend_strength=trend_strength,
        reasoning=reasoning,
        source="llm",
    )


# ---------------------------------------------------------------------------
# AI Brain
# ---------------------------------------------------------------------------
class AIBrain:
    """
    The central AI decision engine.

    1. Receives MarketSnapshots (fed by the orchestrator or data pipeline).
    2. Builds a structured prompt and queries the LLM.
    3. Parses and validates the response into a StrategyDecision.
    4. Falls back to heuristics on failure, or returns a conservative default.

    Usage
    -----
        brain = AIBrain(llm_client=LLMClient(api_key=..., base_url=...))
        decision = await brain.decide(snapshot)
    """

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        fallback_to_heuristics: bool = True,
        default_cycle_sec: float = DEFAULT_CYCLE_SEC,
    ):
        self.llm = llm_client
        self.fallback_to_heuristics = fallback_to_heuristics
        self.default_cycle_sec = default_cycle_sec

        # Per-symbol throttle: minimum seconds between LLM calls
        self._last_call: Dict[str, float] = {}

        # Stats
        self.stats: Dict[str, int] = {"llm_success": 0, "llm_error": 0, "heuristic_fallback": 0}

    @classmethod
    def from_config(cls, config: Optional[Dict[str, Any]] = None) -> "AIBrain":
        """
        Create an AIBrain from a config dict or environment variables.

        Environment variables:
            SHARK2_LLM_API_KEY     — API key
            SHARK2_LLM_BASE_URL    — Base URL (default: https://api.deepseek.com/v1)
            SHARK2_LLM_MODEL       — Model name (default: deepseek-chat)
            SHARK2_LLM_ENABLED     — "true"/"false" (default: true)
        """
        cfg = config or {}
        enabled = os.environ.get("SHARK2_LLM_ENABLED", str(cfg.get("llm_enabled", True))).lower() in (
            "true",
            "1",
            "yes",
        )
        api_key = os.environ.get("SHARK2_LLM_API_KEY", cfg.get("llm_api_key", ""))
        base_url = os.environ.get("SHARK2_LLM_BASE_URL", cfg.get("llm_base_url", "https://api.deepseek.com/v1"))
        model = os.environ.get("SHARK2_LLM_MODEL", cfg.get("llm_model", "deepseek-chat"))

        # Also try the existing darwin config for backward compat
        if not api_key:
            try:
                from src.core.config_manager import config_manager as cm

                dc = cm.get_config().darwin
                api_key = (getattr(dc, "llm_api_key", "") or "").strip()
                if not api_key:
                    api_key = os.environ.get("DEEPSEEK_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
                if getattr(dc, "llm_provider", "mock") == "deepseek":
                    base_url = base_url
                elif getattr(dc, "llm_provider", "") == "openai":
                    base_url = os.environ.get("SHARK2_LLM_BASE_URL", "https://api.openai.com/v1")
            except Exception:
                pass

        llm_client = None
        if enabled and api_key:
            llm_client = LLMClient(api_key=api_key, base_url=base_url, model=model)
        elif enabled:
            log.warning("AI Brain LLM enabled but no API key found — will use heuristic fallback only.")

        return cls(llm_client=llm_client, fallback_to_heuristics=True)

    # ------------------------------------------------------------------
    # Core decision method
    # ------------------------------------------------------------------
    async def decide(self, snapshot: MarketSnapshot) -> StrategyDecision:
        """
        Produce a strategy decision for the given market snapshot.

        Decision chain:  LLM → Heuristics → Conservative Default
        """
        symbol = snapshot.symbol

        # Throttle: don't call LLM more than once per cycle per symbol
        now = time.time()
        if symbol in self._last_call and (now - self._last_call[symbol]) < self.default_cycle_sec:
            # Return a cached-style "hold" decision
            return StrategyDecision(
                symbol=symbol,
                recommended_mode=StrategyMode.IDLE,
                direction=Direction.NEUTRAL,
                reasoning="Cycle throttle — waiting for next analysis window.",
                source="throttle",
            )

        # --- 1. Try LLM ---
        if self.llm is not None:
            try:
                user_prompt = build_market_prompt(snapshot)
                raw = await self.llm.chat_json(SYSTEM_PROMPT, user_prompt)
                decision = parse_decision(symbol, raw)
                self._last_call[symbol] = now
                self.stats["llm_success"] += 1
                log.info(
                    f"[Brain] {symbol} LLM → {decision.recommended_mode.value} "
                    f"({decision.direction.value}) conf={decision.confidence:.0f} "
                    f"size={decision.position_size_pct:.1%}"
                )
                return decision
            except Exception as e:
                self.stats["llm_error"] += 1
                log.error(f"[Brain] LLM error for {symbol}: {type(e).__name__}: {e}")

        # --- 2. Fallback to heuristics ---
        if self.fallback_to_heuristics:
            try:
                from .heuristics import heuristic_decision

                decision = heuristic_decision(snapshot)
                self.stats["heuristic_fallback"] += 1
                decision.source = "heuristic"
                self._last_call[symbol] = now
                log.info(
                    f"[Brain] {symbol} Heuristic → {decision.recommended_mode.value} "
                    f"({decision.direction.value}) conf={decision.confidence:.0f}"
                )
                return decision
            except Exception as e:
                log.error(f"[Brain] Heuristic fallback error for {symbol}: {type(e).__name__}: {e}")

        # --- 3. Conservative default ---
        return StrategyDecision(
            symbol=symbol,
            recommended_mode=StrategyMode.IDLE,
            direction=Direction.NEUTRAL,
            confidence=0.0,
            reasoning="All decision engines failed — conservative IDLE.",
            source="conservative",
        )

    # ------------------------------------------------------------------
    # Batch analysis
    # ------------------------------------------------------------------
    async def decide_batch(self, snapshots: List[MarketSnapshot]) -> Dict[str, StrategyDecision]:
        """Analyze multiple symbols concurrently."""
        tasks = [self.decide(s) for s in snapshots]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        decisions: Dict[str, StrategyDecision] = {}
        for snap, result in zip(snapshots, results):
            if isinstance(result, Exception):
                log.error(f"[Brain] Batch error for {snap.symbol}: {result}")
                decisions[snap.symbol] = StrategyDecision(
                    symbol=snap.symbol,
                    source="conservative",
                    reasoning=f"Batch error: {result}",
                )
            else:
                decisions[snap.symbol] = result
        return decisions

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------
    def get_stats(self) -> Dict[str, Any]:
        return {
            **self.stats,
            "llm_enabled": self.llm is not None,
            "cycle_sec": self.default_cycle_sec,
        }

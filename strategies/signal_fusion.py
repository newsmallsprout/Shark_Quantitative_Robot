"""
Signal Fusion Strategy — multi-source signal combiner with regime-aware gating.

Purpose:
  Fuses signals from GridMaker and other active strategies into a single
  execution decision stream. Acts as a meta-layer that can:
    - Gate entries based on regime / OBI / RSI / AI confidence.
    - Adjust position sizing per fused signal strength.
    - Coordinate between strategies to avoid conflicting positions.
    - Implement a "superposition" scoring model where multiple weak signals
      can combine into a tradable conviction level.

Architecture:
  - Each source strategy writes its intent to a shared fusion ledger.
  - On each tick, fusion reads all pending intents, scores them, and decides
    whether to emit a consolidated SignalEvent.
  - Conflicting signals (e.g. grid_maker wants long, core_neutral wants short)
    are resolved by weighted scoring — strongest conviction wins.
  - Regime filter: in CHAOTIC or UNKNOWN regimes, fusion stays flat.

Config keys (YAML / config_manager):
  signal_fusion:
    enabled: true
    symbols: ["BTC/USDT", "ETH/USDT"]
    source_strategies:
      - GridMaker
      - CoreNeutral
      - CoreAttack
    require_regime_consensus: true    # skip if sources disagree on direction
    min_fusion_score: 60.0            # 0-100
    max_positions_global: 3
    cooldown_sec: 8.0
    grid_maker_weight: 0.40
    core_neutral_weight: 0.30
    core_attack_weight: 0.30
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from src.strategy.base import BaseStrategy
from src.core.events import TickEvent, SignalEvent
from src.core.paper_engine import paper_engine
from src.core.risk_engine import risk_engine
from src.core.config_manager import config_manager
from src.core.globals import bot_context
from src.utils.logger import log


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SourceIntent:
    """A signal intent recorded by a source strategy."""
    source: str          # strategy name (e.g. "GridMaker", "CoreNeutral")
    symbol: str
    side: str            # "buy" or "sell"
    score: float         # 0-100 confidence from the source
    price: float
    timestamp: float
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FusionConfig:
    """Runtime config loaded from config_manager."""
    enabled: bool = False
    symbols: List[str] = field(default_factory=lambda: ["BTC/USDT"])
    source_strategies: List[str] = field(
        default_factory=lambda: ["GridMaker", "CoreNeutral", "CoreAttack"]
    )
    require_regime_consensus: bool = True
    min_fusion_score: float = 60.0
    max_positions_global: int = 3
    cooldown_sec: float = 8.0
    grid_maker_weight: float = 0.40
    core_neutral_weight: float = 0.30
    core_attack_weight: float = 0.30
    # Additional fusion parameters
    rsi_buy_threshold: float = 30.0
    rsi_sell_threshold: float = 70.0
    obi_confirm_threshold: float = 0.05
    leverage: int = 10
    margin_fraction: float = 0.02     # max 2% per fused signal


def _load_fusion_config() -> FusionConfig:
    """Read signal_fusion section from config_manager."""
    cfg = FusionConfig()
    try:
        fc = getattr(config_manager.get_config(), "signal_fusion", None)
        if fc is None:
            return cfg
        cfg.enabled = bool(getattr(fc, "enabled", False))
        cfg.symbols = list(getattr(fc, "symbols", ["BTC/USDT"]) or ["BTC/USDT"])
        cfg.source_strategies = list(
            getattr(fc, "source_strategies", ["GridMaker", "CoreNeutral", "CoreAttack"])
            or ["GridMaker", "CoreNeutral", "CoreAttack"]
        )
        cfg.require_regime_consensus = bool(
            getattr(fc, "require_regime_consensus", True)
        )
        cfg.min_fusion_score = float(getattr(fc, "min_fusion_score", 60.0) or 60.0)
        cfg.max_positions_global = int(getattr(fc, "max_positions_global", 3) or 3)
        cfg.cooldown_sec = float(getattr(fc, "cooldown_sec", 8.0) or 8.0)
        cfg.grid_maker_weight = float(getattr(fc, "grid_maker_weight", 0.40) or 0.40)
        cfg.core_neutral_weight = float(
            getattr(fc, "core_neutral_weight", 0.30) or 0.30
        )
        cfg.core_attack_weight = float(
            getattr(fc, "core_attack_weight", 0.30) or 0.30
        )
        cfg.rsi_buy_threshold = float(getattr(fc, "rsi_buy_threshold", 30.0) or 30.0)
        cfg.rsi_sell_threshold = float(
            getattr(fc, "rsi_sell_threshold", 70.0) or 70.0
        )
        cfg.obi_confirm_threshold = float(
            getattr(fc, "obi_confirm_threshold", 0.05) or 0.05
        )
        cfg.leverage = int(getattr(fc, "leverage", 10) or 10)
        cfg.margin_fraction = float(getattr(fc, "margin_fraction", 0.02) or 0.02)
    except Exception:
        pass
    return cfg


# ---------------------------------------------------------------------------
# SignalFusionStrategy
# ---------------------------------------------------------------------------

class SignalFusionStrategy(BaseStrategy):
    """
    Meta-strategy that fuses signals from multiple source strategies.

    Maintains a ledger of intents from recognized sources, periodically
    evaluates them, and emits a single consolidated signal when fusion score
    exceeds threshold.

    Lifecycle:
      - on_tick: updates prices, performs lightweight confirmation checks.
      - ingest_intent: called by source strategies (or intercepted from
        the signal queue) to register an intent.
      - run(): background loop that periodically fuses intents and emits.
    """

    def __init__(self) -> None:
        super().__init__("SignalFusion")
        self._cfg: FusionConfig = _load_fusion_config()
        # Intent ledger: symbol -> list of SourceIntent
        self._ledger: Dict[str, List[SourceIntent]] = defaultdict(list)
        # Pending fused decisions awaiting execution
        self._pending: Dict[str, Dict[str, Any]] = {}
        # Cooldown tracking
        self._last_fire: Dict[str, float] = {}
        # Price window for RSI calc
        self._price_windows: Dict[str, List[float]] = defaultdict(list)
        # Background task
        self._run_task: Optional[asyncio.Task] = None
        # Intent TTL (max age before dropping)
        self._intent_max_age_sec: float = 5.0

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def _refresh_cfg(self) -> None:
        self._cfg = _load_fusion_config()

    @property
    def cfg(self) -> FusionConfig:
        return self._cfg

    # ------------------------------------------------------------------
    # Ledger management
    # ------------------------------------------------------------------

    def ingest_intent(self, intent: SourceIntent) -> None:
        """Called by source strategies to register their signal intent."""
        if intent.source not in self.cfg.source_strategies:
            return
        if intent.symbol not in self.cfg.symbols:
            return
        ledger = self._ledger[intent.symbol]
        # replace any older intent from same source
        ledger[:] = [i for i in ledger if i.source != intent.source]
        ledger.append(intent)
        log.info(
            f"[SignalFusion] Ingest {intent.source}/{intent.symbol} "
            f"{intent.side} score={intent.score:.1f}"
        )

    def _prune_stale_intents(self, now: float) -> None:
        """Remove intents older than max age."""
        for symbol in list(self._ledger.keys()):
            self._ledger[symbol] = [
                i for i in self._ledger[symbol]
                if now - i.timestamp < self._intent_max_age_sec
            ]
            if not self._ledger[symbol]:
                del self._ledger[symbol]

    # ------------------------------------------------------------------
    # RSI (simple rolling)
    # ------------------------------------------------------------------

    def _rolling_rsi(self, symbol: str, period: int = 14) -> Optional[float]:
        pw = self._price_windows.get(symbol, [])
        if len(pw) < period + 1:
            return None
        recent = pw[-period - 1:]
        gains = 0.0
        losses = 0.0
        for i in range(1, len(recent)):
            delta = recent[i] - recent[i - 1]
            if delta > 0:
                gains += delta
            else:
                losses += abs(delta)
        avg_gain = gains / period
        avg_loss = losses / period
        if avg_loss < 1e-12:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    # ------------------------------------------------------------------
    # OBI retrieval
    # ------------------------------------------------------------------

    def _latest_obi(self, symbol: str) -> float:
        """Get latest OBI from CoreAttack strategy if available."""
        se = bot_context.get_strategy_engine()
        if se is None:
            return 0.0
        for st in getattr(se, "strategies", []) or []:
            if getattr(st, "name", "") == "CoreAttack":
                return float(getattr(st, "latest_obi", 0.0) or 0.0)
        return 0.0

    # ------------------------------------------------------------------
    # Regime
    # ------------------------------------------------------------------

    def _current_regime(self, symbol: str) -> str:
        try:
            from src.ai.regime import regime_classifier
            regime = regime_classifier.analyze(symbol)
            return str(regime.value) if regime else "UNKNOWN"
        except Exception:
            return "UNKNOWN"

    # ------------------------------------------------------------------
    # Source weight lookup
    # ------------------------------------------------------------------

    def _source_weight(self, source_name: str) -> float:
        mapping = {
            "gridmaker": self.cfg.grid_maker_weight,
            "coreneutral": self.cfg.core_neutral_weight,
            "coreattack": self.cfg.core_attack_weight,
            "grid_maker": self.cfg.grid_maker_weight,
            "core_neutral": self.cfg.core_neutral_weight,
            "core_attack": self.cfg.core_attack_weight,
        }
        return mapping.get(source_name.lower(), 0.15)

    # ------------------------------------------------------------------
    # Fusion engine
    # ------------------------------------------------------------------

    def _fuse_intents(
        self, symbol: str, now: float
    ) -> Optional[Tuple[str, float, Dict[str, Any]]]:
        """
        Combine all intents for a symbol into a fused (side, score, meta).

        Returns None if no consensus or score too low.
        """
        intents = self._ledger.get(symbol, [])
        if not intents:
            return None

        # Separate buy and sell scores
        buy_score = 0.0
        sell_score = 0.0
        total_weight = 0.0
        details: Dict[str, Any] = {"sources": []}

        for intent in intents:
            w = self._source_weight(intent.source)
            if w <= 0:
                continue
            total_weight += w
            weighted = intent.score * w
            details["sources"].append({
                "source": intent.source,
                "side": intent.side,
                "raw_score": intent.score,
                "weight": w,
                "weighted_score": weighted,
            })
            if intent.side.lower() == "buy":
                buy_score += weighted
            elif intent.side.lower() == "sell":
                sell_score += weighted

        if total_weight <= 1e-9:
            return None

        # Normalize to 0-100
        buy_norm = (buy_score / total_weight) if total_weight > 0 else 0.0
        sell_norm = (sell_score / total_weight) if total_weight > 0 else 0.0

        if buy_norm > sell_norm and buy_norm >= self.cfg.min_fusion_score:
            return "buy", buy_norm, details
        if sell_norm > buy_norm and sell_norm >= self.cfg.min_fusion_score:
            return "sell", sell_norm, details

        return None

    def _confirm_regime(self, symbol: str, side: str) -> bool:
        """Check regime is compatible with the proposed side."""
        if not self.cfg.require_regime_consensus:
            return True
        regime = self._current_regime(symbol)
        # Allow OSCILLATING for both sides (mean reversion friendly)
        regime_upper = regime.upper()
        if regime_upper in ("UNKNOWN", "CHAOTIC"):
            return False
        if side == "sell" and regime_upper in ("TRENDING_UP",):
            return False
        if side == "buy" and regime_upper in ("TRENDING_DOWN",):
            return False
        return True

    def _confirm_obi(self, symbol: str, side: str) -> bool:
        """Check OBI is aligned with the trade direction."""
        threshold = self.cfg.obi_confirm_threshold
        if threshold <= 0:
            return True
        obi = self._latest_obi(symbol)
        if side == "buy" and obi < -threshold:
            log.info(f"[SignalFusion] OBI veto buy: obi={obi:.3f} < -{threshold:.3f}")
            return False
        if side == "sell" and obi > threshold:
            log.info(f"[SignalFusion] OBI veto sell: obi={obi:.3f} > {threshold:.3f}")
            return False
        return True

    def _confirm_rsi(self, symbol: str, side: str) -> bool:
        """Basic RSI filter to avoid chasing extremes."""
        rsi = self._rolling_rsi(symbol)
        if rsi is None:
            return True  # insufficient data → pass
        if side == "buy" and rsi > self.cfg.rsi_sell_threshold:
            log.info(f"[SignalFusion] RSI veto buy: RSI={rsi:.1f} > {self.cfg.rsi_sell_threshold:.1f}")
            return False
        if side == "sell" and rsi < self.cfg.rsi_buy_threshold:
            log.info(f"[SignalFusion] RSI veto sell: RSI={rsi:.1f} < {self.cfg.rsi_buy_threshold:.1f}")
            return False
        return True

    # ------------------------------------------------------------------
    # Position count
    # ------------------------------------------------------------------

    def _open_positions_count(self) -> int:
        """Count how many symbols currently have paper positions."""
        count = 0
        for sym, pos in paper_engine.positions.items():
            if float(pos.get("size", 0.0) or 0.0) > 1e-12:
                count += 1
        return count

    # ------------------------------------------------------------------
    # Signal emission
    # ------------------------------------------------------------------

    def _emit_fused_signal(
        self,
        symbol: str,
        side: str,
        score: float,
        last_price: float,
        metadata: Dict[str, Any],
    ) -> None:
        """Build and emit a consolidated SignalEvent."""
        eq = max(float(risk_engine.current_balance or 0.0), 1e-9)
        margin = eq * self.cfg.margin_fraction
        notional = margin * self.cfg.leverage
        cs = paper_engine.contract_size_for_symbol(symbol)
        contracts = notional / max(last_price * cs, 1e-12)
        if contracts <= 1e-12:
            return

        bb, ba = paper_engine._best_bid_ask(symbol)
        if side == "buy":
            entry_px = bb if bb > 0 else last_price * 0.9995
        else:
            entry_px = ba if ba > 0 else last_price * 1.0005

        # compute TP/SL brackets using standard core approach
        try:
            from src.strategy.core_strategy import _core_bracket_limit_prices
            tp_px, sl_px = _core_bracket_limit_prices(
                symbol, side, last_price, contracts, leverage=self.cfg.leverage
            )
        except Exception:
            tp_px = 0.0
            sl_px = 0.0

        ctx = {
            "strategy": self.name,
            "signal_fusion": True,
            "fusion_score": float(score),
            "fusion_sources": metadata.get("sources", []),
            "fusion_details": metadata,
            "regime": self._current_regime(symbol),
            "obi": self._latest_obi(symbol),
            "rsi": self._rolling_rsi(symbol),
            "entry_limit_price": float(entry_px),
            "entry_limit_post_only": True,
            "core_limit_requote_enabled": True,
            "core_limit_ttl_ms": 8000,
            "core_limit_requote_max": 2,
            "resting_quote": True,
            "take_profit_limit_price": tp_px,
            "stop_loss_limit_price": sl_px,
        }

        self.emit_signal(
            SignalEvent(
                strategy_name=self.name,
                symbol=symbol,
                side=side,
                order_type="limit",
                price=entry_px,
                amount=float(contracts),
                leverage=self.cfg.leverage,
                post_only=True,
                margin_mode="cross",
                entry_context=ctx,
            )
        )

        log.warning(
            f"[SignalFusion] EMIT {side.upper()} {symbol} "
            f"fusion_score={score:.1f} price≈{last_price:.4f} "
            f"contracts≈{contracts:.6f}"
        )

    # ------------------------------------------------------------------
    # Tick handler
    # ------------------------------------------------------------------

    async def on_tick(self, event: TickEvent) -> None:
        """Lightweight tick: maintain price window for RSI."""
        symbol = event.symbol
        if symbol not in self.cfg.symbols:
            return
        last_price = float(event.ticker.get("last", 0.0))
        if last_price <= 0:
            return
        pw = self._price_windows[symbol]
        pw.append(last_price)
        if len(pw) > 100:
            self._price_windows[symbol] = pw[-50:]

    # ------------------------------------------------------------------
    # Intent interception  (hook for GridMaker)
    # ------------------------------------------------------------------

    def intercept_gridmaker_signal(self, signal: SignalEvent) -> Optional[SourceIntent]:
        """
        Convert a GridMaker SignalEvent into a SourceIntent for fusion.
        Returns None if the signal does not belong to a recognized source.
        """
        if signal.strategy_name not in self.cfg.source_strategies:
            return None

        # Map grid_maker tier to a confidence score
        ect = dict(signal.entry_context or {})
        tier = int(ect.get("grid_tier", 0) or 0)
        # Higher tier = higher confidence (more aggressive)
        base_score = 50.0 + tier * 15.0  # 50, 65, 80, 95
        base_score = min(100.0, base_score)

        side = str(signal.side).lower()
        price = float(signal.price or 0.0)

        return SourceIntent(
            source=signal.strategy_name,
            symbol=signal.symbol,
            side=side,
            score=base_score,
            price=price,
            timestamp=time.time(),
            metadata={"grid_tier": tier, "original_signal": str(signal.strategy_name)},
        )

    # ------------------------------------------------------------------
    # Background run loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        Background fusion loop.
        Periodically evaluates the intent ledger and emits fused signals.
        """
        log.info("[SignalFusion] Background run() started")
        self._refresh_cfg()

        while True:
            try:
                if not self.cfg.enabled:
                    await asyncio.sleep(5.0)
                    self._refresh_cfg()
                    continue

                now = time.time()
                self._prune_stale_intents(now)

                # Check global position cap
                open_count = self._open_positions_count()
                if open_count >= self.cfg.max_positions_global:
                    await asyncio.sleep(1.0)
                    continue

                for symbol in self.cfg.symbols:
                    # Skip if already have a position
                    pos = paper_engine.positions.get(symbol)
                    if pos and float(pos.get("size", 0.0) or 0.0) > 1e-12:
                        continue

                    # Cooldown
                    if now - self._last_fire.get(symbol, 0.0) < self.cfg.cooldown_sec:
                        continue

                    last_price = float(
                        paper_engine.latest_prices.get(symbol, 0.0) or 0.0
                    )
                    if last_price <= 0:
                        continue

                    # Fuse intents
                    result = self._fuse_intents(symbol, now)
                    if result is None:
                        continue

                    side, score, metadata = result

                    # Confirmations
                    if not self._confirm_regime(symbol, side):
                        log.info(
                            f"[SignalFusion] Regime veto {side} {symbol} "
                            f"regime={self._current_regime(symbol)}"
                        )
                        # flush vetoed intents
                        self._ledger.pop(symbol, None)
                        continue

                    if not self._confirm_obi(symbol, side):
                        self._ledger.pop(symbol, None)
                        continue

                    if not self._confirm_rsi(symbol, side):
                        self._ledger.pop(symbol, None)
                        continue

                    # Emit fused signal
                    self._emit_fused_signal(symbol, side, score, last_price, metadata)
                    self._last_fire[symbol] = now
                    # Clear processed intents
                    self._ledger.pop(symbol, None)

                # Periodic config refresh
                self._refresh_cfg()
                await asyncio.sleep(1.0)  # 1s loop interval

            except asyncio.CancelledError:
                log.info("[SignalFusion] run() cancelled")
                break
            except Exception as e:
                log.error(f"[SignalFusion] run() error: {e}")
                await asyncio.sleep(2.0)

    async def start_background(self) -> None:
        """Called by engine to start background fusion loop."""
        if self._run_task is None or self._run_task.done():
            self._run_task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        """Graceful shutdown."""
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
            self._run_task = None

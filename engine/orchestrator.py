"""
Shark 2.0 — Strategy Orchestrator.

The Orchestrator is the central coordination layer that:
  1. Gathers market snapshots from the exchange/data pipeline.
  2. Feeds them to the AI Brain (or heuristic fallback).
  3. Routes strategy decisions to the appropriate strategy modules.
  4. Manages strategy lifecycle (activate, deactivate, adjust).
  5. Enforces cross-strategy risk limits and capital allocation.

Flow:
  Market Data → Brain.decide() → Decision → Strategy dispatch → Execution
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

# ---------------------------------------------------------------------------
# – try importing the existing logger
# ---------------------------------------------------------------------------
try:
    from src.utils.logger import log
except ImportError:
    import logging

    log = logging.getLogger("orchestrator")


# ---------------------------------------------------------------------------
# Local imports (lazy to avoid coupling at module load)
# ---------------------------------------------------------------------------
def _brain_types():
    from ai.brain import (
        AIBrain,
        LLMClient,
        MarketSnapshot,
        StrategyDecision,
        StrategyMode,
        Direction,
    )

    return AIBrain, LLMClient, MarketSnapshot, StrategyDecision, StrategyMode, Direction


# ---------------------------------------------------------------------------
# Orchestrator state
# ---------------------------------------------------------------------------
class StrategyStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COOLDOWN = "COOLDOWN"
    DISABLED = "DISABLED"


@dataclass
class StrategySlot:
    """Tracks the state of one active strategy."""

    mode: str  # StrategyMode value
    symbol: str
    status: StrategyStatus = StrategyStatus.ACTIVE
    entry_time: float = 0.0
    entry_price: float = 0.0
    position_size_usdt: float = 0.0
    unrealized_pnl: float = 0.0
    last_decision: Any = None  # StrategyDecision
    cooldown_until: float = 0.0


@dataclass
class OrchestratorConfig:
    """Configuration for the Orchestrator."""

    max_concurrent_strategies: int = 4
    max_position_pct_per_symbol: float = 0.25  # max 25% of equity per symbol
    max_total_exposure_pct: float = 1.0  # total exposure cap (1.0 = 100%)
    cooldown_sec: float = 45.0
    min_confidence: float = 30.0  # minimum confidence to act on a decision
    cycle_sec: float = 15.0  # orchestrator tick interval
    symbols: List[str] = field(default_factory=lambda: ["BTC/USDT", "ETH/USDT"])
    paper_trading: bool = True


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
class StrategyOrchestrator:
    """
    Central strategy coordinator.

    Usage
    -----
        orch = StrategyOrchestrator(config=OrchestratorConfig(...))
        orch.attach_brain(brain)

        async for snapshot in data_pipeline:
            orch.feed_snapshot(snapshot)
            decisions = await orch.tick()
            # decisions are dispatched to strategy callbacks
    """

    def __init__(
        self,
        config: Optional[OrchestratorConfig] = None,
        brain: Any = None,  # AIBrain (lazy type)
    ):
        self.config = config or OrchestratorConfig()
        self.brain = brain

        # Active strategy slots: key = "symbol:mode"
        self._slots: Dict[str, StrategySlot] = {}

        # Cooldown per symbol
        self._cooldowns: Dict[str, float] = {}

        # Pending snapshots (latest per symbol)
        self._snapshots: Dict[str, Any] = {}

        # Strategy dispatch callbacks: mode → async callable
        self._dispatch: Dict[str, Callable] = {}

        # Performance tracking
        self._decisions_history: List[Any] = []
        self._stats: Dict[str, Any] = defaultdict(float)

        # State
        self._running = False
        self._last_tick = 0.0
        self._account_equity = 10000.0  # default, should be updated externally

        log.info(
            f"[Orchestrator] Initialized: max_strategies={self.config.max_concurrent_strategies} "
            f"symbols={self.config.symbols} paper={self.config.paper_trading}"
        )

    # ------------------------------------------------------------------
    # Brain attachment
    # ------------------------------------------------------------------
    def attach_brain(self, brain):
        """Attach or replace the AI Brain."""
        self.brain = brain
        log.info("[Orchestrator] Brain attached.")

    # ------------------------------------------------------------------
    # Strategy dispatch registration
    # ------------------------------------------------------------------
    def on_decision(self, mode: str, callback: Callable):
        """
        Register a callback for when a strategy mode receives a decision.

        callback(slot: StrategySlot, decision: StrategyDecision) -> None
        """
        self._dispatch[mode] = callback
        log.info(f"[Orchestrator] Registered dispatch for mode={mode}")

    # ------------------------------------------------------------------
    # Data feed
    # ------------------------------------------------------------------
    def feed_snapshot(self, snapshot):
        """Feed a market snapshot into the orchestrator. Thread-safe for async use."""
        self._snapshots[snapshot.symbol] = snapshot

    # ------------------------------------------------------------------
    # Account state
    # ------------------------------------------------------------------
    def update_account(self, equity: float, balance: float, margin_used: float = 0.0):
        """Update account state from the exchange or paper engine."""
        self._account_equity = equity
        self._stats["equity"] = equity
        self._stats["balance"] = balance
        self._stats["margin_used"] = margin_used

    # ------------------------------------------------------------------
    # Main tick
    # ------------------------------------------------------------------
    async def tick(self) -> Dict[str, Any]:
        """
        One orchestrator tick:
          1. Process all pending snapshots through the Brain.
          2. Dispatch decisions to registered strategy callbacks.
          3. Enforce risk limits and cooldowns.

        Returns a summary dict.
        """
        now = time.time()

        # Rate-limit ticks
        if self._last_tick and (now - self._last_tick) < self.config.cycle_sec:
            return {"tick": "throttled", "decisions": 0}
        self._last_tick = now

        if not self._snapshots:
            return {"tick": "no_data", "decisions": 0}

        if self.brain is None:
            log.warning("[Orchestrator] No brain attached — skipping tick.")
            return {"tick": "no_brain", "decisions": 0}

        # Gather snapshots for symbols we care about
        symbols_to_analyze = [
            s for s in self.config.symbols if s in self._snapshots
        ]
        if not symbols_to_analyze:
            return {"tick": "no_matching_symbols", "decisions": 0}

        snapshots = [self._snapshots[s] for s in symbols_to_analyze]

        # Get decisions from the Brain
        AIBrain, _, _, StrategyDecision, StrategyMode, _ = _brain_types()
        decisions = await self.brain.decide_batch(snapshots)

        dispatched = 0
        for symbol, decision in decisions.items():
            if self._dispatch_decision(decision, now):
                dispatched += 1

        # Clean expired cooldowns
        expired = [k for k, v in self._cooldowns.items() if now >= v]
        for k in expired:
            del self._cooldowns[k]

        # Update stats
        self._stats["tick_count"] += 1
        self._stats["total_decisions"] += dispatched

        return {
            "tick": "ok",
            "decisions": dispatched,
            "modes": list(set(d.recommended_mode.value for d in decisions.values())),
            "equity": self._account_equity,
        }

    def _dispatch_decision(self, decision, now: float) -> bool:
        """Route a single decision. Returns True if dispatched."""
        symbol = decision.symbol
        mode = decision.recommended_mode.value

        # --- Pre-dispatch checks ---

        # 1. Confidence floor
        if decision.confidence < self.config.min_confidence and mode != "IDLE":
            log.debug(
                f"[Orch] {symbol} confidence {decision.confidence:.0f} < "
                f"{self.config.min_confidence:.0f} — skipping."
            )
            return False

        # 2. IDLE → always clear slot
        if mode == "IDLE":
            self._clear_slot(symbol)
            return False

        # 3. Cooldown check
        if symbol in self._cooldowns and now < self._cooldowns[symbol]:
            log.debug(f"[Orch] {symbol} in cooldown until {self._cooldowns[symbol]:.0f}")
            return False

        # 4. Max concurrent strategies
        if len(self._slots) >= self.config.max_concurrent_strategies:
            log.debug(f"[Orch] Max concurrent strategies ({self.config.max_concurrent_strategies}) reached.")
            return False

        # 5. Exposure check
        total_exposure = sum(s.position_size_usdt for s in self._slots.values())
        new_exposure = decision.position_size_pct * self._account_equity
        if total_exposure + new_exposure > self._account_equity * self.config.max_total_exposure_pct:
            log.debug(f"[Orch] {symbol} exposure limit reached.")
            return False

        # --- Create / update slot ---
        slot = StrategySlot(
            mode=mode,
            symbol=symbol,
            status=StrategyStatus.ACTIVE,
            entry_time=now,
            entry_price=sum(decision.entry_price_zone) / 2 if decision.entry_price_zone[0] > 0 else 0.0,
            position_size_usdt=new_exposure,
            last_decision=decision,
        )

        key = f"{symbol}:{mode}"
        self._slots[key] = slot

        # --- Dispatch to registered callback ---
        if mode in self._dispatch:
            try:
                cb = self._dispatch[mode]
                if asyncio.iscoroutinefunction(cb):
                    asyncio.create_task(cb(slot, decision))
                else:
                    cb(slot, decision)
            except Exception as e:
                log.error(f"[Orch] Dispatch error for {mode}: {type(e).__name__}: {e}")
        else:
            log.warning(f"[Orch] No dispatch registered for mode={mode}")

        # Set cooldown
        self._cooldowns[symbol] = now + self.config.cooldown_sec

        # Record
        self._decisions_history.append(decision)
        if len(self._decisions_history) > 1000:
            self._decisions_history = self._decisions_history[-500:]

        log.info(
            f"[Orch] DISPATCH {symbol} → {mode} dir={decision.direction.value} "
            f"conf={decision.confidence:.0f} size={new_exposure:.2f} USDT"
        )
        return True

    # ------------------------------------------------------------------
    # Slot management
    # ------------------------------------------------------------------
    def _clear_slot(self, symbol: str):
        """Remove all slots for a symbol (e.g. on IDLE)."""
        to_remove = [k for k, s in self._slots.items() if s.symbol == symbol]
        for k in to_remove:
            log.info(f"[Orch] Clearing slot {k}")
            del self._slots[k]

    def get_active_slots(self) -> Dict[str, StrategySlot]:
        """Return currently active strategy slots."""
        return {k: v for k, v in self._slots.items() if v.status == StrategyStatus.ACTIVE}

    def get_total_exposure(self) -> float:
        """Total USDT exposure across all active slots."""
        return sum(s.position_size_usdt for s in self.get_active_slots().values())

    def pause_symbol(self, symbol: str):
        """Pause all strategies for a symbol."""
        for s in self._slots.values():
            if s.symbol == symbol:
                s.status = StrategyStatus.PAUSED
        log.info(f"[Orch] Paused {symbol}")

    def resume_symbol(self, symbol: str):
        """Resume all strategies for a symbol."""
        for s in self._slots.values():
            if s.symbol == symbol:
                s.status = StrategyStatus.ACTIVE
        log.info(f"[Orch] Resumed {symbol}")

    # ------------------------------------------------------------------
    # Stats & status
    # ------------------------------------------------------------------
    def get_status(self) -> Dict[str, Any]:
        """Get orchestrator operational status."""
        return {
            "running": self._running,
            "equity": self._account_equity,
            "total_exposure": self.get_total_exposure(),
            "active_slots": len(self.get_active_slots()),
            "cooldowns": len(self._cooldowns),
            "decisions_pending": len(self._snapshots),
            "stats": dict(self._stats),
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self):
        """Start the orchestrator main loop."""
        self._running = True
        log.info("[Orchestrator] Starting main loop...")
        while self._running:
            try:
                summary = await self.tick()
                if summary.get("decisions", 0) > 0:
                    log.debug(f"[Orch] Tick: {summary}")
            except Exception as e:
                log.error(f"[Orch] Tick error: {type(e).__name__}: {e}")
            await asyncio.sleep(self.config.cycle_sec)

    async def stop(self):
        """Gracefully stop the orchestrator."""
        self._running = False
        log.info("[Orchestrator] Stopped.")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def create_orchestrator(
    symbols: Optional[List[str]] = None,
    max_strategies: int = 4,
    paper_trading: bool = True,
    cycle_sec: float = 15.0,
    brain: Any = None,
) -> StrategyOrchestrator:
    """
    Convenience factory for creating a configured orchestrator.
    """
    config = OrchestratorConfig(
        symbols=symbols or ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
        max_concurrent_strategies=max_strategies,
        paper_trading=paper_trading,
        cycle_sec=cycle_sec,
    )
    return StrategyOrchestrator(config=config, brain=brain)

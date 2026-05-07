"""
Safety system for Shark Quantitative Robot.

Provides circuit breakers with auto-reset timers and a central SafetyManager
with 5 priority levels to protect against cascading failures in trading operations.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Priority levels
# ---------------------------------------------------------------------------

class Priority(IntEnum):
    """Safety priority levels — lower numeric value = higher severity."""

    CRITICAL = 0   # System‑wide halt (exchange down, auth failure)
    HIGH = 1       # Per‑symbol or per‑strategy halt
    MEDIUM = 2     # Degraded operations
    LOW = 3        # Minor issues
    INFO = 4       # Observability only

    def __str__(self) -> str:
        return self.name


# ---------------------------------------------------------------------------
# Circuit breaker state
# ---------------------------------------------------------------------------

@dataclass
class CircuitBreaker:
    """Tracks failures within a sliding window; trips when threshold is exceeded.

    Attributes
    ----------
    name : str
        Human‑readable identifier (e.g. ``"binance_spot_orders"``).
    threshold : int
        Number of failures allowed inside *window_seconds* before tripping.
    window_seconds : float
        Sliding window duration in seconds.
    triggered : bool
        Whether the breaker is currently open (tripped).
    trigger_time : float or None
        Monotonic timestamp when the breaker last tripped.
    auto_reset_seconds : float
        Seconds after *trigger_time* that the breaker auto‑resets to half‑open.
    failure_count : int
        Current failures inside the active window.
    window_start : float or None
        Monotonic timestamp when the current window began.
    priority : Priority
        Severity level assigned at registration time.
    half_open : bool
        ``True`` after auto‑reset while the breaker waits for a probe success.
    """

    name: str
    threshold: int
    window_seconds: float
    triggered: bool = False
    trigger_time: Optional[float] = None
    auto_reset_seconds: float = 60.0
    failure_count: int = 0
    window_start: Optional[float] = None
    priority: Priority = Priority.MEDIUM
    half_open: bool = False

    # Optional callbacks
    on_trip: Optional[Callable[[str], None]] = field(default=None, repr=False)
    on_reset: Optional[Callable[[str], None]] = field(default=None, repr=False)

    @property
    def is_open(self) -> bool:
        """Shorthand for *triggered* (commonly used terminology)."""
        return self.triggered

    @property
    def remaining_block_seconds(self) -> float:
        """Seconds remaining before auto‑reset (0 if not triggered)."""
        if not self.triggered or self.trigger_time is None:
            return 0.0
        elapsed = time.monotonic() - self.trigger_time
        return max(0.0, self.auto_reset_seconds - elapsed)

    def record_failure(self, now: Optional[float] = None) -> bool:
        """Record a failure.  Returns ``True`` if the breaker just tripped."""
        if now is None:
            now = time.monotonic()

        # Advance or initialise the sliding window
        if self.window_start is None or (now - self.window_start) > self.window_seconds:
            self.window_start = now
            self.failure_count = 0

        self.failure_count += 1

        if not self.triggered and self.failure_count >= self.threshold:
            self.triggered = True
            self.trigger_time = now
            self.half_open = False
            if self.on_trip:
                try:
                    self.on_trip(self.name)
                except Exception:
                    logger.exception("CircuitBreaker.on_trip callback failed")
            return True
        return False

    def record_success(self) -> None:
        """Record a success.  In half‑open state this fully resets the breaker."""
        if self.half_open:
            self.reset()

    def reset(self) -> None:
        """Force‑reset the breaker to closed state."""
        was_triggered = self.triggered
        self.triggered = False
        self.trigger_time = None
        self.failure_count = 0
        self.window_start = None
        self.half_open = False
        if was_triggered and self.on_reset:
            try:
                self.on_reset(self.name)
            except Exception:
                logger.exception("CircuitBreaker.on_reset callback failed")

    def auto_reset(self, now: Optional[float] = None) -> bool:
        """Check whether auto‑reset is due and transition to half‑open if so.

        Returns ``True`` if the breaker transitioned from open → half‑open.
        """
        if not self.triggered:
            return False
        if now is None:
            now = time.monotonic()
        if self.trigger_time is None:
            return False
        if (now - self.trigger_time) >= self.auto_reset_seconds:
            self.half_open = True
            return True
        return False


# ---------------------------------------------------------------------------
# SafetyManager
# ---------------------------------------------------------------------------

class SafetyManager:
    """Central registry of circuit breakers grouped by priority.

    Parameters
    ----------
    poll_interval : float
        Seconds between auto‑reset checks in the background monitor task.
    """

    def __init__(self, poll_interval: float = 1.0) -> None:
        self.poll_interval = poll_interval
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._lock = asyncio.Lock()
        self._monitor_task: Optional[asyncio.Task[None]] = None
        self._running = False

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    async def register(
        self,
        name: str,
        threshold: int,
        window_seconds: float = 60.0,
        auto_reset_seconds: float = 120.0,
        priority: Priority = Priority.MEDIUM,
        on_trip: Optional[Callable[[str], None]] = None,
        on_reset: Optional[Callable[[str], None]] = None,
    ) -> CircuitBreaker:
        """Register a new circuit breaker (or return the existing one).

        Returns the ``CircuitBreaker`` instance.
        """
        async with self._lock:
            if name in self._breakers:
                return self._breakers[name]
            breaker = CircuitBreaker(
                name=name,
                threshold=threshold,
                window_seconds=window_seconds,
                auto_reset_seconds=auto_reset_seconds,
                priority=priority,
                on_trip=on_trip,
                on_reset=on_reset,
            )
            self._breakers[name] = breaker
            logger.info(
                "Registered breaker '%s' priority=%s threshold=%d window=%.1fs reset=%.1fs",
                name, priority, threshold, window_seconds, auto_reset_seconds,
            )
            return breaker

    async def unregister(self, name: str) -> bool:
        """Remove a breaker.  Returns ``True`` if it existed."""
        async with self._lock:
            if name in self._breakers:
                del self._breakers[name]
                return True
            return False

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    async def get(self, name: str) -> Optional[CircuitBreaker]:
        """Return the named breaker or ``None``."""
        async with self._lock:
            return self._breakers.get(name)

    async def list_all(self) -> List[CircuitBreaker]:
        """Return all registered breakers."""
        async with self._lock:
            return list(self._breakers.values())

    async def list_by_priority(self, priority: Priority) -> List[CircuitBreaker]:
        """Return breakers matching *priority*."""
        async with self._lock:
            return [b for b in self._breakers.values() if b.priority == priority]

    async def list_triggered(self) -> List[CircuitBreaker]:
        """Return all breakers currently triggered or half‑open."""
        async with self._lock:
            return [b for b in self._breakers.values() if b.triggered or b.half_open]

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    async def record_failure(self, name: str) -> bool:
        """Record a failure against *name*.  Returns ``True`` if tripped."""
        async with self._lock:
            breaker = self._breakers.get(name)
            if breaker is None:
                logger.warning("record_failure on unknown breaker '%s'", name)
                return False
            tripped = breaker.record_failure()
            if tripped:
                logger.warning(
                    "BREAKER TRIPPED: '%s' threshold=%d (failures=%d in %.1fs)",
                    name, breaker.threshold, breaker.failure_count, breaker.window_seconds,
                )
            return tripped

    async def record_success(self, name: str) -> None:
        """Record a success.  Resets a half‑open breaker."""
        async with self._lock:
            breaker = self._breakers.get(name)
            if breaker is not None:
                breaker.record_success()

    async def is_triggered(self, name: str) -> bool:
        """Check whether *name* is currently triggered (open)."""
        async with self._lock:
            breaker = self._breakers.get(name)
            return breaker is not None and breaker.triggered

    async def is_blocked(self, name: str) -> bool:
        """Check whether *name* is blocking operations (triggered and not half‑open)."""
        async with self._lock:
            breaker = self._breakers.get(name)
            if breaker is None:
                return False
            return breaker.triggered and not breaker.half_open

    async def reset(self, name: str) -> bool:
        """Force‑reset breaker *name*.  Returns ``True`` if it existed and was triggered."""
        async with self._lock:
            breaker = self._breakers.get(name)
            if breaker is None:
                return False
            was_triggered = breaker.triggered
            breaker.reset()
            if was_triggered:
                logger.info("Breaker '%s' manually reset", name)
            return was_triggered

    async def reset_all(self) -> int:
        """Reset every registered breaker.  Returns count of resets performed."""
        async with self._lock:
            count = 0
            for breaker in self._breakers.values():
                if breaker.triggered:
                    breaker.reset()
                    count += 1
            return count

    async def reset_by_priority(self, priority: Priority) -> int:
        """Reset all breakers at or above *priority*."""
        async with self._lock:
            count = 0
            for breaker in self._breakers.values():
                if breaker.triggered and breaker.priority <= priority:
                    breaker.reset()
                    count += 1
            return count

    # ------------------------------------------------------------------
    # Global halt
    # ------------------------------------------------------------------

    async def is_system_halted(self) -> bool:
        """``True`` if any CRITICAL breaker is triggered (system‑wide halt)."""
        async with self._lock:
            return any(
                b.triggered and b.priority == Priority.CRITICAL
                for b in self._breakers.values()
            )

    # ------------------------------------------------------------------
    # Background monitor (auto‑reset)
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Launch the background auto‑reset monitor."""
        if self._running:
            return
        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("SafetyManager monitor started (poll=%.1fs)", self.poll_interval)

    async def stop(self) -> None:
        """Shut down the background monitor."""
        self._running = False
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        logger.info("SafetyManager monitor stopped")

    async def _monitor_loop(self) -> None:
        """Periodically check each triggered breaker for auto‑reset eligibility."""
        while self._running:
            try:
                await asyncio.sleep(self.poll_interval)
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("SafetyManager monitor error")

    async def _tick(self) -> None:
        """Single inspection pass."""
        async with self._lock:
            for breaker in self._breakers.values():
                if breaker.triggered and not breaker.half_open:
                    if breaker.auto_reset():
                        logger.info(
                            "Breaker '%s' auto‑reset → half‑open (will fully reset on next success)",
                            breaker.name,
                        )


# ---------------------------------------------------------------------------
# Convenience helpers for strategy / order execution
# ---------------------------------------------------------------------------

async def check_breakers(
    manager: SafetyManager,
    *names: str,
) -> Optional[str]:
    """Raise‑free check: return the *first* blocked breaker name, or ``None``."""
    for name in names:
        if await manager.is_blocked(name):
            return name
    return None


async def require_breakers(
    manager: SafetyManager,
    *names: str,
) -> None:
    """Assert that every named breaker is closed; raise ``RuntimeError`` otherwise."""
    for name in names:
        if await manager.is_blocked(name):
            raise RuntimeError(f"Circuit breaker '{name}' is open — operation blocked")

"""
Rate limiter for Shark Quantitative Robot.

Provides async‑safe token‑bucket rate limiting with burst support and
optional integration with the SafetyManager circuit breakers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class RateLimitConfig:
    """Immutable configuration for a single rate‑limited resource.

    Attributes
    ----------
    rate : float
        Sustained tokens per second (e.g. 10.0 = 10 ops/s).
    burst : int
        Maximum instantaneous burst size (tokens accrued during idle time).
        Must be >= 1.
    name : str
        Human‑readable identifier (e.g. ``"binance_api"``).
    """

    rate: float
    burst: int = 1
    name: str = ""

    def __post_init__(self) -> None:
        if self.rate <= 0:
            raise ValueError(f"rate must be > 0, got {self.rate}")
        if self.burst < 1:
            raise ValueError(f"burst must be >= 1, got {self.burst}")


# ---------------------------------------------------------------------------
# Internal token bucket
# ---------------------------------------------------------------------------

@dataclass
class _Bucket:
    """Per‑resource token bucket state."""

    config: RateLimitConfig
    tokens: float = field(init=False)
    last_refill: float = field(init=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        self.tokens = float(self.config.burst)
        self.last_refill = time.monotonic()

    def _refill(self, now: float) -> None:
        elapsed = now - self.last_refill
        if elapsed > 0:
            self.tokens = min(
                float(self.config.burst),
                self.tokens + elapsed * self.config.rate,
            )
        self.last_refill = now

    async def acquire(self, tokens: float = 1.0, timeout: Optional[float] = None) -> bool:
        """Acquire *tokens*, waiting up to *timeout* seconds if necessary.

        Returns ``True`` on success, ``False`` on timeout.
        ``timeout=None`` means wait indefinitely.
        """
        deadline: Optional[float] = None
        if timeout is not None:
            deadline = time.monotonic() + timeout

        while True:
            async with self.lock:
                now = time.monotonic()
                self._refill(now)
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return True
                # Calculate wait time needed for the missing tokens
                missing = tokens - self.tokens
                wait = missing / self.config.rate
                # Add a tiny epsilon to avoid spinning on rounding errors
                wait = max(wait, 0.001)

            if deadline is not None and time.monotonic() + wait > deadline:
                return False
            await asyncio.sleep(wait)

    async def try_acquire(self, tokens: float = 1.0) -> bool:
        """Non‑blocking acquisition attempt.  Returns ``True`` on success."""
        async with self.lock:
            now = time.monotonic()
            self._refill(now)
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False

    def peek(self) -> float:
        """Return current token count (approx; does not lock)."""
        return self.tokens


# ---------------------------------------------------------------------------
# Rate limiter registry
# ---------------------------------------------------------------------------

class RateLimiter:
    """Async token‑bucket rate limiter managing multiple named resources.

    Parameters
    ----------
    safety_manager : SafetyManager or None
        Optional ``SafetyManager`` instance.  When provided every
        ``acquire()`` / ``try_acquire()`` call first checks whether the
        corresponding circuit breaker is open and fails immediately if so.

    Example
    -------
    >>> limiter = RateLimiter()
    >>> limiter.configure(RateLimitConfig(rate=5.0, burst=3, name="api"))
    >>> async with limiter.acquire("api"):
    ...     await make_api_call()
    """

    def __init__(self, safety_manager=None) -> None:
        self._buckets: Dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()
        # Weak reference to avoid circular import; SafetyManager is optional.
        self._safety_manager = safety_manager

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    async def configure(self, config: RateLimitConfig) -> None:
        """Register (or replace) a rate limit for *config.name*."""
        if not config.name:
            raise ValueError("RateLimitConfig.name is required")
        async with self._lock:
            self._buckets[config.name] = _Bucket(config=config)
            logger.info(
                "Rate limiter configured: '%s' rate=%.1f/s burst=%d",
                config.name, config.rate, config.burst,
            )

    async def remove(self, name: str) -> bool:
        """Remove a rate limit by name.  Returns ``True`` if it existed."""
        async with self._lock:
            if name in self._buckets:
                del self._buckets[name]
                return True
            return False

    async def get_configs(self) -> Dict[str, RateLimitConfig]:
        """Return a snapshot of all active configurations."""
        async with self._lock:
            return {name: bucket.config for name, bucket in self._buckets.items()}

    # ------------------------------------------------------------------
    # Acquire – blocking
    # ------------------------------------------------------------------

    async def acquire(
        self,
        name: str,
        tokens: float = 1.0,
        timeout: Optional[float] = None,
    ) -> bool:
        """Acquire *tokens* from bucket *name*, waiting up to *timeout* seconds.

        Returns ``True`` on success, ``False`` on timeout or if blocked by
        the safety circuit breaker.
        """
        # Safety check
        if self._safety_manager is not None:
            if await self._safety_manager.is_blocked(name):
                logger.warning("RateLimiter.acquire('%s') blocked by circuit breaker", name)
                return False

        bucket = await self._get_bucket(name)
        if bucket is None:
            logger.error("RateLimiter.acquire('%s'): no such bucket configured", name)
            return False
        return await bucket.acquire(tokens=tokens, timeout=timeout)

    # ------------------------------------------------------------------
    # Acquire – non‑blocking
    # ------------------------------------------------------------------

    async def try_acquire(self, name: str, tokens: float = 1.0) -> bool:
        """Non‑blocking acquisition attempt.  Returns ``True`` on success."""
        if self._safety_manager is not None:
            if await self._safety_manager.is_blocked(name):
                return False

        bucket = await self._get_bucket(name)
        if bucket is None:
            return False
        return await bucket.try_acquire(tokens=tokens)

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def context(
        self,
        name: str,
        tokens: float = 1.0,
        timeout: Optional[float] = None,
    ) -> AsyncIterator[bool]:
        """Async context manager that acquires *tokens* on entry.

        Yields ``True`` if acquisition succeeded, ``False`` otherwise.
        The caller should check the value before proceeding.

        Example
        -------
        >>> async with limiter.context("api") as ok:
        ...     if ok:
        ...         await make_api_call()
        """
        acquired = await self.acquire(name, tokens=tokens, timeout=timeout)
        try:
            yield acquired
        finally:
            pass  # tokens are consumed on acquisition; nothing to release

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    async def peek(self, name: str) -> Optional[float]:
        """Return the current token count for *name*, or ``None`` if unknown."""
        bucket = await self._get_bucket(name)
        return bucket.peek() if bucket else None

    async def stats(self) -> Dict[str, dict]:
        """Return a snapshot of all buckets with current token counts."""
        async with self._lock:
            return {
                name: {
                    "tokens": bucket.peek(),
                    "rate": bucket.config.rate,
                    "burst": bucket.config.burst,
                }
                for name, bucket in self._buckets.items()
            }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _get_bucket(self, name: str) -> Optional[_Bucket]:
        async with self._lock:
            return self._buckets.get(name)


# ---------------------------------------------------------------------------
# Global / shared limiter (convenience)
# ---------------------------------------------------------------------------

_default_limiter: Optional[RateLimiter] = None
_default_lock = asyncio.Lock()


async def get_default_limiter() -> RateLimiter:
    """Return (and lazily create) a process‑wide shared ``RateLimiter``."""
    global _default_limiter
    if _default_limiter is None:
        async with _default_lock:
            if _default_limiter is None:
                _default_limiter = RateLimiter()
    return _default_limiter


# ---------------------------------------------------------------------------
# Convenience top‑level functions that use the default limiter
# ---------------------------------------------------------------------------

async def configure_default(config: RateLimitConfig) -> None:
    limiter = await get_default_limiter()
    await limiter.configure(config)


@asynccontextmanager
async def rate_limit(
    name: str,
    tokens: float = 1.0,
    timeout: Optional[float] = None,
) -> AsyncIterator[bool]:
    """Context manager using the default limiter.

    Example
    -------
    >>> async with rate_limit("binance_api") as ok:
    ...     if ok:
    ...         await call_binance()
    """
    limiter = await get_default_limiter()
    async with limiter.context(name, tokens=tokens, timeout=timeout) as ok:
        yield ok

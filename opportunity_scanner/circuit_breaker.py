"""
Circuit breaker.

Retry (in cache.py) handles a single transient blip — a request that
fails once and succeeds on the second try. Circuit breaker handles the
different problem of a provider being genuinely DOWN for an extended
period: without this, every scan cycle would retry 3x against a dead
endpoint, burning time and rate-limit budget for nothing. Once a
provider has failed enough times in a row, the breaker "opens" and fails
fast (no network call at all) for a cooldown period, then allows a single
test request through to see if the provider has recovered.

States:
  CLOSED     — normal operation, requests go through
  OPEN       — provider considered down, requests fail immediately (no network call)
  HALF_OPEN  — cooldown elapsed, next request is a test; success closes the
               breaker again, failure re-opens it
"""

from __future__ import annotations
import time
import asyncio
import logging
from enum import Enum
from dataclasses import dataclass, field
from typing import Callable, TypeVar, Awaitable, Dict

logger = logging.getLogger("opportunity_scanner.circuit_breaker")

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when a call is attempted while the circuit is OPEN — the caller
    should treat this the same as any other provider failure (fall back,
    return None, etc.), it's just failing fast instead of waiting on a
    network timeout first."""
    pass


@dataclass
class CircuitBreaker:
    name: str                                   # e.g. "bybit", "coinglass" — for logging
    failure_threshold: int = 5                    # consecutive failures before opening
    cooldown_seconds: float = 60.0                  # how long to stay OPEN before testing again
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _consecutive_failures: int = field(default=0, init=False)
    _opened_at: float = field(default=0.0, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN and (time.monotonic() - self._opened_at) >= self.cooldown_seconds:
            return CircuitState.HALF_OPEN
        return self._state

    async def call(self, fn: Callable[[], Awaitable[T]], ignore_exceptions: tuple = ()) -> T:
        """
        ignore_exceptions: exception types that still propagate to the
        caller as normal, but do NOT count toward tripping the breaker.
        For a symbol simply not being listed on a given exchange (ccxt's
        BadSymbol, for example) — that's an expected, routine condition
        for any curated exchange (not every exchange lists every asset),
        and says nothing about whether the exchange itself is healthy.
        Treating it as a failure was a real, confirmed bug: discovering
        even 3 coins in a row that happen not to be listed on Hyperliquid
        was enough to trip the breaker for its full cooldown, taking
        down OHLCV for every OTHER coin in that window — including
        genuinely major, well-listed coins that had nothing to do with
        the missing-symbol coins that tripped it.
        """
        current = self.state

        if current == CircuitState.OPEN:
            raise CircuitOpenError(
                f"Circuit for '{self.name}' is OPEN — {self._consecutive_failures} consecutive failures, "
                f"cooling down for {self.cooldown_seconds - (time.monotonic() - self._opened_at):.0f}s more"
            )

        try:
            result = await fn()
        except ignore_exceptions:
            raise
        except Exception:
            await self._record_failure()
            raise
        else:
            await self._record_success()
            return result

    async def _record_failure(self):
        async with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                if self._state != CircuitState.OPEN:
                    logger.warning(
                        f"Circuit for '{self.name}' OPENING after {self._consecutive_failures} consecutive failures"
                    )
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()

    async def _record_success(self):
        async with self._lock:
            if self._state != CircuitState.CLOSED:
                logger.info(f"Circuit for '{self.name}' CLOSING — provider recovered")
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0

    def status(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "consecutive_failures": self._consecutive_failures,
        }


class CircuitBreakerRegistry:
    """One breaker per named provider, created lazily. Import `breakers`
    (the module-level instance below) rather than constructing your own —
    a shared registry is what lets `/health`-style endpoints report on
    every provider's circuit state in one place."""

    def __init__(self):
        self._breakers: Dict[str, CircuitBreaker] = {}

    def get(self, name: str, failure_threshold: int = 5, cooldown_seconds: float = 60.0) -> CircuitBreaker:
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(name, failure_threshold, cooldown_seconds)
        return self._breakers[name]

    def status_all(self) -> list[dict]:
        return [b.status() for b in self._breakers.values()]


breakers = CircuitBreakerRegistry()

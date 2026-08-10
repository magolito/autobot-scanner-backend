"""
Shared caching + retry infrastructure for all data sources.

Every external call (Bybit, LunarCrush, Whale Alert) goes through
`@with_retry` for resilience against transient failures, and reads that
don't need to be second-fresh go through `TTLCache` to stay under rate
limits. This lives in one file so the retry/backoff policy is consistent
across every data source rather than each one reinventing it slightly
differently.
"""

from __future__ import annotations
import asyncio
import json
import os
import time
from typing import Any, Callable, Dict, Optional, TypeVar, Awaitable
from tenacity import (
    retry, stop_after_attempt, wait_exponential,
    retry_if_exception_type, before_sleep_log,
)
import logging

logger = logging.getLogger("opportunity_scanner.cache")

T = TypeVar("T")


class TTLCache:
    """
    Minimal async-safe in-memory TTL cache. Swap for Redis if you need
    this shared across multiple processes/instances — the interface
    (get/set, string keys, JSON-serializable-ish values) is deliberately
    Redis-compatible so that swap is mechanical, not a rewrite.
    """

    def __init__(self):
        self._store: Dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.monotonic() > expires_at:
                del self._store[key]
                return None
            return value

    async def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        async with self._lock:
            self._store[key] = (time.monotonic() + ttl_seconds, value)

    async def get_or_fetch(self, key: str, ttl_seconds: float, fetch_fn: Callable[[], Awaitable[T]]) -> T:
        cached = await self.get(key)
        if cached is not None:
            return cached
        value = await fetch_fn()
        if value is not None:
            await self.set(key, value, ttl_seconds)
        return value

    async def invalidate(self, key: str) -> None:
        async with self._lock:
            self._store.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()


# Shared cache instances are created below, after `make_cache` is defined.


class RedisCache:
    """
    Same interface as TTLCache (get/set/get_or_fetch/invalidate/clear) so
    swapping one for the other is mechanical — nothing calling this class
    needs to change. Use this when you need cache state SHARED across
    multiple process instances (e.g. the API server and the scheduler
    worker both hitting the same cache) — TTLCache is per-process memory,
    which is fine for a single instance but doesn't help across instances.

    Values are JSON-serialized. If you're caching something that isn't
    JSON-serializable (a pandas DataFrame, say), serialize it yourself
    before calling `set`, or stick with TTLCache for that particular cache.
    """

    def __init__(self, redis_url: str, key_prefix: str = "oscanner:"):
        import redis.asyncio as aioredis
        self._redis = aioredis.from_url(redis_url, decode_responses=True)
        self._prefix = key_prefix

    def _k(self, key: str) -> str:
        return f"{self._prefix}{key}"

    async def get(self, key: str) -> Optional[Any]:
        raw = await self._redis.get(self._k(key))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw  # not JSON — return as-is rather than erroring

    async def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        try:
            payload = json.dumps(value)
        except TypeError:
            # not JSON-serializable — caller should have serialized it themselves.
            # Fail loudly here rather than silently caching nothing.
            raise TypeError(
                f"RedisCache.set: value for key '{key}' isn't JSON-serializable. "
                f"Serialize it before calling set(), or use TTLCache for this data."
            )
        await self._redis.set(self._k(key), payload, ex=int(ttl_seconds))

    async def get_or_fetch(self, key: str, ttl_seconds: float, fetch_fn: Callable[[], Awaitable[T]]) -> T:
        cached = await self.get(key)
        if cached is not None:
            return cached
        value = await fetch_fn()
        if value is not None:
            await self.set(key, value, ttl_seconds)
        return value

    async def invalidate(self, key: str) -> None:
        await self._redis.delete(self._k(key))

    async def clear(self) -> None:
        # scan + delete rather than FLUSHDB, since this Redis instance may
        # be shared with other prefixed keyspaces
        async for k in self._redis.scan_iter(f"{self._prefix}*"):
            await self._redis.delete(k)

    async def ping(self) -> bool:
        try:
            return await self._redis.ping()
        except Exception:
            return False


def make_cache(name: str):
    """
    Factory: returns a RedisCache if REDIS_URL is set in the environment
    AND redis is importable, otherwise falls back to an in-memory TTLCache.
    This is what production code should call rather than constructing
    TTLCache/RedisCache directly, so the Redis-vs-memory decision lives in
    one place.
    """
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        try:
            return RedisCache(redis_url, key_prefix=f"oscanner:{name}:")
        except ImportError:
            logger.warning(f"REDIS_URL set but redis package unavailable — falling back to in-memory cache for '{name}'")
    return TTLCache()


# Shared cache instances — one per data domain, so a slow LunarCrush call
# doesn't evict a Bybit entry and vice versa. Redis-backed if REDIS_URL is
# set, in-memory otherwise — see make_cache() above.
exchange_cache = make_cache("exchange")
social_cache = make_cache("social")
whale_cache = make_cache("whale")


def with_retry(max_attempts: int = 3, min_wait: float = 1.0, max_wait: float = 10.0):
    """
    Standard retry policy for flaky network calls: exponential backoff,
    up to `max_attempts`, retrying on connection/timeout-shaped exceptions
    but NOT on e.g. auth errors (4xx from a bad key should fail fast and
    loud, not retry three times and waste the rate-limit budget).
    """
    import httpx

    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=min_wait, max=max_wait),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError, httpx.ReadTimeout)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )

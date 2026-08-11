"""
CoinGecko Derivatives — free, no key, US-friendly aggregated source for
perpetual price/funding/open-interest. Confirmed via CoinGecko's public
documentation before building against it (real endpoint, real field
names): GET /api/v3/derivatives returns a flat list of every tracked
perpetual contract across every exchange CoinGecko covers in ONE call —
deliberately used instead of the per-exchange endpoint
(/derivatives/exchanges/{id}), since a single flat list lets us filter
for a given base symbol across all venues without N calls.

This is Tier 2 in the new priority chain: Hyperliquid first (no
US restriction, most accurate for what AutoBot itself trades), this
second when Hyperliquid doesn't have a contract or is down, Bybit last
and optional (see multi_exchange_oi.py for how the chain is assembled).
"""

from __future__ import annotations
from typing import Optional
import httpx
from pydantic import BaseModel

from ..cache import make_cache, with_retry
from ..circuit_breaker import breakers, CircuitOpenError

COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"

coingecko_derivatives_cache = make_cache("coingecko_derivatives")


class CoinGeckoDerivativesSnapshot(BaseModel):
    price: Optional[float] = None
    funding_rate: Optional[float] = None
    open_interest_usd: Optional[float] = None
    exchange_name: Optional[str] = None   # which venue CoinGecko's data came from, for the source-logging requirement


class CoinGeckoDerivativesProvider:
    def __init__(self, api_key: Optional[str] = None, cache_ttl_seconds: float = 60, failure_threshold: int = 4, cooldown_seconds: float = 120):
        self._http = httpx.AsyncClient(
            base_url=COINGECKO_BASE_URL,
            headers={"x-cg-demo-api-key": api_key} if api_key else {},
            timeout=15.0,
        )
        self._breaker = breakers.get("coingecko_derivatives", failure_threshold=failure_threshold, cooldown_seconds=cooldown_seconds)
        self.cache_ttl_seconds = cache_ttl_seconds

    async def close(self):
        await self._http.aclose()

    @with_retry(max_attempts=3)
    async def _get_all_derivatives(self) -> list:
        resp = await self._http.get("/derivatives")
        resp.raise_for_status()
        return resp.json()

    async def _fetch_all_cached(self) -> Optional[list]:
        """The flat /derivatives list is the same for every symbol in a
        given cache window — one shared cache entry, not one per base
        symbol, so scanning 20 coins costs one API call, not 20."""
        async def fetch():
            try:
                return await self._breaker.call(self._get_all_derivatives)
            except (CircuitOpenError, Exception) as e:  # noqa: BLE001
                print(f"[coingecko_derivatives] fetch failed: {e}")
                return None
        return await coingecko_derivatives_cache.get_or_fetch("all_derivatives", ttl_seconds=self.cache_ttl_seconds, fetch_fn=fetch)

    async def get_snapshot(self, base: str) -> Optional[CoinGeckoDerivativesSnapshot]:
        """
        Filters the flat list for contracts matching `base` (CoinGecko's
        `index_id` field, e.g. "BTC"), and picks the entry with the
        largest open interest as the representative price/funding for
        this symbol — the highest-OI venue is the most liquid, and
        therefore the most representative of "the real market", matching
        the "accurate pricing over just having some data" requirement.
        """
        all_derivatives = await self._fetch_all_cached()
        if not all_derivatives:
            return None

        matches = [d for d in all_derivatives if d.get("index_id") == base and d.get("contract_type") == "perpetual"]
        if not matches:
            return None

        def _oi(d):
            try:
                return float(d.get("open_interest") or 0)
            except (TypeError, ValueError):
                return 0.0

        best = max(matches, key=_oi)
        try:
            price = float(best["price"]) if best.get("price") is not None else None
        except (TypeError, ValueError):
            price = None
        try:
            funding_rate = float(best["funding_rate"]) if best.get("funding_rate") is not None else None
        except (TypeError, ValueError):
            funding_rate = None
        try:
            oi_usd = float(best["open_interest"]) if best.get("open_interest") is not None else None
        except (TypeError, ValueError):
            oi_usd = None

        return CoinGeckoDerivativesSnapshot(
            price=price, funding_rate=funding_rate, open_interest_usd=oi_usd,
            exchange_name=best.get("market"),
        )

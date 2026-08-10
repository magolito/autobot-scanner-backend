"""
US-compliant spot price confirmation — Coinbase and Kraken, both
explicitly serve US customers and have free, public, no-key market data
endpoints. This is Tier 3 in the priority chain: used for clean spot
price/OHLCV confirmation when neither Hyperliquid nor CoinGecko covered
a symbol, never for OI/funding (neither exchange's public API exposes
derivatives data the same accessible way).

Coinbase confirmed real endpoint: GET /products/{product_id}/ticker on
api.exchange.coinbase.com (their public Exchange API, no auth for market
data). Kraken confirmed real endpoint: GET /0/public/Ticker on
api.kraken.com — but Kraken's pair naming is genuinely inconsistent
(e.g. "XBT" not "BTC" for Bitcoin, and some pairs use an "X"/"Z" prefix
convention). Rather than guess at every symbol's Kraken code, this
ships with an explicit mapping for major coins only — an unmapped
symbol returns None from Kraken rather than a wrong guess, and the
priority chain just falls through to whatever's next.
"""

from __future__ import annotations
from typing import Optional
import httpx
from pydantic import BaseModel

from ..cache import make_cache, with_retry
from ..circuit_breaker import breakers, CircuitOpenError

COINBASE_BASE_URL = "https://api.exchange.coinbase.com"
KRAKEN_BASE_URL = "https://api.kraken.com/0/public"

us_spot_cache = make_cache("us_spot")

# Kraken's non-obvious pair codes for major coins — deliberately NOT
# guessed for anything outside this list (see module docstring).
KRAKEN_SYMBOL_MAP = {
    "BTC": "XXBTZUSD", "ETH": "XETHZUSD", "SOL": "SOLUSD", "XRP": "XXRPZUSD",
    "ADA": "ADAUSD", "DOGE": "XDGUSD", "AVAX": "AVAXUSD", "DOT": "DOTUSD",
    "LINK": "LINKUSD", "BNB": None,  # Kraken doesn't list BNB — explicitly None, not a guess
}


class SpotSnapshot(BaseModel):
    price: Optional[float] = None
    volume_24h_usd: Optional[float] = None
    source: str


class USSpotProvider:
    def __init__(self, cache_ttl_seconds: float = 20, failure_threshold: int = 5, cooldown_seconds: float = 60):
        self._coinbase_http = httpx.AsyncClient(base_url=COINBASE_BASE_URL, timeout=10.0)
        self._kraken_http = httpx.AsyncClient(base_url=KRAKEN_BASE_URL, timeout=10.0)
        self._coinbase_breaker = breakers.get("coinbase", failure_threshold=failure_threshold, cooldown_seconds=cooldown_seconds)
        self._kraken_breaker = breakers.get("kraken", failure_threshold=failure_threshold, cooldown_seconds=cooldown_seconds)
        self.cache_ttl_seconds = cache_ttl_seconds

    async def close(self):
        await self._coinbase_http.aclose()
        await self._kraken_http.aclose()

    @with_retry(max_attempts=2)
    async def _get_coinbase_ticker(self, product_id: str) -> dict:
        resp = await self._coinbase_http.get(f"/products/{product_id}/ticker")
        resp.raise_for_status()
        return resp.json()

    @with_retry(max_attempts=2)
    async def _get_kraken_ticker(self, pair: str) -> dict:
        resp = await self._kraken_http.get("/Ticker", params={"pair": pair})
        resp.raise_for_status()
        return resp.json()

    async def _fetch_coinbase(self, base: str) -> Optional[SpotSnapshot]:
        product_id = f"{base}-USD"
        cache_key = f"coinbase:{product_id}"

        async def fetch():
            try:
                data = await self._coinbase_breaker.call(lambda: self._get_coinbase_ticker(product_id))
            except (CircuitOpenError, Exception) as e:  # noqa: BLE001
                print(f"[us_spot:coinbase] fetch failed for {product_id}: {e}")
                return None
            if not data or "price" not in data:
                return None
            try:
                return SpotSnapshot(
                    price=float(data["price"]),
                    volume_24h_usd=float(data.get("volume", 0)) * float(data["price"]) if data.get("volume") else None,
                    source="coinbase",
                )
            except (TypeError, ValueError):
                return None

        return await us_spot_cache.get_or_fetch(cache_key, ttl_seconds=self.cache_ttl_seconds, fetch_fn=fetch)

    async def _fetch_kraken(self, base: str) -> Optional[SpotSnapshot]:
        pair = KRAKEN_SYMBOL_MAP.get(base)
        if not pair:
            return None  # not in our verified mapping — don't guess
        cache_key = f"kraken:{pair}"

        async def fetch():
            try:
                data = await self._kraken_breaker.call(lambda: self._get_kraken_ticker(pair))
            except (CircuitOpenError, Exception) as e:  # noqa: BLE001
                print(f"[us_spot:kraken] fetch failed for {pair}: {e}")
                return None
            result = (data or {}).get("result", {})
            ticker = result.get(pair) or next(iter(result.values()), None) if result else None
            if not ticker or "c" not in ticker:
                return None
            try:
                # Kraken's "c" field is [last_trade_price, lot_volume]
                return SpotSnapshot(price=float(ticker["c"][0]), source="kraken")
            except (TypeError, ValueError, KeyError, IndexError):
                return None

        return await us_spot_cache.get_or_fetch(cache_key, ttl_seconds=self.cache_ttl_seconds, fetch_fn=fetch)

    async def get_spot_price(self, base: str) -> Optional[SpotSnapshot]:
        """Coinbase first (broader coin coverage, more consistent symbol
        naming), Kraken as a second attempt if Coinbase doesn't have it."""
        result = await self._fetch_coinbase(base)
        if result is not None:
            return result
        return await self._fetch_kraken(base)

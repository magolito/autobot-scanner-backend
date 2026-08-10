"""
CoinGecko coin discovery — the actual fix for "the scanner only knows
about a list I hardcoded, it'll never find a coin that starts trending
tomorrow." Two real, confirmed-free, no-key endpoints:

  - GET /search/trending — CoinGecko's own trending-search ranking,
    refreshes roughly every 15 minutes, genuinely reflects what people
    are searching for RIGHT NOW (this is what would have caught
    HYPE/PUMP-style attention spikes before they made it into any
    manually curated list).
  - GET /coins/markets?order=volume_desc — coins ranked by actual 24h
    trading volume, live, not a snapshot baked into code.

Combining both gives two different, complementary signals: "what's
liquid" (volume) and "what's getting attention" (trending searches) —
a coin can show up in either without needing to already be in the
other, which is exactly the gap a static list can't close.
"""

from __future__ import annotations
from typing import List, Optional
import httpx

from ..cache import make_cache
from ..circuit_breaker import breakers, CircuitOpenError

COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"

discovery_cache = make_cache("coingecko_discovery")
# CoinGecko's own trending-search endpoint refreshes roughly every 15
# minutes per their documentation — matching that here means this cache
# is never staler than the source itself.
TRENDING_TTL_SECONDS = 900
TOP_VOLUME_TTL_SECONDS = 900


# Known stablecoins — deliberately excluded from discovery. A coin
# pegged to $1 has no meaningful "momentum" or "opportunity" signal to
# surface; including them was a real data-quality bug, not a neutral
# choice — confirmed live when USDT/USDC/USD1/USDG showed up in a real
# Trending Now scan alongside genuine trading opportunities.
KNOWN_STABLECOINS = {
    "USDT", "USDC", "USD1", "USDG", "DAI", "BUSD", "TUSD", "USDP", "GUSD",
    "USDD", "FDUSD", "PYUSD", "USDE", "FRAX", "LUSD", "USDS", "USDX",
}


class CoinGeckoDiscoveryProvider:
    def __init__(self, failure_threshold: int = 4, cooldown_seconds: float = 120):
        self._http = httpx.AsyncClient(base_url=COINGECKO_BASE_URL, timeout=15.0)
        self._breaker = breakers.get("coingecko_discovery", failure_threshold=failure_threshold, cooldown_seconds=cooldown_seconds)

    async def close(self):
        await self._http.aclose()

    async def _get_trending_raw(self) -> dict:
        resp = await self._http.get("/search/trending")
        resp.raise_for_status()
        return resp.json()

    async def get_trending_symbols(self) -> List[str]:
        """Coins currently trending in CoinGecko's own search activity —
        the "people are suddenly paying attention to this" signal."""
        async def fetch():
            try:
                data = await self._breaker.call(self._get_trending_raw)
            except (CircuitOpenError, Exception) as e:  # noqa: BLE001
                print(f"[discovery] CoinGecko trending fetch failed: {e}")
                return []
            coins = data.get("coins", [])
            symbols = []
            for entry in coins:
                item = entry.get("item", {})
                symbol = item.get("symbol")
                if symbol:
                    symbols.append(symbol.upper())
            return symbols

        result = await discovery_cache.get_or_fetch("trending", ttl_seconds=TRENDING_TTL_SECONDS, fetch_fn=fetch)
        return result or []

    async def _get_top_volume_raw(self, limit: int) -> list:
        resp = await self._http.get("/coins/markets", params={
            "vs_currency": "usd", "order": "volume_desc", "per_page": limit, "page": 1, "sparkline": "false",
        })
        resp.raise_for_status()
        return resp.json()

    async def get_top_by_volume(self, limit: int = 30) -> List[str]:
        """Coins ranked by actual current 24h trading volume — the
        "what's genuinely liquid right now" signal, live, not a fixed
        snapshot that goes stale as the market moves."""
        async def fetch():
            try:
                data = await self._breaker.call(lambda: self._get_top_volume_raw(limit))
            except (CircuitOpenError, Exception) as e:  # noqa: BLE001
                print(f"[discovery] CoinGecko top-volume fetch failed: {e}")
                return []
            return [row["symbol"].upper() for row in data if row.get("symbol")]

        result = await discovery_cache.get_or_fetch(f"top_volume:{limit}", ttl_seconds=TOP_VOLUME_TTL_SECONDS, fetch_fn=fetch)
        return result or []

    async def _get_market_cap_lookup_raw(self, top_n: int) -> list:
        resp = await self._http.get("/coins/markets", params={
            "vs_currency": "usd", "order": "market_cap_desc", "per_page": top_n, "page": 1, "sparkline": "false",
        })
        resp.raise_for_status()
        return resp.json()

    async def get_market_overview(self, top_n: int = 250) -> dict:
        """
        The single canonical fetch for CoinGecko's top-N-by-market-cap
        data -- current price, 24h volume, 24h % change, today's
        high/low, market cap, and rank. get_market_cap_lookup() below is
        a thin derived wrapper around this, not a second independent
        parser -- they used to both parse the same raw response
        independently while sharing one cache key, which meant whichever
        one ran first "won" the cache with its own shape and silently
        corrupted the other's results. Having exactly one method own the
        parsing removes that whole class of bug rather than working
        around it.

        Returns {symbol: {"price", "volume_24h_usd", "change_24h_pct",
        "high_24h", "low_24h", "market_cap_usd", "market_cap_rank"}}.
        """
        async def fetch():
            try:
                data = await self._breaker.call(lambda: self._get_market_cap_lookup_raw(top_n))
            except (CircuitOpenError, Exception) as e:  # noqa: BLE001
                print(f"[discovery] CoinGecko market overview fetch failed: {e}")
                return {}
            overview = {}
            for row in data:
                symbol = row.get("symbol")
                if not symbol:
                    continue
                overview[symbol.upper()] = {
                    "price": row.get("current_price"),
                    "volume_24h_usd": row.get("total_volume"),
                    "change_24h_pct": row.get("price_change_percentage_24h"),
                    "high_24h": row.get("high_24h"),
                    "low_24h": row.get("low_24h"),
                    "market_cap_usd": row.get("market_cap"),
                    "market_cap_rank": row.get("market_cap_rank"),
                }
            return overview

        result = await discovery_cache.get_or_fetch(f"market_overview:{top_n}", ttl_seconds=TOP_VOLUME_TTL_SECONDS, fetch_fn=fetch)
        return result or {}

    async def get_market_cap_lookup(self, top_n: int = 250) -> dict:
        """
        Real fix for a genuinely significant, previously-undiscovered
        bug: risk_tier classification (risk.py) returns "high_risk"
        unconditionally whenever market_cap_rank is missing -- and the
        dashboard has never actually supplied market cap data to ANY
        scan, static presets or Trending Now, since it was first built.
        Every coin in every scan has always been forced into high_risk
        regardless of its real standing.

        Thin wrapper deriving (market_cap_rank, market_cap_usd) tuples
        from get_market_overview() -- same cache entry, so calling both
        this and get_market_overview() in the same request costs exactly
        one real network call, not two, and there's no risk of the two
        methods disagreeing since only one of them actually parses the
        raw response.
        """
        overview = await self.get_market_overview(top_n=top_n)
        return {symbol: (row["market_cap_rank"], row["market_cap_usd"]) for symbol, row in overview.items()}

    async def discover_universe(self, max_size: int = 25, top_volume_count: int = 20) -> List[str]:
        """
        Combines both signals into one deduplicated list, capped for
        performance (per the explicit "don't scan hundreds of coins"
        requirement — this is bounded discovery, not unbounded).
        Trending coins are placed FIRST — if the list needs trimming to
        fit max_size, the "people are talking about this right now"
        signal wins over pure volume ranking, since that's the specific
        gap being closed here.
        """
        trending, top_volume = await self._safe_gather(top_volume_count)
        combined: List[str] = []
        for symbol in trending + top_volume:
            if symbol in KNOWN_STABLECOINS:
                continue
            if symbol not in combined:
                combined.append(symbol)
        return combined[:max_size]

    async def _safe_gather(self, top_volume_count: int):
        import asyncio
        trending, top_volume = await asyncio.gather(
            self.get_trending_symbols(), self.get_top_by_volume(top_volume_count),
        )
        return trending, top_volume

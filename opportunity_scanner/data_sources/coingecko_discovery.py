"""
CoinGecko coin discovery — the actual fix for "the scanner only knows
about a list I hardcoded, it'll never find a coin that starts trending
tomorrow." Two real, confirmed-free, no-key endpoints:

  - GET /search/trending — CoinGecko's own trending-search ranking,
    refreshes roughly every 15 minutes. IMPORTANT CAVEAT, found from a
    live result: this reflects raw *search* activity, which includes
    curiosity/scam-check searches for genuinely illiquid or spam
    tokens — it is NOT by itself a signal of real trading opportunity.
  - GET /coins/markets?order=volume_desc — coins ranked by actual 24h
    trading volume, live, not a snapshot baked into code. This is the
    real liquidity signal.

A trending-search hit is only trusted here if the SAME coin also shows
genuine trading volume — search attention without real liquidity behind
it is noise, not opportunity, confirmed directly from a real scan that
surfaced spam-adjacent tickers (e.g. "STONKBROKER") ahead of genuinely
major, currently-strong coins. The final list is ordered by actual 24h
volume, not by which source flagged it first.
"""

from __future__ import annotations
from typing import Dict, List, Optional
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


def _parse_market_row(row: dict) -> dict:
    """Shared parser for a single /coins/markets row — used by every
    method that reads this endpoint (market-cap-sorted or volume-
    sorted; the row shape is identical either way), so there's exactly
    one place that knows the field mapping."""
    return {
        "price": row.get("current_price"),
        "volume_24h_usd": row.get("total_volume"),
        "change_24h_pct": row.get("price_change_percentage_24h"),
        "high_24h": row.get("high_24h"),
        "low_24h": row.get("low_24h"),
        "market_cap_usd": row.get("market_cap"),
        "market_cap_rank": row.get("market_cap_rank"),
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
        the "people are searching for this" signal. Deliberately NOT
        trusted alone by discover_universe() below — see module
        docstring for why."""
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

    async def get_volume_overview(self, limit: int = 100) -> Dict[str, dict]:
        """
        Coins ranked by actual current 24h trading volume, with full
        market data per coin (price, volume, change%, high/low) — the
        canonical "what's genuinely liquid right now" dataset. This is
        what both the Trending Now preview table AND the trending-
        search verification filter read from, so there's one consistent
        source of truth for "is this real," not two that could disagree.
        """
        async def fetch():
            try:
                data = await self._breaker.call(lambda: self._get_top_volume_raw(limit))
            except (CircuitOpenError, Exception) as e:  # noqa: BLE001
                print(f"[discovery] CoinGecko volume overview fetch failed: {e}")
                return {}
            overview = {}
            for row in data:
                symbol = row.get("symbol")
                if symbol:
                    overview[symbol.upper()] = _parse_market_row(row)
            return overview

        result = await discovery_cache.get_or_fetch(f"volume_overview:{limit}", ttl_seconds=TOP_VOLUME_TTL_SECONDS, fetch_fn=fetch)
        return result or {}

    async def get_top_by_volume(self, limit: int = 30) -> List[str]:
        """Symbol-only view of get_volume_overview() — kept for callers
        that just need the list, not the full per-coin data."""
        overview = await self.get_volume_overview(limit=limit)
        return list(overview.keys())

    async def _get_market_cap_lookup_raw(self, top_n: int) -> list:
        resp = await self._http.get("/coins/markets", params={
            "vs_currency": "usd", "order": "market_cap_desc", "per_page": top_n, "page": 1, "sparkline": "false",
        })
        resp.raise_for_status()
        return resp.json()

    async def get_market_overview(self, top_n: int = 250) -> dict:
        """
        The single canonical fetch for CoinGecko's top-N-by-market-cap
        data. get_market_cap_lookup() below is a thin derived wrapper
        around this, not a second independent parser — they used to
        both parse the same raw response independently while sharing
        one cache key, which meant whichever one ran first "won" the
        cache with its own shape and silently corrupted the other's
        results. Having exactly one method own the parsing removes that
        whole class of bug rather than working around it.
        """
        async def fetch():
            try:
                data = await self._breaker.call(lambda: self._get_market_cap_lookup_raw(top_n))
            except (CircuitOpenError, Exception) as e:  # noqa: BLE001
                print(f"[discovery] CoinGecko market overview fetch failed: {e}")
                return {}
            return {row["symbol"].upper(): _parse_market_row(row) for row in data if row.get("symbol")}

        result = await discovery_cache.get_or_fetch(f"market_overview:{top_n}", ttl_seconds=TOP_VOLUME_TTL_SECONDS, fetch_fn=fetch)
        return result or {}

    async def get_market_cap_lookup(self, top_n: int = 250) -> dict:
        """
        Real fix for a genuinely significant, previously-undiscovered
        bug: risk_tier classification (risk.py) returns "high_risk"
        unconditionally whenever market_cap_rank is missing — and the
        dashboard has never actually supplied market cap data to ANY
        scan, static presets or Trending Now, since it was first built.

        Thin wrapper deriving (market_cap_rank, market_cap_usd) tuples
        from get_market_overview().
        """
        overview = await self.get_market_overview(top_n=top_n)
        return {symbol: (row["market_cap_rank"], row["market_cap_usd"]) for symbol, row in overview.items()}

    async def discover_universe(self, max_size: int = 25, top_volume_count: int = 100) -> List[str]:
        """
        The real fix for a live user report: raw trending-search hits
        included spam-adjacent tickers ("STONKBROKER") ahead of
        genuinely major, currently-strong coins. A trending-search hit
        is only kept here if the SAME coin ALSO shows real 24h volume —
        search attention without liquidity behind it is noise. The
        final list is ordered by actual volume (highest first), not by
        which source flagged it or in what order — so "pick the ones
        with more volume and % change" is the actual sort, not an
        afterthought.
        """
        symbols, _overview = await self.discover_universe_with_overview(max_size=max_size, top_volume_count=top_volume_count)
        return symbols

    async def discover_universe_with_overview(self, max_size: int = 25, top_volume_count: int = 100) -> tuple:
        """
        Same discovery logic as discover_universe(), but also returns
        the rich per-coin data (price, volume, change%, high/low) that
        was already fetched to DO the discovery — the exact same
        verified data, not a second, separately-sourced lookup. This
        matters concretely: discover_universe() sources candidates from
        volume-sorted data, which isn't the same coin set as a market-
        cap-sorted lookup — a genuinely high-volume, lower-market-cap
        coin could be present in one and absent from the other. Reusing
        this same data for the dashboard's preview table means what's
        shown always matches what was actually discovered, with zero
        chance of the two disagreeing.

        Returns (symbols, {symbol: overview_dict}).
        """
        trending_symbols, volume_overview = await self._safe_gather(top_volume_count)

        candidates: Dict[str, dict] = {}
        for symbol in trending_symbols:
            if symbol in KNOWN_STABLECOINS:
                continue
            if symbol in volume_overview:  # verified: real trading volume exists, not just a search hit
                candidates[symbol] = volume_overview[symbol]
        for symbol, row in volume_overview.items():
            if symbol in KNOWN_STABLECOINS:
                continue
            candidates.setdefault(symbol, row)

        ranked = sorted(
            candidates.items(),
            key=lambda kv: kv[1].get("volume_24h_usd") or 0,
            reverse=True,
        )
        top = ranked[:max_size]
        return [symbol for symbol, _ in top], {symbol: row for symbol, row in top}

    async def _safe_gather(self, top_volume_count: int):
        import asyncio
        trending, volume_overview = await asyncio.gather(
            self.get_trending_symbols(), self.get_volume_overview(top_volume_count),
        )
        return trending, volume_overview

"""
DexScreener data source — free, no API key, on-chain DEX pair data.

This is the real, documented way to get on-chain memecoin/pump.fun-origin
token data, as opposed to scraping FOMO or pump.fun's own apps (which
don't expose a public API and would violate their terms — see the
"On 'best traders on FOMO and PUMP'" note in README.md from an earlier
build pass, that reasoning hasn't changed). DexScreener directly indexes
Solana pools including Raydium, Orca, Meteora, and PumpSwap — which is
where pump.fun tokens land once they migrate off the bonding curve — so
this gives real coverage of that world through a legitimate, documented
API rather than an unofficial one.

Rate limits (per DexScreener's public docs): 60 req/min on token-profile/
boost endpoints, 300 req/min on pair/token endpoints. No key needed for
either. Base URL: https://api.dexscreener.com
"""

from __future__ import annotations
from datetime import datetime, timezone
from typing import List, Optional
import httpx

from ..cache import make_cache, with_retry
from ..circuit_breaker import breakers, CircuitOpenError
from ..provider_models import DataSourceMeta
from ..degen_models import DexPair, DexTransactionCounts, TimeframeStats, classify_pair_venue

DEXSCREENER_BASE_URL = "https://api.dexscreener.com"

dexscreener_cache = make_cache("dexscreener")


class DexScreenerProvider:
    def __init__(self, cache_ttl_seconds: float = 30, failure_threshold: int = 5, cooldown_seconds: float = 60):
        self._http = httpx.AsyncClient(base_url=DEXSCREENER_BASE_URL, timeout=10.0)
        self._breaker = breakers.get("dexscreener", failure_threshold=failure_threshold, cooldown_seconds=cooldown_seconds)
        self.cache_ttl_seconds = cache_ttl_seconds

    async def close(self):
        await self._http.aclose()

    @with_retry(max_attempts=3)
    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        resp = await self._http.get(path, params=params or {})
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _parse_timeframe(price_change: dict, volume: dict, txns: dict, key: str) -> Optional[TimeframeStats]:
        pc = price_change.get(key)
        vol = volume.get(key)
        tx = txns.get(key, {}) or {}
        if pc is None and vol is None and not tx:
            return None
        return TimeframeStats(
            price_change_pct=float(pc) if pc is not None else None,
            volume_usd=float(vol) if vol is not None else None,
            buys=tx.get("buys"), sells=tx.get("sells"),
        )

    def _parse_pair(self, raw: dict) -> Optional[DexPair]:
        try:
            base = raw.get("baseToken", {})
            quote = raw.get("quoteToken", {})
            all_txns = raw.get("txns", {}) or {}
            txns_24h = all_txns.get("h24", {}) or {}
            price_change = raw.get("priceChange", {}) or {}
            volume = raw.get("volume", {}) or {}
            liquidity = raw.get("liquidity", {}) or {}
            info = raw.get("info", {}) or {}
            socials = info.get("socials", []) or []
            websites = info.get("websites", []) or []
            boosts = raw.get("boosts", {}) or {}

            dex_id = raw.get("dexId", "unknown")
            has_twitter = any(s.get("type") == "twitter" for s in socials)
            has_telegram = any(s.get("type") == "telegram" for s in socials)
            has_website = len(websites) > 0

            return DexPair(
                chain_id=raw.get("chainId", "unknown"),
                dex_id=dex_id,
                venue=classify_pair_venue(dex_id),
                pair_address=raw.get("pairAddress", ""),
                base_symbol=base.get("symbol", "?"),
                base_token_address=base.get("address", ""),
                quote_symbol=quote.get("symbol", "?"),
                price_usd=float(raw["priceUsd"]) if raw.get("priceUsd") else None,
                liquidity_usd=float(liquidity.get("usd")) if liquidity.get("usd") is not None else None,
                market_cap_usd=float(raw["marketCap"]) if raw.get("marketCap") else None,
                volume_24h_usd=float(volume.get("h24")) if volume.get("h24") is not None else None,
                price_change_24h_pct=float(price_change.get("h24")) if price_change.get("h24") is not None else None,
                price_change_1h_pct=float(price_change.get("h1")) if price_change.get("h1") is not None else None,
                txns_24h=DexTransactionCounts(buys=txns_24h.get("buys", 0), sells=txns_24h.get("sells", 0)),
                pair_created_at=(
                    datetime.fromtimestamp(raw["pairCreatedAt"] / 1000, tz=timezone.utc).isoformat()
                    if raw.get("pairCreatedAt") else None
                ),
                fdv_usd=float(raw["fdv"]) if raw.get("fdv") else None,
                meta=DataSourceMeta(source="dexscreener"),
                m5=self._parse_timeframe(price_change, volume, all_txns, "m5"),
                h1=self._parse_timeframe(price_change, volume, all_txns, "h1"),
                h6=self._parse_timeframe(price_change, volume, all_txns, "h6"),
                h24=self._parse_timeframe(price_change, volume, all_txns, "h24"),
                is_boosted=bool(boosts.get("active")),
                boost_amount=float(boosts["active"]) if boosts.get("active") else None,
                has_website=has_website, has_twitter=has_twitter, has_telegram=has_telegram,
            )
        except (KeyError, ValueError, TypeError) as e:
            print(f"[dexscreener] failed to parse pair: {e}")
            return None

    async def get_pairs_for_token(self, token_address: str, chain_id: str = "solana") -> List[DexPair]:
        """All DEX pairs trading a given token address — a token can have
        multiple pools (e.g. a pump.fun token might show both its
        bonding-curve pool and, post-migration, its Raydium pool)."""
        cache_key = f"token_pairs:{chain_id}:{token_address}"

        async def fetch():
            try:
                data = await self._breaker.call(lambda: self._get(f"/latest/dex/tokens/{token_address}"))
            except (CircuitOpenError, Exception) as e:  # noqa: BLE001
                print(f"[dexscreener] fetch failed for token {token_address}: {e}")
                return []
            pairs_raw = data.get("pairs") or []
            parsed = [self._parse_pair(p) for p in pairs_raw]
            return [p for p in parsed if p is not None and p.chain_id == chain_id]

        return await dexscreener_cache.get_or_fetch(cache_key, ttl_seconds=self.cache_ttl_seconds, fetch_fn=fetch)

    async def search_pairs(self, query: str) -> List[DexPair]:
        """Search by symbol/name — useful for 'does this coin have DEX liquidity yet'."""
        cache_key = f"search:{query}"

        async def fetch():
            try:
                data = await self._breaker.call(lambda: self._get("/latest/dex/search", {"q": query}))
            except (CircuitOpenError, Exception) as e:  # noqa: BLE001
                print(f"[dexscreener] search failed for '{query}': {e}")
                return []
            pairs_raw = data.get("pairs") or []
            parsed = [self._parse_pair(p) for p in pairs_raw]
            return [p for p in parsed if p is not None]

        return await dexscreener_cache.get_or_fetch(cache_key, ttl_seconds=self.cache_ttl_seconds, fetch_fn=fetch)

    async def get_best_pair_for_token(self, token_address: str, chain_id: str = "solana") -> Optional[DexPair]:
        """The single most liquid pair for a token — the one worth reading
        for a 'what's this token actually doing' summary."""
        pairs = await self.get_pairs_for_token(token_address, chain_id)
        if not pairs:
            return None
        return max(pairs, key=lambda p: p.liquidity_usd or 0)

    async def get_boosted_token_addresses(self, chain_id: str = "solana") -> List[str]:
        """
        Real-time boosted/trending token addresses — DexScreener's own paid
        promotion signal, which updates immediately unlike social-mention
        data (LunarCrush lags for brand-new launches, see
        MEME_ARCHITECTURE.md §3.3). Useful as a discovery feed: tokens
        someone is actively spending money to promote right now.
        """
        cache_key = f"boosts:{chain_id}"

        async def fetch():
            try:
                data = await self._breaker.call(lambda: self._get("/token-boosts/latest/v1"))
            except (CircuitOpenError, Exception) as e:  # noqa: BLE001
                print(f"[dexscreener] boosts fetch failed: {e}")
                return []
            if isinstance(data, list):
                return [item.get("tokenAddress") for item in data if item.get("chainId") == chain_id and item.get("tokenAddress")]
            return []

        return await dexscreener_cache.get_or_fetch(cache_key, ttl_seconds=60, fetch_fn=fetch)

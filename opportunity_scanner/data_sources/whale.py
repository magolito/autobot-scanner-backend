"""
Whale movement data source — Whale Alert.

This is supplementary context, NOT a 5th scoring pillar (the spec is four
pillars). It surfaces alongside a coin's scan result the same way risk
tier does: informative, but deliberately not blended into the composite
score, since a single large transfer can mean many things (exchange
rebalancing, OTC settlement, cold storage move) that aren't necessarily
"bullish" or "bearish" on their own.

This replaces the whale-tracking endpoint that used to live in the
separate Node `scanner-backend/` — ported here so there's one backend,
not two, per the roadmap decision.
"""

from __future__ import annotations
from typing import List, Optional
import httpx

from ..cache import whale_cache, with_retry

WHALE_ALERT_BASE_URL = "https://api.whale-alert.io/v1"


class WhaleDataSource:
    def __init__(self, api_key: Optional[str], min_value_usd: float = 1_000_000):
        self.api_key = api_key
        self.min_value_usd = min_value_usd
        self._http = httpx.AsyncClient(base_url=WHALE_ALERT_BASE_URL, timeout=10.0)

    async def close(self):
        await self._http.aclose()

    @with_retry(max_attempts=3)
    async def _fetch_raw(self, start_ts: int) -> List[dict]:
        if not self.api_key:
            return []
        resp = await self._http.get(
            "/transactions",
            params={"api_key": self.api_key, "min_value": self.min_value_usd, "start": start_ts},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("transactions", [])

    async def get_recent_transactions(self, lookback_seconds: int = 3600) -> List[dict]:
        """Cached for 2 minutes — whale alert data moves fast but doesn't need per-second freshness."""
        import time
        start_ts = int(time.time()) - lookback_seconds
        cache_key = f"whales:{lookback_seconds}"

        async def fetch():
            raw = await self._fetch_raw(start_ts)
            return [
                {
                    "symbol": t.get("symbol"),
                    "amount_usd": t.get("amount_usd"),
                    "from_type": (t.get("from") or {}).get("owner_type", "unknown"),
                    "to_type": (t.get("to") or {}).get("owner_type", "unknown"),
                    "timestamp": t.get("timestamp"),
                }
                for t in raw
            ]

        return await whale_cache.get_or_fetch(cache_key, ttl_seconds=120, fetch_fn=fetch)

    async def get_transactions_for_symbol(self, base: str, lookback_seconds: int = 3600) -> List[dict]:
        all_tx = await self.get_recent_transactions(lookback_seconds)
        return [t for t in all_tx if (t.get("symbol") or "").upper() == base.upper()]

    async def summarize_for_symbol(self, base: str, lookback_seconds: int = 3600) -> dict:
        """
        Simple directional summary: net flow TO exchanges (often read as
        sell pressure building) vs FROM exchanges (often read as
        accumulation/cold storage). This is a heuristic, not a certainty —
        surfaced as raw context for a human to interpret, not as a score.
        """
        tx = await self.get_transactions_for_symbol(base, lookback_seconds)
        if not tx:
            return {"base": base.upper(), "transaction_count": 0, "to_exchange_usd": 0, "from_exchange_usd": 0, "net_flow_usd": 0}

        to_exchange = sum(t["amount_usd"] for t in tx if t["to_type"] == "exchange")
        from_exchange = sum(t["amount_usd"] for t in tx if t["from_type"] == "exchange")
        return {
            "base": base.upper(),
            "transaction_count": len(tx),
            "to_exchange_usd": to_exchange,
            "from_exchange_usd": from_exchange,
            "net_flow_usd": from_exchange - to_exchange,  # positive = net outflow from exchanges
        }

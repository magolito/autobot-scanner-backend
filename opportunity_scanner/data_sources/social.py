"""
Social data source — LunarCrush.

Same key-security note as the standalone scanner-backend: this class is
meant to run server-side only. Never ship LUNARCRUSH_API_KEY to a browser
or frontend build.

LunarCrush's coins/list endpoint gives current snapshots; for velocity and
sentiment SHIFT (not just level) we also pull each coin's short time-series
so we have a real baseline to compare against, per the spec in factors/social.py.

Fields confirmed against LunarCrush's own official v4 API documentation
before this rewrite (github.com/lunarcrush/api), not guessed:
galaxy_score, galaxy_score_previous, alt_rank, alt_rank_previous,
sentiment, social_dominance, market_dominance. `_previous` fields are a
genuinely useful find — they're LunarCrush's own direct prior-24h
baseline, more reliable than trying to reconstruct one from the
time-series ourselves, and they replace a real bug this rewrite fixes:
the scoring layer (factors/social.py) previously expected
`galaxy_score_7d_ago`/`alt_rank_7d_ago`, fields this data source never
actually populated — that growth sub-signal had silently never worked.
"""

from __future__ import annotations
from typing import Dict, Optional
import httpx

from ..cache import make_cache
from ..circuit_breaker import breakers, CircuitOpenError

LUNARCRUSH_BASE_URL = "https://lunarcrush.com/api4/public"

# The coins-list snapshot is shared across every symbol in a scan (one
# call covers all coins), same reasoning as CoinGecko's derivatives
# list — cached with a real TTL this time, not forever. LunarCrush's own
# data updates roughly hourly per their docs, so a 10-minute TTL is
# comfortably fresher than the source itself refreshes, while still
# meaningfully cutting request volume across a multi-coin scan.
social_cache = make_cache("social")
COINS_LIST_TTL_SECONDS = 600
TIME_SERIES_TTL_SECONDS = 600


class SocialDataSource:
    def __init__(self, api_key: Optional[str], failure_threshold: int = 4, cooldown_seconds: float = 120):
        self.api_key = api_key
        self._http = httpx.AsyncClient(
            base_url=LUNARCRUSH_BASE_URL,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=10.0,
        )
        self._breaker = breakers.get("lunarcrush", failure_threshold=failure_threshold, cooldown_seconds=cooldown_seconds)
        self._logged_missing_key = False

    async def close(self):
        await self._http.aclose()

    async def _get_coins_list_raw(self) -> dict:
        resp = await self._http.get("/coins/list/v2")
        if resp.status_code == 429:
            raise RuntimeError("LunarCrush rate limit hit (429) on /coins/list/v2")
        if resp.status_code in (401, 403):
            raise RuntimeError(f"LunarCrush auth failed ({resp.status_code}) — check LUNARCRUSH_API_KEY is valid and not expired")
        resp.raise_for_status()
        return resp.json()

    async def _fetch_coins_list(self) -> Dict[str, dict]:
        async def fetch():
            try:
                data = await self._breaker.call(self._get_coins_list_raw)
            except (CircuitOpenError, Exception) as e:  # noqa: BLE001
                print(f"[social] LunarCrush coins/list failed: {e}")
                return {}
            rows = data.get("data", [])
            return {r["symbol"].upper(): r for r in rows if r.get("symbol")}

        result = await social_cache.get_or_fetch("coins_list", ttl_seconds=COINS_LIST_TTL_SECONDS, fetch_fn=fetch)
        return result or {}

    async def _get_time_series_raw(self, symbol: str) -> dict:
        resp = await self._http.get(
            f"/coins/{symbol.lower()}/time-series/v2",
            params={"interval": "1d", "points": 8},
        )
        if resp.status_code == 429:
            raise RuntimeError(f"LunarCrush rate limit hit (429) on time-series for {symbol}")
        resp.raise_for_status()
        return resp.json()

    async def _fetch_time_series(self, symbol: str) -> Optional[list]:
        """Short history for a single coin, used to build velocity/sentiment
        baselines and detect whether mention volume is accelerating."""
        async def fetch():
            try:
                data = await self._breaker.call(lambda: self._get_time_series_raw(symbol))
            except (CircuitOpenError, Exception) as e:  # noqa: BLE001
                print(f"[social] LunarCrush time-series failed for {symbol}: {e}")
                return None
            return data.get("data")

        return await social_cache.get_or_fetch(f"time_series:{symbol}", ttl_seconds=TIME_SERIES_TTL_SECONDS, fetch_fn=fetch)

    async def get_social_blob(self, symbol: str) -> Optional[dict]:
        """
        Returns the dict shape expected by factors/social.py, or None if
        social data isn't available for this symbol (missing key, symbol
        not tracked, or request failure — all degrade gracefully).
        """
        if not self.api_key:
            if not self._logged_missing_key:
                print("[social] No LUNARCRUSH_API_KEY configured — Social pillar will be unavailable for every coin this scan. "
                      "LunarCrush has no free tier for this data; there is currently no viable free alternative for the "
                      "mention-velocity + sentiment-shift signal this pillar specifically needs (see factors/social.py's docstring).")
                self._logged_missing_key = True
            return None

        coins_list = await self._fetch_coins_list()
        current = coins_list.get(symbol.upper())
        if current is None:
            print(f"[social] {symbol} not found in LunarCrush's tracked coin list — pillar unavailable for this symbol specifically")
            return None

        series = await self._fetch_time_series(symbol)
        baseline_volume = None
        baseline_interactions = None
        sentiment_prev = None
        recent_volume_points: list = []
        if series and len(series) >= 2:
            history_points = series[:-1]  # exclude most recent, which duplicates `current`
            vols = [p.get("social_volume") for p in history_points if p.get("social_volume") is not None]
            interactions = [p.get("interactions") for p in history_points if p.get("interactions") is not None]
            sentiments = [p.get("sentiment") for p in history_points if p.get("sentiment") is not None]
            if vols:
                baseline_volume = sum(vols) / len(vols)
                recent_volume_points = vols[-4:]  # last few daily points, for spike/acceleration detection
            if interactions:
                baseline_interactions = sum(interactions) / len(interactions)
            if sentiments:
                sentiment_prev = sentiments[0]

        return {
            "galaxy_score": current.get("galaxy_score"),
            "galaxy_score_previous": current.get("galaxy_score_previous"),
            "alt_rank": current.get("alt_rank"),
            "alt_rank_previous": current.get("alt_rank_previous"),
            "social_dominance": current.get("social_dominance"),
            "social_volume_24h": current.get("social_volume_24h") or current.get("social_volume"),
            "social_volume_baseline": baseline_volume,
            "recent_volume_points": recent_volume_points,
            "sentiment": current.get("sentiment"),
            "sentiment_prev": sentiment_prev,
            "interactions_24h": current.get("interactions_24h") or current.get("interactions"),
            "interactions_baseline": baseline_interactions,
        }

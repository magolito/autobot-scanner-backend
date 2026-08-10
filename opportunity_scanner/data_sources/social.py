"""
Social data source — LunarCrush.

Same key-security note as the standalone scanner-backend: this class is
meant to run server-side only. Never ship LUNARCRUSH_API_KEY to a browser
or frontend build.

LunarCrush's coins/list endpoint gives current snapshots; for velocity and
sentiment SHIFT (not just level) we also pull each coin's short time-series
so we have a real baseline to compare against, per the spec in factors/social.py.
"""

from __future__ import annotations
from typing import Dict, Optional
import httpx

LUNARCRUSH_BASE_URL = "https://lunarcrush.com/api4/public"


class SocialDataSource:
    def __init__(self, api_key: Optional[str]):
        self.api_key = api_key
        self._http = httpx.AsyncClient(
            base_url=LUNARCRUSH_BASE_URL,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=10.0,
        )
        self._list_cache: Optional[Dict[str, dict]] = None

    async def close(self):
        await self._http.aclose()

    async def _fetch_coins_list(self) -> Dict[str, dict]:
        if self._list_cache is not None:
            return self._list_cache
        if not self.api_key:
            return {}
        try:
            resp = await self._http.get("/coins/list/v2")
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            print(f"[social] LunarCrush coins/list failed: {e}")
            return {}
        rows = data.get("data", [])
        self._list_cache = {r["symbol"].upper(): r for r in rows if r.get("symbol")}
        return self._list_cache

    async def _fetch_time_series(self, symbol: str) -> Optional[dict]:
        """Short history for a single coin, used to build velocity/sentiment baselines."""
        if not self.api_key:
            return None
        try:
            resp = await self._http.get(
                f"/coins/{symbol.lower()}/time-series/v2",
                params={"interval": "1d", "points": 8},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            print(f"[social] LunarCrush time-series failed for {symbol}: {e}")
            return None
        return data.get("data")

    async def get_social_blob(self, symbol: str) -> Optional[dict]:
        """
        Returns the dict shape expected by factors/social.py, or None if
        social data isn't available for this symbol (missing key, symbol
        not tracked, or request failure — all degrade gracefully).
        """
        coins_list = await self._fetch_coins_list()
        current = coins_list.get(symbol.upper())
        if current is None:
            return None

        series = await self._fetch_time_series(symbol)
        baseline_volume = None
        baseline_interactions = None
        sentiment_prev = None
        if series and len(series) >= 2:
            history_points = series[:-1]  # exclude most recent, which duplicates `current`
            vols = [p.get("social_volume") for p in history_points if p.get("social_volume") is not None]
            interactions = [p.get("interactions") for p in history_points if p.get("interactions") is not None]
            sentiments = [p.get("sentiment") for p in history_points if p.get("sentiment") is not None]
            if vols:
                baseline_volume = sum(vols) / len(vols)
            if interactions:
                baseline_interactions = sum(interactions) / len(interactions)
            if sentiments:
                sentiment_prev = sentiments[0]

        return {
            "galaxy_score": current.get("galaxy_score"),
            "alt_rank": current.get("alt_rank"),
            "social_volume_24h": current.get("social_volume_24h") or current.get("social_volume"),
            "social_volume_baseline": baseline_volume,
            "sentiment": current.get("sentiment"),
            "sentiment_prev": sentiment_prev,
            "interactions_24h": current.get("interactions_24h") or current.get("interactions"),
            "interactions_baseline": baseline_interactions,
        }

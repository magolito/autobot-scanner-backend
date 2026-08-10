"""
Multi-exchange derivatives data provider.

Two-tier design:
  Tier 1 (preferred, if COINGLASS_API_KEY is set): CoinGlass — already
    aggregates OI/funding/long-short (including TOP TRADER ratio) across
    6 exchanges in one call. This is what closes the "top-trader ratio
    not available" gap.
  Tier 2 (fallback, always available, free): direct calls to Bybit AND
    Hyperliquid — the two venues AutoBot itself actually trades on —
    averaged where both respond. No top-trader ratio at this tier —
    that's a real, documented limitation, not faked. (Binance was
    evaluated and deliberately excluded: geo-blocked for US IPs, and
    doesn't match either of AutoBot's actual trading venues — two
    aligned sources beat three where one adds hosting complexity for no
    product benefit.)

Every external call goes through a circuit breaker (see circuit_breaker.py)
so a genuinely-down provider fails fast instead of retrying into a wall,
and results carry `DataSourceMeta` so callers always know which tier
actually answered and whether the data is stale/fallback.
"""

from __future__ import annotations
import asyncio
import os
import time
from datetime import datetime, timezone
from typing import List, Optional
import httpx
import ccxt.async_support as ccxt_async

from ..cache import make_cache, with_retry
from ..circuit_breaker import breakers, CircuitOpenError
from ..provider_models import (
    DataSourceMeta, OpenInterestData, OpenInterestPoint,
    FundingRateData, LongShortRatioData, DerivativesSnapshot,
)

derivatives_cache = make_cache("derivatives")

COINGLASS_BASE_URL = "https://open-api-v4.coinglass.com"
BYBIT_BASE_URL = "https://api.bybit.com"


class CoinGlassProvider:
    """Tier 1: multi-exchange aggregated derivatives data, paid."""

    def __init__(self, api_key: Optional[str], breaker_failure_threshold: int = 4, breaker_cooldown_seconds: float = 120):
        self.api_key = api_key
        self._http = httpx.AsyncClient(
            base_url=COINGLASS_BASE_URL, timeout=10.0,
            headers={"CG-API-KEY": api_key} if api_key else {},
        )
        self._breaker = breakers.get("coinglass", failure_threshold=breaker_failure_threshold, cooldown_seconds=breaker_cooldown_seconds)

    async def close(self):
        await self._http.aclose()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    @with_retry(max_attempts=2)
    async def _get(self, path: str, params: dict) -> dict:
        resp = await self._http.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    async def fetch_derivatives_snapshot(self, base: str) -> Optional[DerivativesSnapshot]:
        if not self.available:
            return None

        async def _fetch():
            oi_resp = await self._get("/api/futures/openInterest/exchange-list", {"symbol": base})
            ls_resp = await self._get("/api/futures/topLongShortAccountRatio/history", {"symbol": base, "interval": "1h", "limit": 1})
            funding_resp = await self._get("/api/futures/fundingRate/exchange-list", {"symbol": base})
            return oi_resp, ls_resp, funding_resp

        try:
            oi_resp, ls_resp, funding_resp = await self._breaker.call(_fetch)
        except (CircuitOpenError, Exception) as e:  # noqa: BLE001
            print(f"[coinglass] fetch failed for {base}: {e}")
            return None

        oi_rows = oi_resp.get("data", []) or []
        total_oi_usd = sum(float(r.get("openInterestUsd", 0) or 0) for r in oi_rows)
        exchanges = [r.get("exchangeName") for r in oi_rows if r.get("exchangeName")]

        ls_rows = ls_resp.get("data", []) or []
        top_trader_ratio = None
        if ls_rows:
            last = ls_rows[-1]
            long_pct = float(last.get("longAccount", 0) or 0)
            short_pct = float(last.get("shortAccount", 0) or 0)
            top_trader_ratio = (long_pct / short_pct) if short_pct else None

        funding_rows = funding_resp.get("data", []) or []
        avg_funding = None
        if funding_rows:
            rates = [float(r.get("fundingRate", 0) or 0) for r in funding_rows if r.get("fundingRate") is not None]
            if rates:
                avg_funding = sum(rates) / len(rates)

        meta = DataSourceMeta(source="coinglass")
        return DerivativesSnapshot(
            symbol=base,
            open_interest=OpenInterestData(symbol=base, current_oi_usd=total_oi_usd or None, history=[], meta=meta),
            funding=FundingRateData(symbol=base, funding_rate=avg_funding, meta=meta),
            long_short=LongShortRatioData(symbol=base, global_ratio=None, top_trader_ratio=top_trader_ratio, meta=meta),
            exchanges_aggregated=exchanges,
        )


class DirectExchangeProvider:
    """
    Tier 2: free fallback, no top-trader ratio at this tier.
    Two sources, matching exactly what AutoBot itself trades on — Bybit
    and Hyperliquid — queried concurrently, averaged over whichever respond:
      - Bybit   — richest OI history (used for trend calc), USDT margin
      - Hyperliquid — USDC margin. No OI history endpoint in ccxt for this
                   venue — current snapshot only.
    (Binance was evaluated and deliberately left out — geo-blocked for US
    IPs, and doesn't match either of AutoBot's actual trading venues. Two
    aligned sources beat three where one adds hosting complexity for no
    product benefit.)
    """

    def __init__(
        self,
        bybit_failure_threshold: int = 5, bybit_cooldown_seconds: float = 60,
        hyperliquid_failure_threshold: int = 5, hyperliquid_cooldown_seconds: float = 60,
    ):
        self._bybit_http = httpx.AsyncClient(base_url=BYBIT_BASE_URL, timeout=10.0)
        self._hyperliquid = ccxt_async.hyperliquid({"enableRateLimit": True})
        self._bybit_breaker = breakers.get("bybit_derivatives", failure_threshold=bybit_failure_threshold, cooldown_seconds=bybit_cooldown_seconds)
        self._hyperliquid_breaker = breakers.get("hyperliquid_derivatives", failure_threshold=hyperliquid_failure_threshold, cooldown_seconds=hyperliquid_cooldown_seconds)

    async def close(self):
        await self._bybit_http.aclose()
        await self._hyperliquid.close()

    @with_retry(max_attempts=3)
    async def _bybit_oi(self, symbol: str) -> Optional[dict]:
        resp = await self._bybit_http.get(
            "/v5/market/open-interest",
            params={"category": "linear", "symbol": symbol, "intervalTime": "1h", "limit": 48},
        )
        resp.raise_for_status()
        return resp.json()

    @with_retry(max_attempts=3)
    async def _bybit_long_short(self, symbol: str) -> Optional[dict]:
        resp = await self._bybit_http.get(
            "/v5/market/account-ratio",
            params={"category": "linear", "symbol": symbol, "period": "1h", "limit": 1},
        )
        resp.raise_for_status()
        return resp.json()

    async def _fetch_bybit(self, base: str, quote: str = "USDT") -> Optional[DerivativesSnapshot]:
        symbol = f"{base}{quote}"

        async def _fetch():
            oi_data = await self._bybit_oi(symbol)
            ls_data = await self._bybit_long_short(symbol)
            funding = await ccxt_async.bybit({"enableRateLimit": True}).fetch_funding_rate(f"{base}/{quote}:{quote}")
            return oi_data, ls_data, funding

        try:
            oi_data, ls_data, funding = await self._bybit_breaker.call(_fetch)
        except (CircuitOpenError, Exception) as e:  # noqa: BLE001
            print(f"[bybit_derivatives] fetch failed for {symbol}: {e}")
            return None

        oi_rows = oi_data.get("result", {}).get("list", []) if oi_data else []
        history = [
            OpenInterestPoint(timestamp=datetime.fromtimestamp(int(r["timestamp"]) / 1000, tz=timezone.utc), oi_usd=float(r["openInterest"]))
            for r in oi_rows if "openInterest" in r and "timestamp" in r
        ]
        current_oi = history[-1].oi_usd if history else None

        ls_rows = ls_data.get("result", {}).get("list", []) if ls_data else []
        global_ratio = None
        if ls_rows:
            row = ls_rows[0]
            buy, sell = float(row.get("buyRatio", 0)), float(row.get("sellRatio", 0))
            global_ratio = (buy / sell) if sell else None

        meta = DataSourceMeta(source="bybit")
        return DerivativesSnapshot(
            symbol=base,
            open_interest=OpenInterestData(symbol=base, current_oi_usd=current_oi, history=history, meta=meta),
            funding=FundingRateData(symbol=base, funding_rate=funding.get("fundingRate") if funding else None, meta=meta),
            long_short=LongShortRatioData(symbol=base, global_ratio=global_ratio, top_trader_ratio=None, meta=meta),
            exchanges_aggregated=["bybit"],
        )

    async def _fetch_hyperliquid(self, base: str) -> Optional[DerivativesSnapshot]:
        # Hyperliquid perpetuals are USDC-margined, not USDT — different
        # symbol convention from Bybit/Binance
        symbol = f"{base}/USDC:USDC"

        async def _fetch():
            oi = await self._hyperliquid.fetch_open_interest(symbol)
            funding = await self._hyperliquid.fetch_funding_rate(symbol)
            return oi, funding

        try:
            oi, funding = await self._hyperliquid_breaker.call(_fetch)
        except (CircuitOpenError, Exception) as e:  # noqa: BLE001
            print(f"[hyperliquid_derivatives] fetch failed for {symbol}: {e}")
            return None

        current_oi = oi.get("openInterestValue") if oi else None

        meta = DataSourceMeta(source="hyperliquid")
        return DerivativesSnapshot(
            symbol=base,
            open_interest=OpenInterestData(symbol=base, current_oi_usd=current_oi, history=[], meta=meta),
            funding=FundingRateData(symbol=base, funding_rate=funding.get("fundingRate") if funding else None, meta=meta),
            long_short=LongShortRatioData(symbol=base, global_ratio=None, top_trader_ratio=None, meta=meta),
            exchanges_aggregated=["hyperliquid"],
        )

    async def fetch_derivatives_snapshot(self, base: str) -> Optional[DerivativesSnapshot]:
        """Query both sources concurrently, average whichever respond.
        Only fails outright if both do — Hyperliquid being down doesn't
        mean losing Bybit's data, and vice versa."""
        bybit_snap, hyperliquid_snap = await asyncio.gather(
            self._fetch_bybit(base),
            self._fetch_hyperliquid(base),
            return_exceptions=True,
        )
        snaps = [s for s in (bybit_snap, hyperliquid_snap) if isinstance(s, DerivativesSnapshot)]

        if not snaps:
            return None
        if len(snaps) == 1:
            return snaps[0]

        oi_values = [s.open_interest.current_oi_usd for s in snaps if s.open_interest.current_oi_usd]
        funding_values = [s.funding.funding_rate for s in snaps if s.funding.funding_rate is not None]
        # Bybit's OI history is the richest (only source with actual history) — keep it for trend calc if present
        history = next((s.open_interest.history for s in snaps if s.open_interest.history), [])
        # long/short ratio: only Bybit exposes this at the free tier
        long_short = next((s.long_short for s in snaps if s.long_short.global_ratio is not None), snaps[0].long_short)

        meta = DataSourceMeta(source="+".join(sorted(set(ex for s in snaps for ex in s.exchanges_aggregated))))
        return DerivativesSnapshot(
            symbol=base,
            open_interest=OpenInterestData(
                symbol=base,
                current_oi_usd=(sum(oi_values) / len(oi_values)) if oi_values else None,
                history=history,
                meta=meta,
            ),
            funding=FundingRateData(
                symbol=base,
                funding_rate=(sum(funding_values) / len(funding_values)) if funding_values else None,
                meta=meta,
            ),
            long_short=long_short,
            exchanges_aggregated=sorted(set(ex for s in snaps for ex in s.exchanges_aggregated)),
        )


class MultiExchangeOIProvider:
    """
    Public entry point: tries CoinGlass first (if configured), falls back
    to direct multi-exchange calls. Caches the final result for 3 minutes
    regardless of which tier answered.
    """

    def __init__(
        self,
        coinglass_api_key: Optional[str] = None,
        cache_ttl_seconds: float = 180,
        breaker_config: Optional[dict] = None,
    ):
        bc = breaker_config or {}
        cg = bc.get("coinglass", {"failure_threshold": 4, "cooldown_seconds": 120})
        bybit = bc.get("bybit", {"failure_threshold": 5, "cooldown_seconds": 60})
        hyperliquid = bc.get("hyperliquid", {"failure_threshold": 5, "cooldown_seconds": 60})

        self.coinglass = CoinGlassProvider(
            coinglass_api_key or os.getenv("COINGLASS_API_KEY"),
            breaker_failure_threshold=cg.get("failure_threshold", 4),
            breaker_cooldown_seconds=cg.get("cooldown_seconds", 120),
        )
        self.direct = DirectExchangeProvider(
            bybit_failure_threshold=bybit.get("failure_threshold", 5),
            bybit_cooldown_seconds=bybit.get("cooldown_seconds", 60),
            hyperliquid_failure_threshold=hyperliquid.get("failure_threshold", 5),
            hyperliquid_cooldown_seconds=hyperliquid.get("cooldown_seconds", 60),
        )
        self.cache_ttl_seconds = cache_ttl_seconds

    async def close(self):
        await self.coinglass.close()
        await self.direct.close()

    async def get_derivatives_snapshot(self, base: str) -> Optional[DerivativesSnapshot]:
        cache_key = f"derivatives:{base}"

        async def fetch():
            if self.coinglass.available:
                result = await self.coinglass.fetch_derivatives_snapshot(base)
                if result is not None:
                    return result
                print(f"[multi_exchange_oi] CoinGlass unavailable for {base}, falling back to direct exchanges")

            result = await self.direct.fetch_derivatives_snapshot(base)
            if result is not None:
                result.open_interest.meta.is_fallback = self.coinglass.available  # only "fallback" if we WANTED coinglass
            return result

        return await derivatives_cache.get_or_fetch(cache_key, ttl_seconds=self.cache_ttl_seconds, fetch_fn=fetch)

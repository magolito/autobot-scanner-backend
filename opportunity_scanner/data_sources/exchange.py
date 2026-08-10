"""
Exchange data source.

OHLCV and ticker data go through ccxt (unified, well-tested across
exchanges — swap `primary_exchange` in config to point at a different
venue with minimal code changes).

Open interest, funding rate, and long/short account ratio are NOT
consistently unified across ccxt exchanges, so those go through direct
calls to Bybit's public v5 REST API. If you swap exchanges, you'll need
to swap these three methods — they're isolated here specifically so
that's a contained change.
"""

from __future__ import annotations
import asyncio
from typing import Dict, List, Optional
import pandas as pd
import httpx
import ccxt.async_support as ccxt_async

from ..config import ScannerConfig
from ..models import MarketSnapshot
from ..cache import exchange_cache, with_retry

BYBIT_BASE_URL = "https://api.bybit.com"

_DEFAULT_CACHE_TTLS = {
    "ticker": 20, "15m": 60, "1h": 180, "4h": 600, "1d": 1800, "open_interest": 300,
}


class ExchangeDataSource:
    def __init__(self, config: ScannerConfig, cache_ttls: Optional[Dict[str, float]] = None):
        self.config = config
        # Configurable via settings.yaml's resilience.cache_ttl_seconds —
        # falls back to these defaults if not supplied, so existing callers
        # that just do ExchangeDataSource(config) are unaffected.
        self.cache_ttls = cache_ttls or dict(_DEFAULT_CACHE_TTLS)
        exchange_cls = getattr(ccxt_async, config.primary_exchange)
        self.exchange = exchange_cls({"enableRateLimit": True})
        self._http = httpx.AsyncClient(base_url=BYBIT_BASE_URL, timeout=10.0)

    async def close(self):
        await self.exchange.close()
        await self._http.aclose()

    # ---------------------------------------------------------------- OHLCV

    async def _fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
        cache_key = f"ohlcv:{symbol}:{timeframe}:{limit}"

        async def fetch():
            try:
                raw = await self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            except Exception as e:  # noqa: BLE001 — deliberately broad, this is a best-effort data pull
                print(f"[exchange] OHLCV fetch failed for {symbol} {timeframe}: {e}")
                return None
            if not raw:
                return None
            df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            return df

        # cache TTL scales with timeframe — no point re-fetching 1d candles every 20s
        ttl = self.cache_ttls.get(timeframe, 120)
        return await exchange_cache.get_or_fetch(cache_key, ttl_seconds=ttl, fetch_fn=fetch)

    async def fetch_multi_timeframe_ohlcv(self, symbol: str) -> Dict[str, pd.DataFrame]:
        tf_config = self.config.timeframe_config
        tasks = {
            tf: self._fetch_ohlcv(symbol, tf, tf_config.candles_per_timeframe)
            for tf in tf_config.timeframes
        }
        results = await asyncio.gather(*tasks.values())
        return {tf: df for tf, df in zip(tasks.keys(), results) if df is not None}

    # ---------------------------------------------------------------- ticker

    async def fetch_ticker_data(self, symbol: str) -> dict:
        cache_key = f"ticker:{symbol}"

        async def fetch():
            try:
                t = await self.exchange.fetch_ticker(symbol)
            except Exception as e:  # noqa: BLE001
                print(f"[exchange] ticker fetch failed for {symbol}: {e}")
                return None
            spread_pct = None
            if t.get("bid") and t.get("ask") and t["bid"] > 0:
                spread_pct = (t["ask"] - t["bid"]) / t["bid"] * 100
            return {
                "price": t.get("last"),
                "volume_24h_usd": (t.get("quoteVolume") or 0.0),
                "bid_ask_spread_pct": spread_pct,
            }

        result = await exchange_cache.get_or_fetch(cache_key, ttl_seconds=self.cache_ttls.get("ticker", 20), fetch_fn=fetch)
        return result or {}

    # ------------------------------------------------------- open interest

    @with_retry(max_attempts=3)
    async def _fetch_open_interest_raw(self, symbol: str) -> dict:
        resp = await self._http.get(
            "/v5/market/open-interest",
            params={"category": "linear", "symbol": symbol, "intervalTime": "1h", "limit": 48},
        )
        resp.raise_for_status()
        return resp.json()

    async def fetch_open_interest_history(self, base: str, quote: str = "USDT") -> Optional[pd.DataFrame]:
        """
        Bybit v5 open interest, linear perps. Returns recent snapshots so
        callers can compute % change over the window. Bybit's public
        endpoint returns historical points directly (no need for us to
        poll over time ourselves), unlike a pure ticker snapshot.
        """
        symbol = f"{base}{quote}"
        cache_key = f"oi:{symbol}"

        async def fetch():
            try:
                data = await self._fetch_open_interest_raw(symbol)
            except Exception as e:  # noqa: BLE001
                print(f"[exchange] OI fetch failed for {symbol}: {e}")
                return None
            rows = data.get("result", {}).get("list", [])
            if not rows:
                return None
            df = pd.DataFrame(rows)
            if "openInterest" not in df.columns or "timestamp" not in df.columns:
                return None
            df["oi_usd"] = df["openInterest"].astype(float)
            df["ts"] = pd.to_datetime(df["timestamp"].astype("int64"), unit="ms")
            return df[["ts", "oi_usd"]].sort_values("ts").reset_index(drop=True)

        return await exchange_cache.get_or_fetch(cache_key, ttl_seconds=self.cache_ttls.get("open_interest", 300), fetch_fn=fetch)

    async def fetch_funding_rate(self, base: str, quote: str = "USDT") -> Optional[float]:
        symbol = f"{base}/{quote}:{quote}"  # ccxt unified swap symbol notation
        try:
            fr = await self.exchange.fetch_funding_rate(symbol)
            return fr.get("fundingRate")
        except Exception as e:  # noqa: BLE001
            print(f"[exchange] funding rate fetch failed for {symbol}: {e}")
            return None

    async def fetch_long_short_ratio(self, base: str, quote: str = "USDT") -> Optional[float]:
        """Bybit v5 account long/short ratio — not unified in ccxt, raw call."""
        symbol = f"{base}{quote}"
        try:
            resp = await self._http.get(
                "/v5/market/account-ratio",
                params={"category": "linear", "symbol": symbol, "period": "1h", "limit": 1},
            )
            resp.raise_for_status()
            data = resp.json()
            rows = data.get("result", {}).get("list", [])
            if not rows:
                return None
            row = rows[0]
            buy_ratio = float(row.get("buyRatio", 0))
            sell_ratio = float(row.get("sellRatio", 0))
            if sell_ratio == 0:
                return None
            return buy_ratio / sell_ratio
        except Exception as e:  # noqa: BLE001
            print(f"[exchange] long/short ratio fetch failed for {symbol}: {e}")
            return None

    # --------------------------------------------------------- orchestrator

    async def build_snapshot(
        self,
        base: str,
        quote: Optional[str] = None,
        market_cap_usd: Optional[float] = None,
        exchange_listings: int = 1,
    ) -> MarketSnapshot:
        quote = quote or self.config.quote_currency
        symbol = f"{base}/{quote}"

        ohlcv, ticker, oi_history, funding, long_short = await asyncio.gather(
            self.fetch_multi_timeframe_ohlcv(symbol),
            self.fetch_ticker_data(symbol),
            self.fetch_open_interest_history(base, quote),
            self.fetch_funding_rate(base, quote),
            self.fetch_long_short_ratio(base, quote),
        )

        return MarketSnapshot(
            symbol=symbol,
            base=base,
            price=ticker.get("price") or 0.0,
            market_cap_usd=market_cap_usd,
            volume_24h_usd=ticker.get("volume_24h_usd", 0.0),
            bid_ask_spread_pct=ticker.get("bid_ask_spread_pct"),
            exchange_listings=exchange_listings,
            ohlcv=ohlcv,
            open_interest_history=oi_history,
            open_interest_usd=(oi_history["oi_usd"].iloc[-1] if oi_history is not None and len(oi_history) else None),
            funding_rate=funding,
            long_short_ratio=long_short,
        )

"""
Exchange data source.

Price/OHLCV/ticker and OI/funding all go through a strict priority chain
(config.market_data_priority, default: Hyperliquid -> CoinGecko ->
Coinbase -> Kraken -> Bybit), not averaging. First source to answer
wins — the point is accuracy from the best available source, not
blending multiple venues' numbers together. Every fetch logs which
source actually answered (see MarketSnapshot.data_sources), and Bybit
is deliberately last/optional: it's geo-blocked for US-hosted
deployments (confirmed live, not theoretical — see MEME_ARCHITECTURE.md
and the Railway deployment notes), so the scan must never depend on it
succeeding.

OHLCV multi-timeframe candles specifically only participate for
Hyperliquid and Bybit (the two sources with ccxt-native candle support
in this implementation) — Coinbase/Kraken contribute ticker/spot-price
confirmation only, not OHLCV, an explicit scope decision, not a silent
gap.
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
from ..circuit_breaker import breakers, CircuitOpenError
from .coingecko_derivatives import CoinGeckoDerivativesProvider
from .us_spot import USSpotProvider

BYBIT_BASE_URL = "https://api.bybit.com"

_DEFAULT_CACHE_TTLS = {
    "ticker": 20, "15m": 60, "1h": 180, "4h": 600, "1d": 1800, "open_interest": 300,
}


class ExchangeDataSource:
    def __init__(self, config: ScannerConfig, cache_ttls: Optional[Dict[str, float]] = None,
                 hyperliquid_failure_threshold: int = 3, hyperliquid_cooldown_seconds: float = 90,
                 bybit_failure_threshold: int = 3, bybit_cooldown_seconds: float = 90):
        self.config = config
        self.cache_ttls = cache_ttls or dict(_DEFAULT_CACHE_TTLS)
        self.priority: List[str] = list(getattr(config, "market_data_priority", None) or ["hyperliquid", "coingecko", "coinbase", "kraken", "bybit"])

        # Explicit 10s timeout (ccxt ms) on both ccxt clients — without
        # this, a slow-but-not-erroring source pays its full default
        # timeout on every single call, and with no circuit breaker (see
        # below) that cost repeats for every coin/data-point in a scan
        # rather than failing fast after a few tries. This combination —
        # missing breaker + relying on ccxt's default timeout — is what
        # actually caused multi-minute scans in production, not a
        # theoretical concern.
        exchange_cls = getattr(ccxt_async, config.primary_exchange)
        self.exchange = exchange_cls({"enableRateLimit": True, "timeout": 10000})  # kept for backward compat + as the "bybit" priority slot
        self._hyperliquid = ccxt_async.hyperliquid({"enableRateLimit": True, "timeout": 10000})
        # ccxt's enableRateLimit paces a SEQUENCE of calls, but scan_many
        # fires many coroutines concurrently via asyncio.gather (every
        # coin x every data point), and those all reach for the SAME
        # Hyperliquid client near-simultaneously — enableRateLimit alone
        # doesn't prevent that. Confirmed as a real cause of 429 Too Many
        # Requests responses in a live scan, not theoretical. Bounding
        # actual concurrency with a semaphore fixes what pacing a
        # sequence can't.
        self._hyperliquid_semaphore = asyncio.Semaphore(4)
        self._coingecko = CoinGeckoDerivativesProvider()
        self._us_spot = USSpotProvider()
        self._http = httpx.AsyncClient(base_url=BYBIT_BASE_URL, timeout=10.0)

        self._hyperliquid_breaker = breakers.get("hyperliquid_exchange", failure_threshold=hyperliquid_failure_threshold, cooldown_seconds=hyperliquid_cooldown_seconds)
        self._bybit_breaker = breakers.get("bybit_exchange", failure_threshold=bybit_failure_threshold, cooldown_seconds=bybit_cooldown_seconds)

    async def close(self):
        await self.exchange.close()
        await self._hyperliquid.close()
        await self._coingecko.close()
        await self._us_spot.close()
        await self._http.aclose()

    # ---------------------------------------------------------------- OHLCV
    #
    # Every _with_source variant below returns (value, source_str) — no
    # shared mutable state on `self`, which matters because
    # asyncio.gather() runs these concurrently (both within one
    # build_snapshot call, and across DIFFERENT symbols' concurrent
    # build_snapshot calls during scan_many). An instance-level
    # "self._last_sources" dict would race across concurrent calls;
    # returning the source directly alongside the value, per-call,
    # can't race because there's nothing shared to race over. Public
    # methods (fetch_ticker_data, fetch_funding_rate, etc.) keep their
    # original return types for backward compatibility — they're thin
    # wrappers that discard the source label.

    async def _fetch_ohlcv_from(self, source: str, base: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
        try:
            if source == "hyperliquid":
                async with self._hyperliquid_semaphore:
                    raw = await self._hyperliquid_breaker.call(lambda: self._hyperliquid.fetch_ohlcv(f"{base}/USDC:USDC", timeframe=timeframe, limit=limit))
            elif source == "bybit":
                raw = await self._bybit_breaker.call(lambda: self.exchange.fetch_ohlcv(f"{base}/USDT", timeframe=timeframe, limit=limit))
            else:
                return None  # coingecko/coinbase/kraken don't participate in OHLCV — see module docstring
        except (CircuitOpenError, Exception) as e:  # noqa: BLE001 — best-effort per-source pull, the chain handles failure
            print(f"[exchange:{source}] OHLCV fetch failed for {base} {timeframe}: {e}")
            return None
        if not raw:
            return None
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df

    async def _fetch_ohlcv_with_source(self, symbol: str, timeframe: str, limit: int) -> tuple:
        base = symbol.split("/")[0]
        cache_key = f"ohlcv:{base}:{timeframe}:{limit}"

        async def fetch():
            for source in self.priority:
                if source not in ("hyperliquid", "bybit"):
                    continue  # only these two support OHLCV in this implementation
                df = await self._fetch_ohlcv_from(source, base, timeframe, limit)
                if df is not None and not df.empty:
                    return (df, source)
            return (None, "none")

        ttl = self.cache_ttls.get(timeframe, 120)
        result = await exchange_cache.get_or_fetch(cache_key, ttl_seconds=ttl, fetch_fn=fetch)
        return result if result is not None else (None, "none")

    async def _fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
        df, _source = await self._fetch_ohlcv_with_source(symbol, timeframe, limit)
        return df

    async def fetch_multi_timeframe_ohlcv_with_source(self, symbol: str) -> tuple:
        """Returns (Dict[timeframe, DataFrame], representative_source) —
        the source of whichever timeframe answered first/most, since in
        practice every timeframe for one symbol comes from the same
        priority-chain source (they all try the same chain in the same
        order)."""
        tf_config = self.config.timeframe_config
        tasks = {
            tf: self._fetch_ohlcv_with_source(symbol, tf, tf_config.candles_per_timeframe)
            for tf in tf_config.timeframes
        }
        results = await asyncio.gather(*tasks.values())
        dfs = {tf: df for tf, (df, _src) in zip(tasks.keys(), results) if df is not None}
        sources_used = [src for _df, src in results if src != "none"]
        representative_source = sources_used[0] if sources_used else "none"
        return dfs, representative_source

    async def fetch_multi_timeframe_ohlcv(self, symbol: str) -> Dict[str, pd.DataFrame]:
        dfs, _source = await self.fetch_multi_timeframe_ohlcv_with_source(symbol)
        return dfs

    # ---------------------------------------------------------------- ticker

    async def _fetch_ticker_from(self, source: str, base: str) -> Optional[dict]:
        try:
            if source == "hyperliquid":
                async with self._hyperliquid_semaphore:
                    t = await self._hyperliquid_breaker.call(lambda: self._hyperliquid.fetch_ticker(f"{base}/USDC:USDC"))
                spread_pct = None
                if t.get("bid") and t.get("ask") and t["bid"] > 0:
                    spread_pct = (t["ask"] - t["bid"]) / t["bid"] * 100
                return {"price": t.get("last"), "volume_24h_usd": t.get("quoteVolume") or 0.0, "bid_ask_spread_pct": spread_pct}

            if source == "coingecko":
                snap = await self._coingecko.get_snapshot(base)
                if snap is None or snap.price is None:
                    return None
                return {"price": snap.price, "volume_24h_usd": 0.0, "bid_ask_spread_pct": None}

            if source in ("coinbase", "kraken"):
                spot = await self._us_spot.get_spot_price(base)
                if spot is None or spot.price is None or spot.source != source:
                    return None  # get_spot_price itself tries coinbase-then-kraken; only accept if it matched the source we're currently trying
                return {"price": spot.price, "volume_24h_usd": spot.volume_24h_usd or 0.0, "bid_ask_spread_pct": None}

            if source == "bybit":
                t = await self._bybit_breaker.call(lambda: self.exchange.fetch_ticker(f"{base}/USDT"))
                spread_pct = None
                if t.get("bid") and t.get("ask") and t["bid"] > 0:
                    spread_pct = (t["ask"] - t["bid"]) / t["bid"] * 100
                return {"price": t.get("last"), "volume_24h_usd": t.get("quoteVolume") or 0.0, "bid_ask_spread_pct": spread_pct}
        except (CircuitOpenError, Exception) as e:  # noqa: BLE001
            print(f"[exchange:{source}] ticker fetch failed for {base}: {e}")
            return None
        return None

    async def fetch_ticker_data_with_source(self, symbol: str) -> tuple:
        base = symbol.split("/")[0]
        cache_key = f"ticker:{base}"

        async def fetch():
            for source in self.priority:
                result = await self._fetch_ticker_from(source, base)
                if result is not None and result.get("price") is not None:
                    print(f"[exchange] {base} price sourced from {source}")
                    return (result, source)
            print(f"[exchange] {base} price unavailable from every source in priority order: {self.priority}")
            return ({}, "none")

        result = await exchange_cache.get_or_fetch(cache_key, ttl_seconds=self.cache_ttls.get("ticker", 20), fetch_fn=fetch)
        return result if result is not None else ({}, "none")

    async def fetch_ticker_data(self, symbol: str) -> dict:
        result, _source = await self.fetch_ticker_data_with_source(symbol)
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

    async def fetch_open_interest_history_with_source(self, base: str, quote: str = "USDT") -> tuple:
        """
        Priority chain, not Bybit-only. Bybit is the only source with a
        real HISTORY endpoint (multiple past OI points), so if Bybit is
        reachable it's still genuinely useful for the trend calc even
        though it's last in priority for "which single number is most
        accurate" — Hyperliquid/CoinGecko give a current snapshot only
        (wrapped as a single-point history), which is enough for the
        current-value pillars but not for trend detection. This is a
        real, honest tradeoff: prioritizing Bybit's history over its
        general reliability would contradict "never let Bybit being
        blocked break the scan," so single-point-history from a higher-
        priority source is preferred, with Bybit's fuller history used
        only when nothing higher-priority answered at all.
        """
        for source in self.priority:
            if source == "hyperliquid":
                try:
                    async with self._hyperliquid_semaphore:
                        oi = await self._hyperliquid_breaker.call(lambda: self._hyperliquid.fetch_open_interest(f"{base}/USDC:USDC"))
                    current = oi.get("openInterestValue") if oi else None
                except (CircuitOpenError, Exception) as e:  # noqa: BLE001
                    print(f"[exchange:hyperliquid] OI fetch failed for {base}: {e}")
                    current = None
                if current is not None:
                    return (pd.DataFrame([{"ts": pd.Timestamp.now(tz="UTC"), "oi_usd": current}]), "hyperliquid")

            elif source == "coingecko":
                snap = await self._coingecko.get_snapshot(base)
                if snap is not None and snap.open_interest_usd is not None:
                    return (pd.DataFrame([{"ts": pd.Timestamp.now(tz="UTC"), "oi_usd": snap.open_interest_usd}]), "coingecko")

            elif source == "bybit":
                symbol = f"{base}{quote}"
                cache_key = f"oi:{symbol}"

                async def fetch():
                    try:
                        data = await self._fetch_open_interest_raw(symbol)
                    except Exception as e:  # noqa: BLE001
                        print(f"[exchange:bybit] OI fetch failed for {symbol}: {e}")
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

                result = await exchange_cache.get_or_fetch(cache_key, ttl_seconds=self.cache_ttls.get("open_interest", 300), fetch_fn=fetch)
                if result is not None:
                    return (result, "bybit")

        return (None, "none")

    async def fetch_open_interest_history(self, base: str, quote: str = "USDT") -> Optional[pd.DataFrame]:
        df, _source = await self.fetch_open_interest_history_with_source(base, quote)
        return df

    async def fetch_funding_rate_with_source(self, base: str, quote: str = "USDT") -> tuple:
        for source in self.priority:
            if source == "hyperliquid":
                try:
                    async with self._hyperliquid_semaphore:
                        fr = await self._hyperliquid_breaker.call(lambda: self._hyperliquid.fetch_funding_rate(f"{base}/USDC:USDC"))
                    rate = fr.get("fundingRate") if fr else None
                except (CircuitOpenError, Exception) as e:  # noqa: BLE001
                    print(f"[exchange:hyperliquid] funding rate fetch failed for {base}: {e}")
                    rate = None
                if rate is not None:
                    return (rate, "hyperliquid")

            elif source == "coingecko":
                snap = await self._coingecko.get_snapshot(base)
                if snap is not None and snap.funding_rate is not None:
                    return (snap.funding_rate, "coingecko")

            elif source == "bybit":
                symbol = f"{base}/{quote}:{quote}"
                try:
                    fr = await self._bybit_breaker.call(lambda: self.exchange.fetch_funding_rate(symbol))
                    rate = fr.get("fundingRate") if fr else None
                except (CircuitOpenError, Exception) as e:  # noqa: BLE001
                    print(f"[exchange:bybit] funding rate fetch failed for {symbol}: {e}")
                    rate = None
                if rate is not None:
                    return (rate, "bybit")

        return (None, "none")

    async def fetch_funding_rate(self, base: str, quote: str = "USDT") -> Optional[float]:
        rate, _source = await self.fetch_funding_rate_with_source(base, quote)
        return rate

    async def fetch_long_short_ratio_with_source(self, base: str, quote: str = "USDT") -> tuple:
        """
        Honest limitation, stated plainly: none of Hyperliquid, CoinGecko,
        Coinbase, or Kraken expose a public account-level long/short
        ratio the way Bybit's v5 API does. This stays Bybit-only —
        gracefully None (not a crash, not a fabricated value) if Bybit
        is unreachable, exactly the "never let Bybit block the scan"
        requirement, just with no higher-priority substitute available
        for this specific data point.
        """
        if "bybit" not in self.priority:
            return (None, "none")
        symbol = f"{base}{quote}"
        try:
            async def _get():
                resp = await self._http.get(
                    "/v5/market/account-ratio",
                    params={"category": "linear", "symbol": symbol, "period": "1h", "limit": 1},
                )
                resp.raise_for_status()
                return resp.json()
            data = await self._bybit_breaker.call(_get)
            rows = data.get("result", {}).get("list", [])
            if not rows:
                return (None, "none")
            row = rows[0]
            buy_ratio = float(row.get("buyRatio", 0))
            sell_ratio = float(row.get("sellRatio", 0))
            if sell_ratio == 0:
                return (None, "none")
            return (buy_ratio / sell_ratio, "bybit")
        except Exception as e:  # noqa: BLE001
            print(f"[exchange:bybit] long/short ratio fetch failed for {symbol}: {e}")
            return (None, "none")

    async def fetch_long_short_ratio(self, base: str, quote: str = "USDT") -> Optional[float]:
        ratio, _source = await self.fetch_long_short_ratio_with_source(base, quote)
        return ratio

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

        # Every awaited coroutine here returns (value, source) directly —
        # no shared instance state, so this is safe even when scan_many
        # runs build_snapshot for many symbols concurrently.
        (ohlcv, ohlcv_source), (ticker, ticker_source), (oi_history, oi_source), \
            (funding, funding_source), (long_short, ls_source) = await asyncio.gather(
                self.fetch_multi_timeframe_ohlcv_with_source(symbol),
                self.fetch_ticker_data_with_source(symbol),
                self.fetch_open_interest_history_with_source(base, quote),
                self.fetch_funding_rate_with_source(base, quote),
                self.fetch_long_short_ratio_with_source(base, quote),
            )

        data_sources = {
            "price": ticker_source, "ohlcv": ohlcv_source, "open_interest": oi_source,
            "funding_rate": funding_source, "long_short_ratio": ls_source,
        }

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
            data_sources=data_sources,
        )

"""
Exchange priority chain test — the actual point of the Bybit-blocking
fix. Two things matter most here:

  1. The priority/fallback logic itself: first source to answer wins,
     no averaging, Bybit only used as a last resort.
  2. The concurrency fix: build_snapshot for multiple DIFFERENT symbols
     running concurrently (exactly what scan_many does) must not have
     their source labels cross-contaminate — this was a real bug caught
     while building this (a shared self._last_sources dict would race
     under concurrent calls), fixed by having every fetch return its
     source directly rather than mutating shared instance state.

No live network needed — every source method is monkeypatched at the
instance level with synthetic responses.
"""

from __future__ import annotations
import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.config import ScannerConfig
from opportunity_scanner.data_sources.exchange import ExchangeDataSource


def make_source(monkeypatched_priority=None):
    config = ScannerConfig()
    if monkeypatched_priority is not None:
        config.market_data_priority = monkeypatched_priority
    return ExchangeDataSource(config)


async def main():
    # 1. Hyperliquid succeeds -> used, CoinGecko/Bybit never even tried
    source = make_source()
    coingecko_called = {"value": False}
    bybit_called = {"value": False}

    async def fake_hl_ticker(symbol):
        return {"last": 65000.0, "bid": 64999.0, "ask": 65001.0, "quoteVolume": 1000000}

    async def fake_coingecko_snapshot(base):
        coingecko_called["value"] = True
        return None

    async def fake_bybit_ticker(symbol):
        bybit_called["value"] = True
        return {"last": 64000.0}

    source._hyperliquid.fetch_ticker = fake_hl_ticker
    source._coingecko.get_snapshot = fake_coingecko_snapshot
    source.exchange.fetch_ticker = fake_bybit_ticker

    result, used_source = await source.fetch_ticker_data_with_source("CHECK1/USDT")
    assert used_source == "hyperliquid"
    assert result["price"] == 65000.0
    assert coingecko_called["value"] is False, "CoinGecko should never be tried when Hyperliquid succeeds"
    assert bybit_called["value"] is False, "Bybit should never be tried when Hyperliquid succeeds"
    print("1. Hyperliquid success -> used directly, lower-priority sources never even called: OK")

    # 2. Hyperliquid fails -> CoinGecko tried and succeeds -> used, Bybit never tried
    source2 = make_source()
    from opportunity_scanner.data_sources.coingecko_derivatives import CoinGeckoDerivativesSnapshot

    async def failing_hl_ticker(symbol):
        raise ConnectionError("Hyperliquid down")

    async def working_coingecko(base):
        return CoinGeckoDerivativesSnapshot(price=64500.0, funding_rate=0.01, open_interest_usd=1000000)

    bybit_called2 = {"value": False}
    async def fake_bybit_ticker2(symbol):
        bybit_called2["value"] = True
        return {"last": 64000.0}

    source2._hyperliquid.fetch_ticker = failing_hl_ticker
    source2._coingecko.get_snapshot = working_coingecko
    source2.exchange.fetch_ticker = fake_bybit_ticker2

    result2, used_source2 = await source2.fetch_ticker_data_with_source("CHECK2/USDT")
    assert used_source2 == "coingecko"
    assert result2["price"] == 64500.0
    assert bybit_called2["value"] is False, "Bybit should never be tried when CoinGecko succeeds"
    print("2. Hyperliquid failure correctly falls through to CoinGecko, Bybit never tried: OK")

    # 3. Everything fails, Bybit is no longer in the DEFAULT priority at
    # all (confirmed permanently blocked, removed entirely rather than
    # just deprioritized) -> correctly unavailable, not a silent
    # fallback to a source that was never going to work anyway
    source3 = make_source()

    async def failing(*a, **kw):
        raise ConnectionError("down")

    async def failing_coingecko(base):
        return None

    source3._hyperliquid.fetch_ticker = failing
    source3._coingecko.get_snapshot = failing_coingecko
    source3._us_spot.get_spot_price = lambda base: asyncio.sleep(0, result=None)

    result3, used_source3 = await source3.fetch_ticker_data_with_source("CHECK3/USDT")
    assert used_source3 == "none"
    assert result3 == {}
    assert "bybit" not in source3.priority, "Bybit should not be in the default priority at all anymore"
    print("3. THE ACTUAL FIX: with Bybit removed from the default priority (confirmed permanently blocked), everything else failing correctly resolves to unavailable — no pointless attempt at a source that never succeeds: OK")

    # 3b. Bybit's integration code itself is NOT broken — proving it
    # still genuinely works as a fallback if explicitly re-added to a
    # custom priority list (e.g. if the geo-block is ever lifted)
    source3b = make_source(monkeypatched_priority=["hyperliquid", "coingecko", "coinbase", "kraken", "bybit"])

    async def working_bybit_ticker(symbol):
        return {"last": 63000.0, "bid": 62999, "ask": 63001, "quoteVolume": 500000}

    source3b._hyperliquid.fetch_ticker = failing
    source3b._coingecko.get_snapshot = failing_coingecko
    source3b._us_spot.get_spot_price = lambda base: asyncio.sleep(0, result=None)
    source3b.exchange.fetch_ticker = working_bybit_ticker

    result3b, used_source3b = await source3b.fetch_ticker_data_with_source("CHECK3B/USDT")
    assert used_source3b == "bybit"
    assert result3b["price"] == 63000.0
    print("3b. Bybit's fallback code path itself still genuinely works — confirmed by explicitly re-adding it to a custom priority list, proving this was a configuration choice, not broken code: OK")

    # 4. Every source fails -> graceful None, not a crash
    source4 = make_source()
    source4._hyperliquid.fetch_ticker = failing
    source4._coingecko.get_snapshot = failing_coingecko
    source4._us_spot.get_spot_price = lambda base: asyncio.sleep(0, result=None)
    async def failing_bybit(symbol):
        raise ConnectionError("Bybit also down (e.g. CloudFront geo-block)")
    source4.exchange.fetch_ticker = failing_bybit

    result4, used_source4 = await source4.fetch_ticker_data_with_source("CHECK4/USDT")
    assert used_source4 == "none"
    assert result4 == {}
    print("4. Every source failing (including Bybit) correctly degrades to empty/None, doesn't crash: OK")

    # Tests 2-4 deliberately failed Hyperliquid/Bybit several times each to
    # exercise the fallback chain — since the circuit breaker registry is a
    # shared singleton (correct for production: one real breaker per
    # provider name across the whole process), those accumulated failures
    # would otherwise trip the breaker before this next check runs with a
    # genuinely-succeeding Hyperliquid. Reset explicitly for test isolation.
    from opportunity_scanner.circuit_breaker import breakers
    await breakers.get("hyperliquid_exchange")._record_success()
    await breakers.get("bybit_exchange")._record_success()

    # 5. data_sources dict on the assembled MarketSnapshot correctly reflects per-field sources
    # (explicitly includes "bybit" in priority here since this test specifically verifies
    # Bybit-sourced attribution for long/short ratio, which is opt-in now, not default)
    source5 = make_source(monkeypatched_priority=["hyperliquid", "coingecko", "coinbase", "kraken", "bybit"])
    source5._hyperliquid.fetch_ticker = fake_hl_ticker
    source5._hyperliquid.fetch_ohlcv = lambda symbol, timeframe, limit: asyncio.sleep(0, result=[[1700000000000, 1, 2, 0.5, 1.5, 100]])
    source5._hyperliquid.fetch_open_interest = lambda symbol: asyncio.sleep(0, result={"openInterestAmount": 200000, "openInterestValue": None, "info": {"markPx": "25.0"}})
    source5._hyperliquid.fetch_funding_rate = lambda symbol: asyncio.sleep(0, result={"fundingRate": 0.005})
    # long/short ratio has no Hyperliquid equivalent — force it through to Bybit
    async def working_ls(*a, **kw):
        class FakeResp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {"result": {"list": [{"buyRatio": "0.6", "sellRatio": "0.4"}]}}
        return FakeResp()
    source5._http.get = working_ls

    snapshot = await source5.build_snapshot(base="SNAPFIVE")
    print(f"5. Assembled MarketSnapshot.data_sources: {snapshot.data_sources}")
    assert snapshot.data_sources["price"] == "hyperliquid"
    assert snapshot.data_sources["open_interest"] == "hyperliquid"
    assert snapshot.data_sources["funding_rate"] == "hyperliquid"
    assert snapshot.data_sources["long_short_ratio"] == "bybit"  # the one data point with no non-Bybit equivalent
    print("   Per-field source tracking correctly reflects which source answered each data point: OK")

    # 6. THE CRITICAL TEST: concurrent build_snapshot for different symbols
    # don't cross-contaminate source labels (proves the concurrency bug is fixed)
    await breakers.get("hyperliquid_exchange")._record_success()
    await breakers.get("bybit_exchange")._record_success()
    # Explicitly includes "bybit" here since this test's fallback scenario
    # (BTC falls through to the last-resort source) specifically needs a
    # last-resort source in the chain to exercise — the actual thing being
    # tested (concurrent snapshots not cross-contaminating source labels)
    # is independent of which specific source that is.
    source6 = make_source(monkeypatched_priority=["hyperliquid", "coingecko", "coinbase", "kraken", "bybit"])

    async def hl_ticker_by_symbol(symbol):
        return {"last": 100.0, "bid": 99, "ask": 101, "quoteVolume": 1000}

    async def coingecko_fails(base):
        return None

    call_count = {"n": 0}
    async def bybit_ticker_alternating(symbol):
        # BTC will succeed via bybit, ETH will succeed via hyperliquid —
        # deliberately different sources per symbol, run CONCURRENTLY,
        # to prove they don't leak into each other's result
        call_count["n"] += 1
        return {"last": 200.0, "bid": 199, "ask": 201, "quoteVolume": 2000}

    hl_call_state = {"eth_should_succeed": True}
    async def hl_ticker_selective(symbol):
        if "ETH" in symbol and hl_call_state["eth_should_succeed"]:
            return {"last": 3000.0, "bid": 2999, "ask": 3001, "quoteVolume": 5000}
        raise ConnectionError("Hyperliquid doesn't have this symbol")

    source6._hyperliquid.fetch_ticker = hl_ticker_selective
    source6._coingecko.get_snapshot = coingecko_fails
    source6._us_spot.get_spot_price = lambda base: asyncio.sleep(0, result=None)
    source6.exchange.fetch_ticker = bybit_ticker_alternating
    # minimal stubs so build_snapshot doesn't error on the other fields
    source6._hyperliquid.fetch_ohlcv = lambda *a, **kw: asyncio.sleep(0, result=[])
    source6.exchange.fetch_ohlcv = lambda *a, **kw: asyncio.sleep(0, result=[])
    source6._hyperliquid.fetch_open_interest = lambda symbol: asyncio.sleep(0, result=None) if "ETH" not in symbol else asyncio.sleep(0, result={"openInterestAmount": 1, "openInterestValue": None, "info": {"markPx": "1.0"}})
    source6._hyperliquid.fetch_funding_rate = lambda symbol: asyncio.sleep(0, result=None) if "ETH" not in symbol else asyncio.sleep(0, result={"fundingRate": 0.001})
    source6._http.get = lambda *a, **kw: asyncio.sleep(0, result=type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: {"result": {"list": []}}})())

    # Run BTC (falls through to bybit) and ETH (hyperliquid succeeds) CONCURRENTLY
    btc_snap, eth_snap = await asyncio.gather(
        source6.build_snapshot(base="CONCBTC"),
        source6.build_snapshot(base="CONCETH"),
    )
    print(f"6. Concurrent snapshots — BTC price source: {btc_snap.data_sources['price']}, ETH price source: {eth_snap.data_sources['price']}")
    assert btc_snap.data_sources["price"] == "bybit", f"BTC should have fallen through to bybit, got {btc_snap.data_sources['price']}"
    assert eth_snap.data_sources["price"] == "hyperliquid", f"ETH should have used hyperliquid, got {eth_snap.data_sources['price']}"
    assert btc_snap.price == 200.0
    assert eth_snap.price == 3000.0
    print("   CRITICAL: concurrent build_snapshot for different symbols did NOT cross-contaminate source labels — the concurrency bug is genuinely fixed: OK")

    await source.close()
    await source2.close()
    await source3.close()
    await source4.close()
    await source5.close()
    await source6.close()

    print("\n✅ Exchange priority chain test passed: strict priority order, correct fallback at every tier, graceful all-sources-failed degradation, per-field source tracking, and — critically — no cross-contamination between concurrent symbol scans.")


if __name__ == "__main__":
    asyncio.run(main())

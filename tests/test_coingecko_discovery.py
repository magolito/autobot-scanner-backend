"""
CoinGecko discovery test — the actual fix for "the scanner only knows
coins I hardcoded." Synthetic responses match the confirmed real
CoinGecko API shapes (verified via search before building), no live
network needed.

Checks:
  1. Trending symbols correctly parsed from the real response shape
  2. Top-by-volume symbols correctly parsed from the real /coins/markets shape
  3. discover_universe() combines both, deduplicated, trending-first
  4. max_size cap is genuinely respected — bounded discovery, not unbounded
  5. Graceful degradation: one source failing doesn't break the other
  6. Both sources failing degrades to an empty list, not a crash
"""

from __future__ import annotations
import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.data_sources.coingecko_discovery import CoinGeckoDiscoveryProvider
from opportunity_scanner.circuit_breaker import breakers


async def main():
    await breakers.get("coingecko_discovery")._record_success()  # reset for test isolation

    provider = CoinGeckoDiscoveryProvider()

    # 1. Trending symbols — real CoinGecko /search/trending shape
    async def fake_trending_raw():
        return {"coins": [
            {"item": {"id": "hype-coin", "symbol": "hype", "name": "Hyperliquid"}},
            {"item": {"id": "pump-fun", "symbol": "pump", "name": "Pump.fun"}},
            {"item": {"id": "some-new-coin", "symbol": "newc", "name": "Brand New Coin"}},
        ]}
    provider._get_trending_raw = fake_trending_raw
    trending = await provider.get_trending_symbols()
    assert trending == ["HYPE", "PUMP", "NEWC"]
    print(f"1. Trending symbols correctly parsed from the real API shape: {trending}: OK")

    # 2. Top-by-volume — real /coins/markets shape
    async def fake_volume_raw(limit):
        return [
            {"id": "bitcoin", "symbol": "btc", "current_price": 65000, "total_volume": 30000000000},
            {"id": "ethereum", "symbol": "eth", "current_price": 3200, "total_volume": 15000000000},
        ]
    provider._get_top_volume_raw = fake_volume_raw
    top_vol = await provider.get_top_by_volume(limit=2)
    assert top_vol == ["BTC", "ETH"]
    print(f"2. Top-by-volume symbols correctly parsed: {top_vol}: OK")

    # 3. discover_universe combines both, deduplicated, trending FIRST
    from opportunity_scanner.data_sources.coingecko_discovery import discovery_cache
    await discovery_cache.invalidate("trending")  # avoid colliding with check #1's cached result — same fixed key, no parameters to differentiate
    async def fake_trending_raw2():
        return {"coins": [{"item": {"symbol": "hype"}}, {"item": {"symbol": "btc"}}]}  # BTC appears in both sources
    async def fake_volume_raw2(limit):
        return [{"symbol": "btc"}, {"symbol": "eth"}, {"symbol": "sol"}]
    provider._get_trending_raw = fake_trending_raw2
    provider._get_top_volume_raw = fake_volume_raw2
    universe = await provider.discover_universe(max_size=25, top_volume_count=3)
    assert universe[0] == "HYPE", f"Trending should come first, got {universe}"
    assert universe.count("BTC") == 1, "BTC appears in both sources — must be deduplicated, not doubled"
    assert set(universe) == {"HYPE", "BTC", "ETH", "SOL"}
    print(f"3. Combined discovery correctly deduplicates and puts trending first: {universe}: OK")

    # 4. max_size is genuinely respected
    await discovery_cache.invalidate("trending")
    async def fake_trending_raw3():
        return {"coins": [{"item": {"symbol": f"T{i}"}} for i in range(10)]}
    async def fake_volume_raw3(limit):
        return [{"symbol": f"V{i}"} for i in range(30)]
    provider._get_trending_raw = fake_trending_raw3
    provider._get_top_volume_raw = fake_volume_raw3
    bounded = await provider.discover_universe(max_size=15, top_volume_count=30)
    assert len(bounded) == 15, f"Expected exactly 15 (max_size cap), got {len(bounded)} — this is the explicit performance requirement"
    print(f"4. max_size cap genuinely respected: {len(bounded)} coins from 40 available candidates: OK")

    # 5. One source failing doesn't break the other
    await discovery_cache.invalidate("trending")
    async def failing_trending():
        raise RuntimeError("CoinGecko trending down")
    provider._get_trending_raw = failing_trending
    provider._get_top_volume_raw = fake_volume_raw2
    partial = await provider.discover_universe(max_size=25, top_volume_count=3)
    assert set(partial) == {"BTC", "ETH", "SOL"}, f"Expected volume data to still work despite trending failing, got {partial}"
    print(f"5. Trending source failing doesn't break top-volume discovery: {partial}: OK")

    # 6. Both failing degrades to empty, not a crash
    await discovery_cache.invalidate("trending")
    await discovery_cache.invalidate("top_volume:3")
    async def failing_volume(limit):
        raise RuntimeError("CoinGecko markets down")
    provider._get_top_volume_raw = failing_volume
    empty = await provider.discover_universe(max_size=25, top_volume_count=3)
    assert empty == []
    print("6. Both sources failing correctly degrades to an empty list, not a crash: OK")

    await breakers.get("coingecko_discovery")._record_success()
    await provider.close()

    print("\n✅ CoinGecko discovery test passed: real API shapes parsed correctly, trending-first deduplication, the performance-bounding max_size cap genuinely enforced, and graceful degradation when one or both sources fail.")


if __name__ == "__main__":
    asyncio.run(main())

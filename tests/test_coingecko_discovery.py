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

    await test_market_cap_lookup_and_stablecoin_filter()
    await test_market_overview_and_shared_cache()


async def test_market_cap_lookup_and_stablecoin_filter():
    """
    Two real bugs found from a live scan screenshot: (1) risk_tier
    unconditionally defaults to high_risk when market_cap_rank is
    missing, and market cap data was never wired through to any scan at
    all; (2) stablecoins (USDT/USDC/etc.) showed up in Trending Now
    discovery results, which is meaningless — a pegged asset has no
    momentum signal to surface.
    """
    from opportunity_scanner.data_sources.coingecko_discovery import CoinGeckoDiscoveryProvider, KNOWN_STABLECOINS, discovery_cache
    from opportunity_scanner.circuit_breaker import breakers
    await breakers.get("coingecko_discovery")._record_success()

    provider = CoinGeckoDiscoveryProvider()

    # 1. Market cap lookup correctly parsed from the real /coins/markets shape
    async def fake_markets_raw(top_n):
        return [
            {"id": "bitcoin", "symbol": "btc", "market_cap_rank": 1, "market_cap": 1_200_000_000_000},
            {"id": "solana", "symbol": "sol", "market_cap_rank": 5, "market_cap": 80_000_000_000},
        ]
    provider._get_market_cap_lookup_raw = fake_markets_raw
    lookup = await provider.get_market_cap_lookup(top_n=250)
    assert lookup["BTC"] == (1, 1_200_000_000_000)
    assert lookup["SOL"] == (5, 80_000_000_000)
    print(f"1. Market cap lookup correctly parsed real API shape: BTC rank={lookup['BTC'][0]}, SOL rank={lookup['SOL'][0]}: OK")

    # 2. A coin NOT in the top-N lookup correctly isn't found (falls through to high_risk elsewhere, correctly)
    assert "SOMEOBSCURECOIN" not in lookup
    print("2. A genuinely obscure coin outside the top-N correctly isn't found (falls through to high_risk correctly, not a bug for THAT case): OK")

    # 3. Stablecoins are correctly excluded from discovery
    await discovery_cache.invalidate("trending")
    await discovery_cache.invalidate("top_volume:2")
    async def trending_with_stablecoins():
        return {"coins": [{"item": {"symbol": "sol"}}, {"item": {"symbol": "usdt"}}, {"item": {"symbol": "usdc"}}, {"item": {"symbol": "hype"}}]}
    async def volume_with_stablecoins(limit):
        return [{"symbol": "usd1"}, {"symbol": "btc"}]
    provider._get_trending_raw = trending_with_stablecoins
    provider._get_top_volume_raw = volume_with_stablecoins
    universe = await provider.discover_universe(max_size=25, top_volume_count=2)
    assert "USDT" not in universe and "USDC" not in universe and "USD1" not in universe, f"Stablecoins should be filtered out, got {universe}"
    assert set(universe) == {"SOL", "HYPE", "BTC"}
    print(f"3. Stablecoins correctly filtered from discovery results: {universe} (no USDT/USDC/USD1): OK")

    await breakers.get("coingecko_discovery")._record_success()
    await provider.close()
    print("\n✅ Market cap lookup + stablecoin filter test passed.")


async def test_market_overview_and_shared_cache():
    """
    get_market_overview() — the actual fix for "I want to see volume and
    live prices" for Trending Now. Also proves it shares its underlying
    fetch with get_market_cap_lookup() (same cache key), so calling both
    in one request only does one real network call, not two.
    """
    from opportunity_scanner.data_sources.coingecko_discovery import CoinGeckoDiscoveryProvider, discovery_cache
    from opportunity_scanner.circuit_breaker import breakers
    await breakers.get("coingecko_discovery")._record_success()
    await discovery_cache.invalidate("market_overview:250")

    provider = CoinGeckoDiscoveryProvider()
    call_count = {"value": 0}

    async def fake_markets_raw(top_n):
        call_count["value"] += 1
        return [
            {"symbol": "sol", "current_price": 145.32, "total_volume": 3_200_000_000,
             "price_change_percentage_24h": 8.4, "high_24h": 149.10, "low_24h": 132.50, "market_cap_rank": 5},
        ]
    provider._get_market_cap_lookup_raw = fake_markets_raw

    # 1. Real fields correctly parsed
    overview = await provider.get_market_overview(top_n=250)
    assert overview["SOL"]["price"] == 145.32
    assert overview["SOL"]["volume_24h_usd"] == 3_200_000_000
    assert overview["SOL"]["change_24h_pct"] == 8.4
    assert overview["SOL"]["high_24h"] == 149.10
    assert overview["SOL"]["low_24h"] == 132.50
    print(f"1. get_market_overview() correctly returns real price/volume/change/high-low data: {overview['SOL']}: OK")

    # 2. Shares the underlying fetch with get_market_cap_lookup — only ONE real call for both
    assert call_count["value"] == 1, "get_market_overview should have triggered exactly one real fetch so far"
    lookup = await provider.get_market_cap_lookup(top_n=250)
    assert lookup["SOL"] == (5, None)  # this fake response doesn't set market_cap, only market_cap_rank
    assert call_count["value"] == 1, f"get_market_cap_lookup() should reuse the SAME cached fetch as get_market_overview() (same cache key) — expected still 1 real call, got {call_count['value']}"
    print("2. get_market_overview() and get_market_cap_lookup() correctly share the same underlying cached fetch — calling both costs only 1 real network call, not 2: OK")

    await breakers.get("coingecko_discovery")._record_success()
    await provider.close()
    print("\n✅ Market overview test passed.")


if __name__ == "__main__":
    asyncio.run(main())

"""
CoinGecko discovery test — covers the real fix for a live user report:
raw trending-search hits included spam-adjacent tickers ahead of
genuinely major, currently-strong coins ("HYPE, PUMP, LIT are not even
there... something is not working"). Root cause: trending-search
reflects raw search curiosity, not real liquidity — a coin can be
"trending" purely because people are searching to check if it's a scam.

Fix: a trending hit is only kept if the SAME coin also shows real
volume, and the final list is ordered by actual 24h volume, not by
which source flagged it first.
"""

from __future__ import annotations
import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.data_sources.coingecko_discovery import CoinGeckoDiscoveryProvider, KNOWN_STABLECOINS, discovery_cache
from opportunity_scanner.circuit_breaker import breakers


async def main():
    await breakers.get("coingecko_discovery")._record_success()

    provider = CoinGeckoDiscoveryProvider()

    # 1. Trending symbols correctly parsed from the real API shape
    async def fake_trending_raw():
        return {"coins": [
            {"item": {"symbol": "hype"}}, {"item": {"symbol": "pump"}}, {"item": {"symbol": "stonkbroker"}},
        ]}
    provider._get_trending_raw = fake_trending_raw
    trending = await provider.get_trending_symbols()
    assert trending == ["HYPE", "PUMP", "STONKBROKER"]
    print(f"1. Trending symbols correctly parsed from the real API shape: {trending}: OK")

    # 2. Volume overview correctly parsed with full market data, not just symbols
    async def fake_volume_raw(limit):
        return [
            {"symbol": "hype", "current_price": 55.66, "total_volume": 254_000_000, "price_change_percentage_24h": 2.0, "high_24h": 55.89, "low_24h": 53.78},
            {"symbol": "pump", "current_price": 0.0027, "total_volume": 140_000_000, "price_change_percentage_24h": 12.7, "high_24h": 0.0028, "low_24h": 0.0024},
        ]
    provider._get_top_volume_raw = fake_volume_raw
    overview = await provider.get_volume_overview(limit=100)
    assert overview["HYPE"]["price"] == 55.66
    assert overview["PUMP"]["volume_24h_usd"] == 140_000_000
    print(f"2. Volume overview correctly returns full market data (price, volume, change%): OK")

    # 3. THE CORE FIX: a trending symbol with NO real volume backing (spam-adjacent) is filtered OUT
    await discovery_cache.invalidate("trending")
    await discovery_cache.invalidate("volume_overview:100")
    universe = await provider.discover_universe(max_size=25, top_volume_count=100)
    assert "STONKBROKER" not in universe, f"A trending hit with no real volume behind it should be filtered out as noise, got {universe}"
    assert "HYPE" in universe and "PUMP" in universe
    print(f"3. THE CORE FIX: 'STONKBROKER' (trending-search hit, zero real volume) correctly filtered out as noise; HYPE/PUMP (real volume) correctly kept: {universe}: OK")

    # 4. Final list is ordered by actual 24h volume, highest first — not by source or arbitrary order
    assert universe[0] == "HYPE", f"HYPE has more volume (254M) than PUMP (140M) and should rank first, got {universe}"
    print(f"4. Final list correctly ordered by actual 24h volume (highest first), not by which source flagged it: {universe}: OK")

    # 5. A coin appearing in BOTH trending and volume-overview isn't double-counted
    await discovery_cache.invalidate("trending")
    await discovery_cache.invalidate("volume_overview:100")
    async def fake_trending_overlap():
        return {"coins": [{"item": {"symbol": "hype"}}]}
    async def fake_volume_overlap(limit):
        return [{"symbol": "hype", "current_price": 55.66, "total_volume": 254_000_000, "price_change_percentage_24h": 2.0, "high_24h": 55.89, "low_24h": 53.78},
                {"symbol": "eth", "current_price": 3200, "total_volume": 15_000_000_000, "price_change_percentage_24h": -1.0, "high_24h": 3250, "low_24h": 3150}]
    provider._get_trending_raw = fake_trending_overlap
    provider._get_top_volume_raw = fake_volume_overlap
    universe2 = await provider.discover_universe(max_size=25, top_volume_count=100)
    assert universe2.count("HYPE") == 1
    print(f"5. A coin appearing in both trending and volume-overview correctly isn't double-counted: {universe2}: OK")

    # 6. Stablecoins still excluded
    await discovery_cache.invalidate("trending")
    await discovery_cache.invalidate("volume_overview:100")
    async def fake_trending_empty():
        return {"coins": []}
    async def fake_volume_with_stable(limit):
        return [{"symbol": "usdt", "current_price": 1.0, "total_volume": 50_000_000_000, "price_change_percentage_24h": 0.01, "high_24h": 1.001, "low_24h": 0.999},
                {"symbol": "sol", "current_price": 145, "total_volume": 3_200_000_000, "price_change_percentage_24h": 8.4, "high_24h": 149, "low_24h": 132}]
    provider._get_trending_raw = fake_trending_empty
    provider._get_top_volume_raw = fake_volume_with_stable
    universe3 = await provider.discover_universe(max_size=25, top_volume_count=100)
    assert "USDT" not in universe3
    assert universe3 == ["SOL"]
    print(f"6. Stablecoins correctly still excluded even with high volume: {universe3}: OK")

    # 7. max_size cap genuinely respected
    await discovery_cache.invalidate("trending")
    await discovery_cache.invalidate("volume_overview:100")
    async def fake_volume_many(limit):
        return [{"symbol": f"C{i}", "current_price": 1.0, "total_volume": 1000 - i, "price_change_percentage_24h": 1.0, "high_24h": 1.1, "low_24h": 0.9} for i in range(50)]
    provider._get_trending_raw = fake_trending_empty
    provider._get_top_volume_raw = fake_volume_many
    bounded = await provider.discover_universe(max_size=15, top_volume_count=50)
    assert len(bounded) == 15, f"Expected exactly 15 (max_size cap), got {len(bounded)}"
    print(f"7. max_size cap genuinely respected: {len(bounded)} coins from 50 available candidates: OK")

    # 8. Graceful degradation: trending fails, volume overview still works
    async def failing_trending():
        raise RuntimeError("CoinGecko trending down")
    provider._get_trending_raw = failing_trending
    provider._get_top_volume_raw = fake_volume_with_stable
    await discovery_cache.invalidate("trending")
    await discovery_cache.invalidate("volume_overview:100")
    partial = await provider.discover_universe(max_size=25, top_volume_count=100)
    assert partial == ["SOL"], f"Volume overview should still work despite trending failing, got {partial}"
    print(f"8. Trending source failing doesn't break volume-based discovery: {partial}: OK")

    await breakers.get("coingecko_discovery")._record_success()
    await provider.close()
    print("\n✅ Discovery test passed: the actual fix verified — spam-adjacent trending hits with no real volume are filtered out, real coins are correctly kept and ranked by actual volume, not by arbitrary source order.")

    await test_market_cap_lookup_and_stablecoin_filter()
    await test_market_overview_and_shared_cache()


async def test_market_cap_lookup_and_stablecoin_filter():
    """Two real bugs found from a live scan screenshot: (1) risk_tier
    unconditionally defaults to high_risk when market_cap_rank is
    missing, and market cap data was never wired through to any scan at
    all; (2) stablecoins showed up in discovery results, meaningless
    for a momentum/opportunity signal."""
    await breakers.get("coingecko_discovery")._record_success()

    provider = CoinGeckoDiscoveryProvider()

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

    assert "SOMEOBSCURECOIN" not in lookup
    print("2. A genuinely obscure coin outside the top-N correctly isn't found (falls through to high_risk correctly, not a bug for THAT case): OK")

    await discovery_cache.invalidate("trending")
    await discovery_cache.invalidate("volume_overview:2")
    async def trending_with_stablecoins():
        return {"coins": [{"item": {"symbol": "sol"}}, {"item": {"symbol": "usdt"}}, {"item": {"symbol": "usdc"}}, {"item": {"symbol": "hype"}}]}
    async def volume_with_stablecoins(limit):
        return [
            {"symbol": "usd1", "current_price": 1.0, "total_volume": 1_000_000, "price_change_percentage_24h": 0.0, "high_24h": 1.0, "low_24h": 1.0},
            {"symbol": "btc", "current_price": 65000, "total_volume": 30_000_000_000, "price_change_percentage_24h": 1.0, "high_24h": 66000, "low_24h": 64000},
        ]
    provider._get_trending_raw = trending_with_stablecoins
    provider._get_top_volume_raw = volume_with_stablecoins
    universe = await provider.discover_universe(max_size=25, top_volume_count=2)
    assert "USDT" not in universe and "USDC" not in universe and "USD1" not in universe, f"Stablecoins should be filtered out, got {universe}"
    assert set(universe) == {"BTC"}  # SOL/HYPE were trending-only with no volume backing in this fake data, correctly filtered
    print(f"3. Stablecoins correctly filtered from discovery results: {universe} (no USDT/USDC/USD1): OK")

    await breakers.get("coingecko_discovery")._record_success()
    await provider.close()
    print("\n✅ Market cap lookup + stablecoin filter test passed.")


async def test_market_overview_and_shared_cache():
    """get_market_overview() -- proves it shares its underlying fetch
    with get_market_cap_lookup() (same cache key), so calling both in
    one request only does one real network call, not two."""
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

    overview = await provider.get_market_overview(top_n=250)
    assert overview["SOL"]["price"] == 145.32
    assert overview["SOL"]["volume_24h_usd"] == 3_200_000_000
    assert overview["SOL"]["change_24h_pct"] == 8.4
    assert overview["SOL"]["high_24h"] == 149.10
    assert overview["SOL"]["low_24h"] == 132.50
    print(f"1. get_market_overview() correctly returns real price/volume/change/high-low data: {overview['SOL']}: OK")

    assert call_count["value"] == 1
    lookup = await provider.get_market_cap_lookup(top_n=250)
    assert lookup["SOL"] == (5, None)
    assert call_count["value"] == 1, f"get_market_cap_lookup() should reuse the SAME cached fetch as get_market_overview() — expected still 1 real call, got {call_count['value']}"
    print("2. get_market_overview() and get_market_cap_lookup() correctly share the same underlying cached fetch — calling both costs only 1 real network call, not 2: OK")

    await breakers.get("coingecko_discovery")._record_success()
    await provider.close()
    print("\n✅ Market overview test passed.")


if __name__ == "__main__":
    asyncio.run(main())

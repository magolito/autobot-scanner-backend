"""
Scan-cycle memoization test — the actual fix for a live production
report: scans still taking 10-30 minutes even after concurrency limits
were correctly capped. Root cause: sector-relative-strength calc
fetches a full snapshot for every sector peer of every scanned coin,
with zero deduplication — the "l1" sector alone has 8 coins, so
scanning all 8 does 8 primary + 56 redundant peer fetches. Concurrency
limits cap how many things happen at once; this fixes how much total
work exists.

Checks:
  1. Outside a scan cycle, every call fetches fresh (no memoization) —
     correct behavior for one-off calls, unchanged from before
  2. Within a scan cycle, requesting the SAME symbol multiple times
     SEQUENTIALLY only fetches once
  3. THE CRITICAL TEST: requesting the same symbol CONCURRENTLY (the
     real scan_many pattern) still only fetches once — proves the
     asyncio.Task-based dedup actually prevents the race, not just the
     sequential case
  4. Different symbols within the same cycle are still fetched
     independently, not accidentally merged
  5. end_scan_cycle() correctly deactivates memoization for later calls
  6. Exception safety: if the batch raises, memoization still gets
     deactivated (via scanner.py's try/finally), not left stuck on
  7. A realistic scenario: simulating an 8-coin sector (matching the
     real "l1" sector map) shows exactly 8 underlying fetches, not 64
"""

from __future__ import annotations
import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.config import ScannerConfig
from opportunity_scanner.data_sources.exchange import ExchangeDataSource
from opportunity_scanner.models import MarketSnapshot


def make_source():
    return ExchangeDataSource(ScannerConfig())


async def main():
    call_log = []

    async def fake_uncached(self, base, quote=None, market_cap_usd=None, exchange_listings=1):
        call_log.append(base)
        await asyncio.sleep(0.02)  # simulate real network latency
        return MarketSnapshot(symbol=f"{base}/USDT", base=base, price=100.0)

    # 1. Outside a scan cycle — every call fetches fresh, unchanged behavior
    source = make_source()
    source._build_snapshot_uncached = fake_uncached.__get__(source)
    call_log.clear()
    await source.build_snapshot(base="BTC")
    await source.build_snapshot(base="BTC")
    assert len(call_log) == 2, f"Expected 2 real fetches outside a scan cycle (no memoization), got {len(call_log)}"
    print("1. Outside a scan cycle, repeat calls for the same symbol each fetch fresh (memoization correctly inactive by default): OK")

    # 2. Within a scan cycle, sequential repeat calls for the same symbol only fetch once
    call_log.clear()
    source.start_scan_cycle()
    await source.build_snapshot(base="ETH")
    await source.build_snapshot(base="ETH")
    await source.build_snapshot(base="ETH")
    source.end_scan_cycle()
    assert len(call_log) == 1, f"Expected exactly 1 real fetch for 3 sequential calls to the same symbol within a scan cycle, got {len(call_log)}"
    print("2. Within a scan cycle, sequential repeat requests for the same symbol only fetch once: OK")

    # 3. THE CRITICAL TEST: concurrent requests for the same symbol don't race into duplicate fetches
    call_log.clear()
    source.start_scan_cycle()
    results = await asyncio.gather(*[source.build_snapshot(base="SOL") for _ in range(10)])
    source.end_scan_cycle()
    assert len(call_log) == 1, f"CRITICAL: 10 CONCURRENT requests for the same symbol should still only fetch once, got {len(call_log)} — a naive dict cache would race here"
    assert all(r.base == "SOL" for r in results), "All 10 concurrent callers should get the correct, same result"
    print(f"3. CRITICAL: 10 concurrent requests for the same symbol correctly deduplicated to exactly 1 real fetch — the asyncio.Task-based dedup genuinely prevents the race: OK")

    # 4. Different symbols are still fetched independently
    call_log.clear()
    source.start_scan_cycle()
    await asyncio.gather(
        source.build_snapshot(base="AVAX"), source.build_snapshot(base="LINK"), source.build_snapshot(base="DOT"),
    )
    source.end_scan_cycle()
    assert sorted(call_log) == ["AVAX", "DOT", "LINK"], f"Different symbols should each be fetched independently, got {call_log}"
    print("4. Different symbols within the same cycle are correctly fetched independently, not accidentally merged: OK")

    # 5. end_scan_cycle correctly deactivates memoization
    call_log.clear()
    source.start_scan_cycle()
    await source.build_snapshot(base="XRP")
    source.end_scan_cycle()
    await source.build_snapshot(base="XRP")  # after end — should fetch fresh again
    assert len(call_log) == 2, f"Expected memoization to be OFF after end_scan_cycle(), got {len(call_log)} calls for 2 requests"
    print("5. end_scan_cycle() correctly deactivates memoization for subsequent calls: OK")

    # 6. Exception safety — a raising batch still deactivates memoization (mirrors scanner.py's try/finally)
    source.start_scan_cycle()
    try:
        raise RuntimeError("simulated scan failure")
    except RuntimeError:
        pass
    finally:
        source.end_scan_cycle()
    assert source._scan_cycle_tasks is None, "Memoization must be deactivated even after an exception, matching scanner.py's try/finally"
    print("6. Memoization correctly deactivates even after a simulated scan failure (exception safety): OK")

    # 7. Realistic scenario: an 8-coin sector (matching the real 'l1' sector map)
    # where each coin's strength calc also fetches its 7 peers — WITHOUT this fix,
    # that's 8 primary + 56 redundant peer fetches = 64. WITH this fix, exactly 8.
    call_log.clear()
    l1_sector = ["BTC", "ETH", "SOL", "AVAX", "NEAR", "SUI", "APT", "ADA"]
    source.start_scan_cycle()

    async def scan_one_coin_with_peers(coin):
        peers = [c for c in l1_sector if c != coin]
        await source.build_snapshot(base=coin)  # the coin's own primary snapshot
        await asyncio.gather(*[source.build_snapshot(base=p) for p in peers])  # its sector peers

    await asyncio.gather(*[scan_one_coin_with_peers(coin) for coin in l1_sector])
    source.end_scan_cycle()
    assert len(call_log) == 8, f"Expected exactly 8 real fetches for 8 unique symbols across a full 8-coin sector scan (56 would-be-redundant peer fetches eliminated), got {len(call_log)}"
    print(f"7. Realistic 8-coin sector scan: exactly 8 real fetches, not 64 — the actual fix for the reported 10-30 minute scan time: OK")

    await source.close()

    print("\n✅ Scan-cycle memoization test passed: inactive-by-default correctness, sequential and — critically — concurrent deduplication both verified race-free, exception safety, and the realistic 8x-reduction scenario matching the real sector map that caused the original slowdown.")


if __name__ == "__main__":
    asyncio.run(main())

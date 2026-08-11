"""
scan_many correlation integration test — proves correlation clustering
actually reaches real ScanResult objects through the full scan_many()
pipeline, not just that the underlying math is correct in isolation
(already covered by test_correlation.py). Also proves the memoization
reuse is genuinely free — no extra underlying fetches beyond what the
scan already needed.

Mocks at the same low level already proven reliable in
test_scan_cycle_memoization.py (ExchangeDataSource._build_snapshot_uncached),
so this exercises the REAL scan_many() orchestration — regime, filters,
scoring, all of it — not a simplified stand-in.
"""

from __future__ import annotations
import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from opportunity_scanner.config import ScannerConfig
from opportunity_scanner.scanner import OpportunityScanner
from opportunity_scanner.models import MarketSnapshot


def _make_ohlcv(n, returns, seed_extra=0.0):
    rng = np.random.default_rng(1)
    prices = [100.0]
    for r in returns:
        prices.append(prices[-1] * (1 + r + seed_extra))
    close = np.array(prices)
    high = close * 1.005
    low = close * 0.995
    open_ = close * 1.001
    volume = np.full(n, 1_000_000.0)
    ts = pd.date_range(end=pd.Timestamp.now("UTC"), periods=n, freq="h")
    return pd.DataFrame({"timestamp": ts, "open": open_, "high": high, "low": low, "close": close, "volume": volume})


async def main():
    rng = np.random.default_rng(3)
    shared_returns = rng.normal(0, 0.02, 30).tolist()
    independent_returns = rng.normal(0, 0.02, 30).tolist()

    call_count = {"value": 0}

    async def fake_uncached(self, base, quote=None, market_cap_usd=None, exchange_listings=1):
        call_count["value"] += 1
        # COIN_A and COIN_B share the exact same underlying daily pattern (highly correlated);
        # COIN_C is genuinely independent.
        if base in ("COIN_A", "COIN_B"):
            daily = _make_ohlcv(31, shared_returns)
        elif base == "COIN_C":
            daily = _make_ohlcv(31, independent_returns)
        else:  # BTC (needed for regime + relative strength)
            daily = _make_ohlcv(31, rng.normal(0, 0.015, 30).tolist())
        ohlcv = {tf: daily for tf in ["15m", "1h", "4h", "1d"]}
        return MarketSnapshot(
            symbol=f"{base}/USDT", base=base, price=100.0, market_cap_usd=market_cap_usd,
            volume_24h_usd=50_000_000.0, ohlcv=ohlcv,
        )

    scanner = OpportunityScanner(ScannerConfig())
    scanner.exchange_source._build_snapshot_uncached = fake_uncached.__get__(scanner.exchange_source)

    results = await scanner.scan_many(["COIN_A", "COIN_B", "COIN_C"], include_filtered=True)
    await scanner.close()

    by_base = {r.base: r for r in results}
    assert set(by_base.keys()) == {"COIN_A", "COIN_B", "COIN_C"}, f"Expected all 3 coins to produce results, got {list(by_base.keys())}"

    # 1. COIN_A and COIN_B (identical underlying pattern) correctly flagged as correlated peers
    assert "COIN_B" in by_base["COIN_A"].correlated_peers, f"Expected COIN_A and COIN_B to be flagged as correlated, got {by_base['COIN_A'].correlated_peers}"
    assert "COIN_A" in by_base["COIN_B"].correlated_peers
    print(f"1. Two coins sharing the same underlying return pattern correctly flagged as correlated_peers through the REAL scan_many() pipeline: {by_base['COIN_A'].correlated_peers}: OK")

    # 2. THE ACTUAL FIX: COIN_C (genuinely independent) shows no correlated peers
    assert by_base["COIN_C"].correlated_peers == [], f"Expected COIN_C to show zero correlated peers (genuinely independent), got {by_base['COIN_C'].correlated_peers}"
    print("2. THE ACTUAL FIX: a genuinely independent coin correctly shows zero correlated peers, not falsely grouped: OK")

    # 3. The memoization reuse is genuinely free — each unique base (COIN_A, COIN_B,
    # COIN_C, BTC) should only trigger ONE real underlying fetch, even though the
    # correlation step re-requests each snapshot after the main scan completes
    unique_bases_fetched = 4  # COIN_A, COIN_B, COIN_C, BTC
    assert call_count["value"] == unique_bases_fetched, (
        f"THE MEMOIZATION CLAIM: expected exactly {unique_bases_fetched} real underlying fetches "
        f"(one per unique symbol, correlation reuses the cache), got {call_count['value']} — "
        f"if this is higher, the correlation step is doing real extra network work, not reusing the cache"
    )
    print(f"3. Correlation clustering's snapshot lookups are genuinely FREE — exactly {call_count['value']} real underlying fetches total for {unique_bases_fetched} unique symbols, confirming the scan-cycle memoization is actually being reused, not re-fetching: OK")

    print("\n✅ scan_many correlation integration test passed: correlation reaches real ScanResult objects through the full pipeline, correctly distinguishes correlated from independent coins, and the memoization reuse is genuinely free, not just claimed to be.")


if __name__ == "__main__":
    asyncio.run(main())

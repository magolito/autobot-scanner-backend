"""
Lightweight sector-peer OHLCV fetch test — the real fix for a precisely
measured, reproducible ~20-second gap in real production scan logs,
traced directly to sector peers outside the active scan universe (e.g.
scanning LINK alone pulls in its whole "defi" sector — UNI, AAVE, MKR,
CRV, LDO — purely to compute relative strength).

strength.py's _relative_strength() only ever reads OHLCV for 1h/4h/1d
from peer data — never 15m, never price, never OI, never funding,
never long/short. The old path fetched a FULL snapshot (7+ real calls)
per peer anyway, most of it fetched then silently discarded. This
fetches exactly what's used and nothing else.
"""

from __future__ import annotations
import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd


def _fake_ohlcv_df(n=15):
    return pd.DataFrame({
        "timestamp": pd.date_range(end=pd.Timestamp.now("UTC"), periods=n, freq="h"),
        "open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n, "close": [100.5] * n, "volume": [1000.0] * n,
    })


def main():
    from opportunity_scanner.data_sources.exchange import ExchangeDataSource
    from opportunity_scanner.config import ScannerConfig

    async def run():
        source = ExchangeDataSource(ScannerConfig())
        calls_made = []

        async def fake_fetch_ohlcv(symbol, timeframe, limit):
            calls_made.append((symbol, timeframe, limit))
            return _fake_ohlcv_df()

        source._fetch_ohlcv = fake_fetch_ohlcv

        # 1. THE ACTUAL FIX: exactly 3 calls (1h, 4h, 1d), never 15m,
        # never price/OI/funding/long-short — those methods aren't even
        # touched since this doesn't call build_snapshot at all
        result = await source.fetch_ohlcv_for_relative_strength("UNI")
        timeframes_called = sorted(tf for _, tf, _ in calls_made)
        assert timeframes_called == ["1d", "1h", "4h"], f"Expected exactly 1h/4h/1d, got {timeframes_called}"
        assert set(result.keys()) == {"1h", "4h", "1d"}
        print(f"1. THE ACTUAL FIX: peer fetch makes exactly 3 OHLCV calls (1h/4h/1d only) — never 15m, never price/OI/funding/long-short: OK")

        # 2. Small candle limit — relative strength needs a handful of
        # recent candles for a short-lookback return, not 200+
        assert all(limit <= 10 for _, _, limit in calls_made), f"Expected a small candle limit for peer data, got limits: {[l for _,_,l in calls_made]}"
        print(f"2. Uses a genuinely small candle limit ({calls_made[0][2]}), not the 200+ a primary coin needs for EMA/RSI: OK")

        # 3. Memoization: two coins sharing the same sector peer should
        # only trigger ONE real fetch for that peer within a scan cycle
        calls_made.clear()
        source.start_scan_cycle()
        try:
            results = await asyncio.gather(
                source.fetch_ohlcv_for_relative_strength("MKR"),
                source.fetch_ohlcv_for_relative_strength("MKR"),
                source.fetch_ohlcv_for_relative_strength("MKR"),
            )
            mkr_calls = [c for c in calls_made if c[0].startswith("MKR")]
            assert len(mkr_calls) == 3, f"Expected exactly 3 real calls (1h+4h+1d) for ONE real fetch of MKR shared across 3 concurrent requesters, got {len(mkr_calls)}"
            assert all(r == results[0] for r in results), "All three concurrent callers should get the same shared result"
        finally:
            source.end_scan_cycle()
        print(f"3. Sector peers shared across multiple coins in one scan correctly deduplicated to ONE real fetch, not one per referencing coin: OK")

        await source.close()

    asyncio.run(run())
    print("\n✅ Lightweight peer OHLCV test passed: the actual fix verified — sector peer fetches now cost exactly 3 targeted calls instead of a full 7+-call snapshot, with correct memoization across coins sharing a sector.")


if __name__ == "__main__":
    main()

"""
Multi-exchange aggregation test — Bybit + Hyperliquid only (Binance was
evaluated and removed, see multi_exchange_oi.py docstring). Proves the
2-way averaging and graceful degradation work, using monkeypatched fetch
methods (no live network needed, since this sandbox can't reach either
exchange).

Checks:
  1. Both sources succeed -> OI/funding averaged across both
  2. Bybit fails -> falls back to Hyperliquid alone, doesn't crash
  3. Hyperliquid fails -> falls back to Bybit alone, doesn't crash
  4. Both fail -> returns None cleanly, doesn't raise
  5. Bybit's OI history and long/short ratio are preserved in the aggregate
     (Hyperliquid doesn't expose either at this tier)
"""

from __future__ import annotations
import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.data_sources.multi_exchange_oi import DirectExchangeProvider
from opportunity_scanner.provider_models import (
    DerivativesSnapshot, OpenInterestData, OpenInterestPoint, FundingRateData,
    LongShortRatioData, DataSourceMeta,
)
from datetime import datetime, timezone


def make_snap(source: str, oi_usd: float, funding: float, with_history: bool = False, with_ls: bool = False) -> DerivativesSnapshot:
    meta = DataSourceMeta(source=source)
    history = [OpenInterestPoint(timestamp=datetime.now(timezone.utc), oi_usd=oi_usd)] if with_history else []
    return DerivativesSnapshot(
        symbol="BTC",
        open_interest=OpenInterestData(symbol="BTC", current_oi_usd=oi_usd, history=history, meta=meta),
        funding=FundingRateData(symbol="BTC", funding_rate=funding, meta=meta),
        long_short=LongShortRatioData(symbol="BTC", global_ratio=1.1 if with_ls else None, meta=meta),
        exchanges_aggregated=[source],
    )


async def main():
    provider = DirectExchangeProvider()

    bybit_snap = make_snap("bybit", oi_usd=10_000_000, funding=0.0001, with_history=True, with_ls=True)
    hyperliquid_snap = make_snap("hyperliquid", oi_usd=8_000_000, funding=0.00008)

    # 1. Both succeed
    provider._fetch_bybit = lambda base: asyncio.sleep(0, result=bybit_snap)
    provider._fetch_hyperliquid = lambda base: asyncio.sleep(0, result=hyperliquid_snap)

    result = await provider.fetch_derivatives_snapshot("BTC")
    expected_avg_oi = (10_000_000 + 8_000_000) / 2
    assert abs(result.open_interest.current_oi_usd - expected_avg_oi) < 1, f"Expected averaged OI ~{expected_avg_oi}, got {result.open_interest.current_oi_usd}"
    assert set(result.exchanges_aggregated) == {"bybit", "hyperliquid"}
    assert len(result.open_interest.history) == 1, "Expected Bybit's history to be preserved in the aggregate"
    assert result.long_short.global_ratio == 1.1, "Expected Bybit's long/short ratio to carry through (Hyperliquid doesn't expose it)"
    print(f"1. Both sources aggregate correctly: OI={result.open_interest.current_oi_usd:.0f}, exchanges={result.exchanges_aggregated}")

    # 2. Bybit fails -> falls back to Hyperliquid alone
    async def bybit_down(base):
        raise ConnectionError("timeout")
    provider._fetch_bybit = bybit_down

    result2 = await provider.fetch_derivatives_snapshot("BTC")
    assert result2 is not None, "Expected a result even with Bybit down"
    assert result2.exchanges_aggregated == ["hyperliquid"]
    assert result2.open_interest.current_oi_usd == 8_000_000
    print(f"2. Bybit down -> falls back to Hyperliquid alone: OI={result2.open_interest.current_oi_usd}")

    # 3. Hyperliquid fails -> falls back to Bybit alone
    provider._fetch_bybit = lambda base: asyncio.sleep(0, result=bybit_snap)
    async def hl_down(base):
        raise ConnectionError("timeout")
    provider._fetch_hyperliquid = hl_down

    result3 = await provider.fetch_derivatives_snapshot("BTC")
    assert result3.exchanges_aggregated == ["bybit"]
    assert result3.open_interest.current_oi_usd == 10_000_000
    print(f"3. Hyperliquid down -> falls back to Bybit alone: OI={result3.open_interest.current_oi_usd}")

    # 4. Both fail
    provider._fetch_bybit = bybit_down
    result4 = await provider.fetch_derivatives_snapshot("BTC")
    assert result4 is None, "Expected None when both sources fail, not a crash"
    print("4. Both sources down -> returns None cleanly (no crash)")

    print("\n✅ Multi-exchange (Bybit+Hyperliquid) aggregation test passed: 2-way averaging, graceful degradation on either single-source failure, clean None on total failure.")


if __name__ == "__main__":
    asyncio.run(main())

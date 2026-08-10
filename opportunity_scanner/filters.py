"""
Noise/quality filters.

These run BEFORE scoring. A coin that fails these is excluded from results
entirely (or shown separately as "filtered out") rather than scored low —
the goal is to keep the scanner's output free of illiquid, unlistable,
or data-starved symbols that would otherwise produce misleadingly extreme
scores in either direction.
"""

from __future__ import annotations
from typing import List, Tuple
from .config import QualityFilters
from .models import MarketSnapshot


def passes_quality_filters(snap: MarketSnapshot, filters: QualityFilters) -> Tuple[bool, List[str]]:
    notes: List[str] = []
    ok = True

    if snap.volume_24h_usd < filters.min_24h_volume_usd:
        ok = False
        notes.append(
            f"24h volume ${snap.volume_24h_usd:,.0f} below minimum "
            f"${filters.min_24h_volume_usd:,.0f}"
        )

    if snap.market_cap_usd is not None and snap.market_cap_usd < filters.min_market_cap_usd:
        ok = False
        notes.append(
            f"Market cap ${snap.market_cap_usd:,.0f} below minimum "
            f"${filters.min_market_cap_usd:,.0f}"
        )

    if snap.exchange_listings < filters.min_exchange_listings:
        ok = False
        notes.append(
            f"Listed on {snap.exchange_listings} exchange(s), "
            f"below minimum {filters.min_exchange_listings}"
        )

    if snap.bid_ask_spread_pct is not None and snap.bid_ask_spread_pct > filters.max_bid_ask_spread_pct:
        ok = False
        notes.append(
            f"Bid/ask spread {snap.bid_ask_spread_pct:.2f}% wider than "
            f"maximum {filters.max_bid_ask_spread_pct:.2f}% (thin book)"
        )

    for tf, df in snap.ohlcv.items():
        if df is None or len(df) < filters.require_min_candles:
            ok = False
            n = 0 if df is None else len(df)
            notes.append(
                f"Only {n} candles on {tf}, below minimum {filters.require_min_candles} "
                f"— not enough history for reliable indicators"
            )

    if ok:
        notes.append("Passed all quality filters")

    return ok, notes

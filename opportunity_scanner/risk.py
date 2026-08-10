"""
Risk tier — deliberately kept SEPARATE from the composite Opportunity Score.

A brand-new, thinly-traded coin with genuine momentum, OI confirmation, and
real social virality should still score highly — the point of this scanner
is to surface real strength wherever it appears, not just in blue chips.
But the person reading the output needs to know the liquidity/maturity
context alongside the score, so risk tier ships as its own field rather
than being blended into (and thus muddying) the score itself.
"""

from __future__ import annotations
from typing import Optional


def classify_risk_tier(
    market_cap_rank: Optional[int],
    market_cap_usd: Optional[float],
    volume_24h_usd: float,
    core_rank_cutoff: int = 100,
    small_cap_rank_cutoff: int = 300,
    high_risk_market_cap_usd: float = 25_000_000,
) -> str:
    if market_cap_rank is None:
        return "high_risk"
    if market_cap_rank <= core_rank_cutoff:
        return "core"
    if market_cap_rank <= small_cap_rank_cutoff:
        # even inside the small-cap rank band, very low absolute market cap
        # or volume still gets flagged high risk
        if market_cap_usd is not None and market_cap_usd < high_risk_market_cap_usd:
            return "high_risk"
        return "small_cap"
    return "high_risk"

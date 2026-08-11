"""
Risk tier — deliberately kept SEPARATE from the composite Opportunity Score.

A brand-new, thinly-traded coin with genuine momentum, OI confirmation, and
real social virality should still score highly — the point of this scanner
is to surface real strength wherever it appears, not just in blue chips.
But the person reading the output needs to know the liquidity/maturity
context alongside the score, so risk tier ships as its own field rather
than being blended into (and thus muddying) the score itself.

Real fix from a direct quant-style review: market cap rank alone is a
weak proxy for actual risk. A $500M coin with deep, tight order books is
genuinely a different risk than a $500M coin nobody's trading — rank
doesn't distinguish them, and this classifier used to accept a
volume_24h_usd parameter and never actually use it at all. Now: rank sets
a base tier, then liquidity (volume relative to the coin's own size) and
realized volatility can only DOWNGRADE it, never upgrade — a coin doesn't
get to buy its way to a safer tier just by being large or calm; it can
only get flagged riskier for genuinely thin liquidity or abnormal
volatility that rank alone would miss.
"""

from __future__ import annotations
from typing import Optional
import math
import pandas as pd


def compute_realized_volatility(daily_ohlcv: Optional[pd.DataFrame], lookback_days: int = 20) -> Optional[float]:
    """Annualized realized volatility from daily closes — same calc
    regime.py already uses for BTC's own volatility read, extracted here
    as a reusable helper so per-coin risk classification can use the
    identical, already-tested method rather than a second one that could
    drift out of sync."""
    if daily_ohlcv is None or len(daily_ohlcv) <= lookback_days:
        return None
    returns = daily_ohlcv["close"].pct_change().dropna()
    recent_returns = returns.tail(lookback_days)
    if len(recent_returns) <= 1 or recent_returns.std() <= 0:
        return None
    return recent_returns.std() * math.sqrt(365)


def classify_risk_tier(
    market_cap_rank: Optional[int],
    market_cap_usd: Optional[float],
    volume_24h_usd: float,
    realized_volatility_annualized: Optional[float] = None,
    core_rank_cutoff: int = 100,
    small_cap_rank_cutoff: int = 300,
    high_risk_market_cap_usd: float = 25_000_000,
    min_liquidity_ratio: float = 0.02,
    extreme_volatility_annualized: float = 1.5,
) -> str:
    """
    min_liquidity_ratio: daily volume should be at least ~2% of market
    cap for healthy liquidity — majors typically run well above this
    (often 5-15%+); a coin genuinely thin relative to its OWN size gets
    flagged regardless of nominal rank, since that thinness is exactly
    what makes entering/exiting at real size costly.

    extreme_volatility_annualized: 150% annualized realized vol. Crypto
    is volatile by nature (alts routinely run 80-150%+ even in normal
    conditions), so this is deliberately looser than regime.py's own
    BTC-specific "extreme" threshold (120%) — BTC is the calmest major
    asset in this market, and reusing its threshold for every coin would
    over-flag normal altcoin volatility as abnormal.
    """
    if market_cap_rank is None:
        return "high_risk"

    if market_cap_rank <= core_rank_cutoff:
        base_tier = "core"
    elif market_cap_rank <= small_cap_rank_cutoff:
        if market_cap_usd is not None and market_cap_usd < high_risk_market_cap_usd:
            return "high_risk"
        base_tier = "small_cap"
    else:
        return "high_risk"

    liquidity_ratio = (volume_24h_usd / market_cap_usd) if market_cap_usd and market_cap_usd > 0 else None
    if liquidity_ratio is not None and liquidity_ratio < min_liquidity_ratio:
        return "high_risk"  # thin relative to its own size, regardless of nominal rank — real, not implied

    if realized_volatility_annualized is not None and realized_volatility_annualized > extreme_volatility_annualized:
        # Extreme volatility downgrades one tier rather than jumping
        # straight to high_risk — abnormally violent right now is a real
        # risk signal, but not automatically equivalent to "illiquid or
        # unranked," which is what high_risk otherwise represents.
        return "small_cap" if base_tier == "core" else "high_risk"

    return base_tier

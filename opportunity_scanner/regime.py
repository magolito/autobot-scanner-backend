"""
Regime Awareness (BTC Filter)
-------------------------------
Every pillar so far scores a coin in isolation. This module answers a
different question: is the overall market in a state where a bullish
call on an ALTCOIN should be trusted?

Alts overwhelmingly beta-trade off BTC. A "Strong Buy" on some alt while
BTC itself is breaking down is much more likely to be a relief bounce
inside a larger downtrend than genuine independent strength. This module
computes BTC's own regime once per scan cycle (not per-coin — it's a
property of the market, not of any individual coin) and applies a
dampener to bullish-leaning scores on other coins when that regime looks
unhealthy. It never inflates a score, and it never touches bearish/
neutral calls — a "Caution" during Risk-Off needs no extra scrutiny.
"""

from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import List, Optional
import pandas as pd

from .config import RegimeConfig, TimeframeConfig
from .models import MarketSnapshot
from .factors.momentum import compute_momentum


def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def _normalize(v: float, lo: float, hi: float) -> float:
    if hi == lo:
        return 50.0
    return _clamp((v - lo) / (hi - lo) * 100.0)


@dataclass
class RegimeResult:
    label: str                       # "Risk-On" | "Neutral" | "Risk-Off"
    score: float                     # 0-100
    btc_momentum_score: float
    volatility_score: float
    realized_vol_annualized: Optional[float]
    reasons: List[str] = field(default_factory=list)


def compute_market_regime(
    btc_snapshot: MarketSnapshot,
    timeframe_config: TimeframeConfig,
    regime_config: RegimeConfig,
) -> RegimeResult:
    btc_momentum = compute_momentum(btc_snapshot, timeframe_config)

    daily = btc_snapshot.ohlcv.get("1d")
    realized_vol = None
    vol_score = 50.0
    if daily is not None and len(daily) > regime_config.volatility_lookback_days:
        returns = daily["close"].pct_change().dropna()
        recent_returns = returns.tail(regime_config.volatility_lookback_days)
        if len(recent_returns) > 1 and recent_returns.std() > 0:
            realized_vol = recent_returns.std() * math.sqrt(365)
            # lower volatility = healthier regime = higher score
            vol_score = 100 - _normalize(
                realized_vol, regime_config.volatility_normalize_lo, regime_config.volatility_normalize_hi
            )

    regime_score = btc_momentum.score * 0.7 + vol_score * 0.3

    if regime_score >= regime_config.risk_on_threshold:
        label = "Risk-On"
    elif regime_score <= regime_config.risk_off_threshold:
        label = "Risk-Off"
    else:
        label = "Neutral"

    reasons = [
        f"BTC momentum score {btc_momentum.score:.0f}/100",
        f"BTC realized volatility (20d, annualized): {realized_vol*100:.0f}%" if realized_vol is not None else "Insufficient data for volatility read",
        f"Regime: {label} (composite {regime_score:.0f}/100)",
    ]

    return RegimeResult(
        label=label,
        score=round(_clamp(regime_score), 1),
        btc_momentum_score=btc_momentum.score,
        volatility_score=round(vol_score, 1),
        realized_vol_annualized=realized_vol,
        reasons=reasons,
    )


def apply_regime_filter(
    composite_score: float,
    regime: RegimeResult,
    regime_config: RegimeConfig,
    is_btc_itself: bool,
) -> tuple[float, Optional[str]]:
    """
    Returns (possibly-adjusted score, explanatory note or None if unchanged).
    BTC never dampens itself — it's the regime anchor, not a subject of it.
    Only bullish-leaning scores (above dampen_above_score) are touched,
    and only when regime is Risk-Off. Bearish/neutral calls pass through
    unchanged — a low score during Risk-Off doesn't need extra scrutiny,
    it's already consistent with the regime.
    """
    if is_btc_itself:
        return composite_score, None

    if regime.label == "Risk-Off" and composite_score > regime_config.dampen_above_score:
        adjusted = _clamp(composite_score - regime_config.risk_off_dampener_points)
        note = (
            f"Dampened {regime_config.risk_off_dampener_points:.0f}pts: BTC regime is Risk-Off "
            f"(regime score {regime.score:.0f}/100) — bullish signals need extra scrutiny while BTC is unhealthy"
        )
        return adjusted, note

    return composite_score, None

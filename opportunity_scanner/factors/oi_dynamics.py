"""
Pillar 2: Open Interest Dynamics
----------------------------------
This is the derivatives-market read: is new leveraged money actually
entering in the direction price is moving, or is price moving on thin
positioning that could snap back?

Four inputs, each explainable on its own:
  - OI change over the scan window
  - Price vs OI divergence (the core signal — see _divergence_score)
  - Funding rate (extreme funding = crowded positioning, contrarian risk)
  - Long/short account ratio, where available (Bybit exposes this)

Requires snap.open_interest_history (a DataFrame with columns ts, oi_usd)
and a recent price return to compare it against. If OI data isn't
available for a symbol (not all coins have perps), this pillar returns
available=False and the composite scorer redistributes its weight.
"""

from __future__ import annotations
from typing import Optional
import pandas as pd

from ..models import MarketSnapshot, FactorResult


def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def _normalize(v: float, lo: float, hi: float) -> float:
    if hi == lo:
        return 50.0
    return _clamp((v - lo) / (hi - lo) * 100.0)


def _oi_change_pct(oi_history: pd.DataFrame) -> Optional[float]:
    if oi_history is None or len(oi_history) < 2:
        return None
    start = oi_history["oi_usd"].iloc[0]
    end = oi_history["oi_usd"].iloc[-1]
    if not start:
        return None
    return (end / start - 1.0) * 100.0


def _divergence_score(price_change_pct: float, oi_change_pct: float) -> tuple[float, str]:
    """
    The core read:
      price UP + OI UP     -> new longs entering, real conviction    (strong)
      price UP + OI DOWN   -> short covering, not new demand         (weak-ish)
      price DOWN + OI UP   -> new shorts entering, real conviction (bearish, but "confirmed")
      price DOWN + OI DOWN -> long liquidation / capitulation fading (weak-ish)
    We score for "trend is confirmed by fresh positioning," direction-agnostic,
    since this pillar measures conviction strength, not direction —
    direction itself comes from the Momentum pillar.
    """
    aligned = (price_change_pct >= 0) == (oi_change_pct >= 0)
    magnitude = _normalize(abs(oi_change_pct), 0, 20)

    if aligned:
        score = _clamp(60 + magnitude * 0.4)
        label = (
            f"OI {oi_change_pct:+.1f}% moving WITH price ({price_change_pct:+.1f}%) "
            f"— fresh positioning confirms the move"
        )
    else:
        score = _clamp(40 - magnitude * 0.4)
        label = (
            f"OI {oi_change_pct:+.1f}% moving AGAINST price ({price_change_pct:+.1f}%) "
            f"— move looks like covering/liquidation, not fresh conviction"
        )
    return score, label


def _funding_score(funding_rate: Optional[float]) -> tuple[float, list[str]]:
    """
    Funding near zero = healthy, balanced positioning (higher score).
    Extreme funding (either direction) = crowded trade, elevated
    reversal/squeeze risk (lower score) — this is a risk read, not a
    directional one.
    """
    if funding_rate is None:
        return 50.0, ["No funding rate data — neutral default"]
    # typical perp funding is quoted per 8h, e.g. 0.01% = 0.0001
    abs_bps = abs(funding_rate) * 10000  # convert to basis points
    score = _clamp(100 - _normalize(abs_bps, 0, 50))
    note = f"Funding rate {funding_rate*100:.3f}% ({abs_bps:.1f}bps) — "
    note += "balanced, low crowding risk" if abs_bps < 10 else "elevated, crowded positioning"
    return score, [note]


def _long_short_score(ratio: Optional[float]) -> tuple[float, list[str]]:
    """
    Long/short account ratio far from 1.0 in either direction signals a
    crowded/lopsided market — treated as a mild risk discount, same logic
    as funding.
    """
    if ratio is None:
        return 50.0, ["No long/short ratio data available — neutral default"]
    deviation = abs(ratio - 1.0)
    score = _clamp(100 - _normalize(deviation, 0, 2))
    note = f"Long/short account ratio {ratio:.2f} — "
    note += "balanced" if deviation < 0.3 else "lopsided, crowded positioning"
    return score, [note]


def _oi_mcap_dampener(open_interest_usd: Optional[float], market_cap_usd: Optional[float]) -> tuple[float, list[str]]:
    """
    High OI relative to market cap = heavy derivatives interest relative
    to the coin's actual size — elevated liquidation-cascade/volatility
    risk. Only dampens above a 70/100 threshold; below that it's neutral
    (we don't want to penalize normal derivatives activity, only extremes).
    """
    if not open_interest_usd or not market_cap_usd or market_cap_usd <= 0:
        return 0.0, []
    ratio = open_interest_usd / market_cap_usd
    ratio_score = _normalize(ratio, 0, 0.5)
    dampener = max(0.0, ratio_score - 70) * 0.10
    note = []
    if dampener > 0:
        note.append(f"OI is {ratio*100:.0f}% of market cap — elevated leverage risk, score dampened")
    return dampener, note


def compute_oi_dynamics(snap: MarketSnapshot, price_change_24h_pct: Optional[float]) -> FactorResult:
    if snap.open_interest_history is None or price_change_24h_pct is None:
        return FactorResult(
            name="oi_dynamics",
            score=50.0,
            reasons=["No open interest data for this symbol (spot-only or unlisted on derivatives) — pillar excluded"],
            available=False,
        )

    oi_change = _oi_change_pct(snap.open_interest_history)
    reasons: list[str] = []

    if oi_change is None:
        return FactorResult(
            name="oi_dynamics",
            score=50.0,
            reasons=["Not enough OI history yet to compute change — neutral default"],
            available=False,
        )

    div_score, div_note = _divergence_score(price_change_24h_pct, oi_change)
    reasons.append(div_note)

    fund_score, fund_reasons = _funding_score(snap.funding_rate)
    reasons.extend(fund_reasons)

    ls_score, ls_reasons = _long_short_score(snap.long_short_ratio)
    reasons.extend(ls_reasons)

    # Divergence/confirmation is the dominant signal; funding & long-short are risk modifiers
    composite = div_score * 0.60 + fund_score * 0.20 + ls_score * 0.20

    dampener, dampener_notes = _oi_mcap_dampener(snap.open_interest_usd, snap.market_cap_usd)
    reasons.extend(dampener_notes)
    composite = _clamp(composite - dampener)

    return FactorResult(
        name="oi_dynamics",
        score=round(_clamp(composite), 1),
        reasons=reasons,
        raw={"oi_change_pct": oi_change, "div_score": div_score, "fund_score": fund_score, "ls_score": ls_score},
        available=True,
    )

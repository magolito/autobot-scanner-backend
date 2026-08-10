"""
Degen-tier data models — on-chain/DEX pair data (DexScreener), covering
memecoins and pump.fun-origin tokens once they have DEX liquidity.

Deliberately NOT part of provider_models.py's DerivativesSnapshot/
SocialMetrics family, and NOT fed into the four-pillar Opportunity Score.
This is a different risk category (thin liquidity, high manipulation
risk, extreme volatility) that gets its own clearly-labeled surface
("Degen Radar") rather than diluting the main scanner's credibility —
same reasoning as the risk-tier separation elsewhere in this codebase,
just drawn at a starker line here because the underlying assets are
fundamentally riskier, not just smaller.
"""

from __future__ import annotations
from enum import Enum
from typing import Optional, List
from pydantic import BaseModel
from .provider_models import DataSourceMeta


class PairVenue(str, Enum):
    """
    Pump.fun tokens go through two structurally different phases, and
    conflating them is a real correctness bug, not a cosmetic one: a
    still-on-the-curve token has no traditional LP to lock (the bonding
    curve itself IS the market maker), so an "LP locked %" safety check
    would either misfire (checking a field that doesn't apply) or
    silently return None and get treated as missing data rather than
    N/A. Classify the venue first, apply the right checks second.
    """
    BONDING_CURVE = "bonding_curve"      # still on pump.fun's curve, not yet graduated — no traditional LP
    PUMPSWAP = "pumpswap"                  # graduated from pump.fun to its own AMM
    RAYDIUM = "raydium"
    OTHER_DEX = "other_dex"


def classify_pair_venue(dex_id: str) -> PairVenue:
    dex_id_lower = (dex_id or "").lower()
    if dex_id_lower in ("pumpfun", "pump.fun", "pump-fun"):
        return PairVenue.BONDING_CURVE
    if dex_id_lower in ("pumpswap", "pump-swap"):
        return PairVenue.PUMPSWAP
    if dex_id_lower == "raydium":
        return PairVenue.RAYDIUM
    return PairVenue.OTHER_DEX


class DexTransactionCounts(BaseModel):
    buys: int = 0
    sells: int = 0

    @property
    def buy_sell_ratio(self) -> Optional[float]:
        if self.sells == 0:
            return None
        return self.buys / self.sells


class TimeframeStats(BaseModel):
    """One timeframe bucket (5m/1h/6h/24h) of volume + price change — DexScreener's
    full pair response includes all four; the original DexPair model only
    captured 24h, which was too coarse for momentum/hype-velocity reads
    that need to see what's happening in the last few minutes, not the
    last day."""
    price_change_pct: Optional[float] = None
    volume_usd: Optional[float] = None
    buys: Optional[int] = None
    sells: Optional[int] = None


class DexPair(BaseModel):
    chain_id: str                       # e.g. "solana"
    dex_id: str                          # e.g. "raydium", "pumpswap", "pumpfun"
    venue: PairVenue = PairVenue.OTHER_DEX
    pair_address: str
    base_symbol: str
    base_token_address: str
    quote_symbol: str
    price_usd: Optional[float] = None
    liquidity_usd: Optional[float] = None
    market_cap_usd: Optional[float] = None
    volume_24h_usd: Optional[float] = None
    price_change_24h_pct: Optional[float] = None
    price_change_1h_pct: Optional[float] = None
    txns_24h: Optional[DexTransactionCounts] = None
    pair_created_at: Optional[str] = None    # ISO timestamp, None if unknown
    fdv_usd: Optional[float] = None           # fully diluted valuation
    meta: DataSourceMeta

    # Multi-timeframe buckets — needed for hype-velocity and momentum-
    # acceleration reads that a single 24h number can't support
    m5: Optional[TimeframeStats] = None
    h1: Optional[TimeframeStats] = None
    h6: Optional[TimeframeStats] = None
    h24: Optional[TimeframeStats] = None

    # Boost/trending status (DexScreener's own paid promotion signal —
    # real-time, unlike social-mention data which lags for brand-new tokens)
    is_boosted: bool = False
    boost_amount: Optional[float] = None

    # Bonding-curve-specific (only meaningful when venue == BONDING_CURVE)
    bonding_curve_progress_pct: Optional[float] = None

    # Social links, straight from DexScreener's pair info — cheap presence check
    has_website: bool = False
    has_twitter: bool = False
    has_telegram: bool = False


class DegenFlag(BaseModel):
    label: str
    severity: str    # "info" | "warning" | "danger"


class DegenSnapshot(BaseModel):
    """
    Risk-forward summary for one token — deliberately NOT a 0-100 score
    matching the main pillars' shape, so nobody mistakes this for
    something with the same rigor/confidence as the Opportunity Score.
    Flags are the primary output; raw metrics are secondary.
    """
    symbol: str
    token_address: str
    chain_id: str
    price_usd: Optional[float] = None
    liquidity_usd: Optional[float] = None
    volume_24h_usd: Optional[float] = None
    price_change_1h_pct: Optional[float] = None
    price_change_24h_pct: Optional[float] = None
    buy_sell_ratio_24h: Optional[float] = None
    pair_age_hours: Optional[float] = None
    flags: List[DegenFlag] = []
    source: str = "dexscreener"

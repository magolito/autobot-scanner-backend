"""
Pydantic data contracts for the provider layer.

These are the clean, validated shapes every provider returns, regardless
of which underlying API produced them. Two things every one of these
carries that a raw API response wouldn't: `source` (which provider
actually answered — critical once there's a fallback chain) and
`is_stale` (whether this is fresh data or a cached/fallback value being
served because the live call failed). A caller should always be able to
tell "how much should I trust this," not just "what is the number."
"""

from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional, List
from pydantic import BaseModel, Field


class DataSourceMeta(BaseModel):
    source: str                              # e.g. "bybit", "coinglass", "lunarcrush"
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_stale: bool = False                    # True if served from cache past its intended freshness, or a fallback
    is_fallback: bool = False                  # True if the primary source failed and this came from a fallback provider


class OHLCVBar(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class OHLCVSeries(BaseModel):
    symbol: str
    timeframe: str
    bars: List[OHLCVBar]
    meta: DataSourceMeta


class TickerData(BaseModel):
    symbol: str
    price: float
    volume_24h_usd: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_ask_spread_pct: Optional[float] = None
    meta: DataSourceMeta


class OpenInterestPoint(BaseModel):
    timestamp: datetime
    oi_usd: float


class OpenInterestData(BaseModel):
    symbol: str
    current_oi_usd: Optional[float] = None
    history: List[OpenInterestPoint] = []
    meta: DataSourceMeta

    def change_pct(self) -> Optional[float]:
        if len(self.history) < 2:
            return None
        start, end = self.history[0].oi_usd, self.history[-1].oi_usd
        if not start:
            return None
        return (end / start - 1.0) * 100.0


class FundingRateData(BaseModel):
    symbol: str
    funding_rate: Optional[float] = None
    funding_rate_prev: Optional[float] = None
    meta: DataSourceMeta


class LongShortRatioData(BaseModel):
    symbol: str
    global_ratio: Optional[float] = None       # all-account long/short ratio
    top_trader_ratio: Optional[float] = None    # None unless a source exposes it (e.g. CoinGlass)
    meta: DataSourceMeta


class DerivativesSnapshot(BaseModel):
    """Bundled OI + funding + long/short for one symbol — what MultiExchangeOIProvider returns."""
    symbol: str
    open_interest: OpenInterestData
    funding: FundingRateData
    long_short: LongShortRatioData
    exchanges_aggregated: List[str] = []       # which exchanges actually contributed data


class SocialMetrics(BaseModel):
    symbol: str
    galaxy_score: Optional[float] = None
    alt_rank: Optional[int] = None
    galaxy_score_prior: Optional[float] = None
    social_volume_24h: Optional[float] = None
    social_volume_baseline_7d: Optional[float] = None
    social_volume_baseline_30d: Optional[float] = None
    sentiment: Optional[float] = None
    sentiment_prev: Optional[float] = None
    engagement_score: Optional[float] = None
    meta: DataSourceMeta


class MarketMetadata(BaseModel):
    symbol: str
    market_cap_usd: Optional[float] = None
    market_cap_rank: Optional[int] = None
    exchange_listings: int = 1
    meta: DataSourceMeta

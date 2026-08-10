"""Shared data models for the Opportunity Scanner pipeline."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import pandas as pd


@dataclass
class MarketSnapshot:
    """Raw market data for one symbol, pulled fresh each scan cycle."""
    symbol: str                              # e.g. "BTC/USDT"
    base: str                                # e.g. "BTC"
    price: float
    market_cap_usd: Optional[float] = None
    volume_24h_usd: float = 0.0
    bid_ask_spread_pct: Optional[float] = None
    exchange_listings: int = 1
    ohlcv: Dict[str, pd.DataFrame] = field(default_factory=dict)   # timeframe -> OHLCV df
    open_interest_usd: Optional[float] = None
    open_interest_history: Optional[pd.DataFrame] = None           # columns: ts, oi_usd
    funding_rate: Optional[float] = None
    long_short_ratio: Optional[float] = None                       # >1 = more longs than shorts
    social: Optional[Dict] = None                                  # raw social metrics blob


@dataclass
class FactorResult:
    """
    Output of a single pillar (strength / oi_dynamics / momentum / social).
    `score` is always 0-100. `reasons` is the explainability trail — every
    factor must justify its own number in plain language.
    """
    name: str
    score: float                    # 0-100, or None if pillar couldn't be computed
    reasons: List[str] = field(default_factory=list)
    raw: Dict = field(default_factory=dict)   # intermediate numbers, useful for debugging/UI
    available: bool = True          # False if data source was missing entirely


@dataclass
class ScanResult:
    """Final composite output for one coin — this is what the API/UI renders."""
    symbol: str
    base: str
    price: float
    composite_score: float
    confidence: float                # 0-100 — how trustworthy the score is (data completeness + pillar agreement)
    confidence_label: str            # "High" | "Medium" | "Low"
    signal: str
    factors: Dict[str, FactorResult]
    weights_used: Dict[str, float]
    reasons_summary: List[str]      # top 3-5 human-readable reasons, across all pillars
    risk_tier: str                  # "core" | "small_cap" | "high_risk"
    passed_filters: bool
    filter_notes: List[str] = field(default_factory=list)
    regime_label: str = "Unknown"           # "Risk-On" | "Neutral" | "Risk-Off" | "Unknown"
    regime_score: Optional[float] = None
    regime_adjustment_note: Optional[str] = None
    score_before_regime_adjustment: Optional[float] = None

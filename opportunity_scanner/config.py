"""
Central configuration for the Opportunity Scanner.

Everything a person is likely to want to tune lives here: pillar weights,
quality/noise filters, timeframes, and signal grade bands. Import
ScannerConfig and override fields rather than editing factor code.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Weights:
    """Composite score weights. Must sum to 1.0 — validated in __post_init__."""
    strength: float = 0.22
    oi_dynamics: float = 0.28
    momentum: float = 0.25
    social: float = 0.25

    def __post_init__(self):
        total = self.strength + self.oi_dynamics + self.momentum + self.social
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"Weights must sum to 1.0, got {total:.4f}. "
                f"({self.strength=}, {self.oi_dynamics=}, {self.momentum=}, {self.social=})"
            )

    def as_dict(self) -> Dict[str, float]:
        return {
            "strength": self.strength,
            "oi_dynamics": self.oi_dynamics,
            "momentum": self.momentum,
            "social": self.social,
        }

    def redistribute_missing(self, missing: List[str]) -> "Weights":
        """
        Return a new Weights with `missing` pillars zeroed out and their
        weight redistributed proportionally across the remaining pillars.
        Used when a data source (e.g. social) is unavailable for a symbol.
        """
        d = self.as_dict()
        missing_total = sum(d[k] for k in missing)
        if missing_total <= 0:
            return self
        remaining_keys = [k for k in d if k not in missing]
        remaining_total = sum(d[k] for k in remaining_keys)
        if remaining_total <= 0:
            raise ValueError("Cannot redistribute — no remaining weight to spread to.")
        new_d = dict(d)
        for k in missing:
            new_d[k] = 0.0
        for k in remaining_keys:
            new_d[k] = d[k] + missing_total * (d[k] / remaining_total)
        return Weights(**new_d)


@dataclass
class QualityFilters:
    """Noise filters — coins failing these are excluded before scoring, not scored low."""
    min_24h_volume_usd: float = 250_000
    min_market_cap_usd: float = 3_000_000
    min_exchange_listings: int = 1          # must be listed on at least N tracked exchanges
    max_bid_ask_spread_pct: float = 1.5      # reject if spread wider than this (thin book)
    require_min_candles: int = 50            # need enough history for indicators to be meaningful


@dataclass
class TimeframeConfig:
    """Multi-timeframe set used for momentum/trend analysis, and their relative weight."""
    timeframes: List[str] = field(default_factory=lambda: ["15m", "1h", "4h", "1d"])
    timeframe_weights: Dict[str, float] = field(default_factory=lambda: {
        "15m": 0.10,
        "1h": 0.25,
        "4h": 0.30,
        "1d": 0.35,
    })
    candles_per_timeframe: int = 250  # need 200+ for EMA200 in the momentum pillar


@dataclass
class SignalBands:
    """Composite score (0-100) -> discrete signal grade."""
    strong_buy: float = 80.0
    buy: float = 65.0
    neutral: float = 45.0
    caution: float = 25.0
    # below `caution` = Strong Avoid

    def grade(self, score: float) -> str:
        if score >= self.strong_buy:
            return "Strong Buy"
        if score >= self.buy:
            return "Buy"
        if score >= self.neutral:
            return "Neutral"
        if score >= self.caution:
            return "Caution"
        return "Strong Avoid"


@dataclass
class ConfidenceBands:
    """Confidence score (0-100) -> qualitative label, shown alongside the signal grade."""
    high: float = 75.0
    medium: float = 50.0
    # below `medium` = Low

    def label(self, confidence: float) -> str:
        if confidence >= self.high:
            return "High"
        if confidence >= self.medium:
            return "Medium"
        return "Low"


# Starter sector map for relative-strength-vs-sector comparisons.
# Expand this as your coverage grows — anything not listed here is
# treated as its own single-coin "sector" (relative-strength-vs-sector
# gracefully falls back to relative-strength-vs-BTC only in that case).
# Coin universe presets for the dashboard's "Majors / High Liquidity /
# Full Universe / Custom" selector. Deliberately bounded even at the
# widest tier — "Full Universe" here is ~30 curated liquid coins, not a
# literal "scan everything," per the explicit performance requirement.
# This is a living list, not a permanent one — worth refreshing
# periodically as market cap rankings and liquidity shift; also editable
# via settings.yaml without touching code (see Settings.exchange or the
# dedicated universe_presets section).
UNIVERSE_PRESETS: Dict[str, List[str]] = {
    "Majors": ["BTC", "ETH", "SOL", "BNB", "XRP"],
    "High Liquidity": [
        "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX", "LINK", "DOT",
        "TRX", "LTC", "ATOM", "NEAR", "APT", "SUI",
    ],
    "Full Universe": [
        "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX", "LINK", "DOT",
        "TRX", "LTC", "ATOM", "NEAR", "APT", "SUI",
        "ARB", "OP", "INJ", "TIA", "SEI", "RENDER", "ICP", "FIL", "UNI", "AAVE",
        "MKR", "PEPE", "WIF", "BONK", "SHIB",
    ],
}
DEFAULT_UNIVERSE_PRESET = "High Liquidity"   # a new user's first-ever scan, before any preference is saved


DEFAULT_SECTOR_MAP: Dict[str, List[str]] = {
    "l1": ["BTC", "ETH", "SOL", "AVAX", "NEAR", "SUI", "APT", "ADA"],
    "l2": ["ARB", "OP", "MATIC", "STRK", "ZK"],
    "defi": ["UNI", "AAVE", "LINK", "MKR", "CRV", "LDO"],
    "ai": ["TAO", "FET", "RNDR", "AGIX", "WLD"],
    "memecoin": ["DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI"],
}


@dataclass
class RegimeConfig:
    """
    BTC regime filter thresholds. Regime is computed once per scan cycle
    from BTC's own momentum + realized volatility, then used to dampen
    (never inflate) bullish scores on OTHER coins when BTC itself looks
    unhealthy. See regime.py.
    """
    risk_on_threshold: float = 65.0
    risk_off_threshold: float = 35.0
    risk_off_dampener_points: float = 12.0   # subtracted from bullish-leaning (>50) composite scores
    dampen_above_score: float = 50.0          # only bullish-leaning scores get dampened
    volatility_lookback_days: int = 20
    volatility_normalize_lo: float = 0.30     # annualized realized vol treated as "calm"
    volatility_normalize_hi: float = 1.20     # annualized realized vol treated as "extreme"


@dataclass
class BucketThresholds:
    """One bucket's admission criteria — a result must meet ALL of these
    to qualify. Buckets are evaluated most-selective-first (Super Strong,
    then Strong, then Building), so a result lands in the first bucket
    it qualifies for; anything meeting none of them falls to High Risk /
    Low Conviction, the catch-all."""
    min_score: float
    min_confidence: float
    allowed_risk_tiers: List[str]
    min_data_completeness: float   # fraction of pillars with available=True, 0.0-1.0


@dataclass
class SmartViewConfig:
    """
    Bucket thresholds for the dashboard's Smart View. Deliberately
    aligned with the existing SignalBands/ConfidenceBands defaults
    (strong_buy=80, buy=65, neutral=45; confidence high=75, medium=50)
    rather than inventing a disconnected second grading system — Smart
    View is a presentation grouping on top of the same underlying
    scores, not a competing scoring model.

    Super Strong is deliberately narrow: high score AND high confidence
    AND acceptable risk tier AND good data completeness, all four
    required — a coin can't buy its way into Super Strong with a great
    score alone if the underlying data was thin or the risk tier is
    high_risk. This is what "very selective" means concretely.
    """
    enabled: bool = True
    super_strong: BucketThresholds = field(default_factory=lambda: BucketThresholds(
        min_score=80.0, min_confidence=75.0, allowed_risk_tiers=["core", "small_cap"], min_data_completeness=0.75,
    ))
    strong: BucketThresholds = field(default_factory=lambda: BucketThresholds(
        min_score=65.0, min_confidence=50.0, allowed_risk_tiers=["core", "small_cap", "high_risk"], min_data_completeness=0.5,
    ))
    building: BucketThresholds = field(default_factory=lambda: BucketThresholds(
        min_score=45.0, min_confidence=0.0, allowed_risk_tiers=["core", "small_cap", "high_risk"], min_data_completeness=0.0,
    ))
    # Anything not meeting even "building"'s thresholds -> High Risk / Low Conviction (no config needed, it's the catch-all)


@dataclass
class ScannerConfig:
    weights: Weights = field(default_factory=Weights)
    filters: QualityFilters = field(default_factory=QualityFilters)
    timeframe_config: TimeframeConfig = field(default_factory=TimeframeConfig)
    signal_bands: SignalBands = field(default_factory=SignalBands)
    confidence_bands: ConfidenceBands = field(default_factory=ConfidenceBands)
    regime_config: RegimeConfig = field(default_factory=RegimeConfig)
    sector_map: Dict[str, List[str]] = field(default_factory=lambda: dict(DEFAULT_SECTOR_MAP))
    smart_view: SmartViewConfig = field(default_factory=SmartViewConfig)

    # Exchange to use for OHLCV / OI / funding / long-short ratio (ccxt id)
    primary_exchange: str = "bybit"
    # Strict priority order for price/OHLCV/OI/funding — tried in order,
    # first success wins, no averaging. Hyperliquid first (no US
    # restriction, matches what AutoBot actually trades), Bybit last and
    # optional (per the explicit "never let Bybit being blocked break the
    # scan" requirement). Configurable via settings.yaml's
    # market_data_priority — see Settings.to_scanner_config().
    market_data_priority: list = None  # set to the real default in __post_init__ below

    def __post_init__(self):
        if self.market_data_priority is None:
            self.market_data_priority = ["hyperliquid", "coingecko", "coinbase", "kraken", "bybit"]
    quote_currency: str = "USDT"

    # LunarCrush (social pillar) — see data_sources/social.py
    lunarcrush_api_key: str | None = None

    def sector_of(self, base: str) -> Optional[str]:
        base = base.upper()
        for sector, coins in self.sector_map.items():
            if base in coins:
                return sector
        return None

    def sector_peers(self, base: str) -> List[str]:
        sector = self.sector_of(base)
        if sector is None:
            return []
        return [c for c in self.sector_map[sector] if c != base.upper()]

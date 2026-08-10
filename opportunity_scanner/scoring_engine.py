"""
Scoring Engine — metrics-in, score-out.

This is a deliberately decoupled layer: it takes already-computed scalar
metrics (CoinMetrics) rather than raw OHLCV/time-series data, and turns
them into an Opportunity Score + grade + confidence + explainable thesis.

Why this is useful alongside `scanner.py`/`factors/*.py`: those modules
own the expensive part (pulling OHLCV, computing indicators from time
series). This engine owns the cheap, pure part — weighting, blending,
grading, explaining — and can be tested, reused, or fed by a completely
different upstream pipeline without touching an exchange at all. Every
`calculate_*` method degrades gracefully: missing fields don't crash it
and don't get treated as "bad," they just don't contribute to that
sub-score, and weights renormalize over what IS available.

Formulas match opportunity_scanner/ARCHITECTURE.md exactly. See that file
for the full derivation of every constant here.
"""

from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional, Dict, List, Tuple
from enum import Enum
import numpy as np


class Grade(str, Enum):
    STRONG_OPPORTUNITY = "Strong Opportunity"
    OPPORTUNITY = "Opportunity"
    WATCHLIST = "Watchlist"
    IGNORE = "Ignore"


class PillarScores(BaseModel):
    strength: float = Field(..., ge=0, le=100)
    open_interest: float = Field(..., ge=0, le=100)
    trend_momentum: float = Field(..., ge=0, le=100)
    social_virality: float = Field(..., ge=0, le=100)


class HardFilters(BaseModel):
    """Per ARCHITECTURE.md Section 0. A coin failing these is excluded, not scored low."""
    min_volume_24h_usd: float = 5_000_000.0
    min_market_cap_usd: float = 10_000_000.0
    min_exchange_listings: int = 1
    max_bid_ask_spread_pct: float = 1.5


class CoinMetrics(BaseModel):
    symbol: str
    price: float
    volume_24h: float
    market_cap: Optional[float] = None
    exchange_listings: int = 1
    bid_ask_spread_pct: Optional[float] = None

    # --- Strength ---
    rs_vs_btc_1h: Optional[float] = None
    rs_vs_btc_4h: Optional[float] = None
    rs_vs_btc_24h: Optional[float] = None
    rs_vs_sector_1h: Optional[float] = None
    rs_vs_sector_4h: Optional[float] = None
    rs_vs_sector_24h: Optional[float] = None
    volume_surge: Optional[float] = None            # % vs trailing average, e.g. 0.5 = +50%
    volume_profile_pct: Optional[float] = None        # 0-1, share of recent volume near current price
    obv_slope: Optional[float] = None                  # normalized OBV slope, roughly -1..1
    higher_high: Optional[bool] = None
    higher_low: Optional[bool] = None
    break_of_structure_bullish: Optional[bool] = None
    break_of_structure_bearish: Optional[bool] = None

    # --- Open Interest ---
    oi_change_1h: Optional[float] = None
    oi_change_4h: Optional[float] = None
    oi_change_24h: Optional[float] = None
    price_change_1h: Optional[float] = None
    price_change_4h: Optional[float] = None
    price_change_24h: Optional[float] = None
    funding_rate: Optional[float] = None
    funding_rate_prev: Optional[float] = None
    long_short_ratio: Optional[float] = None            # global account ratio
    top_trader_long_short_ratio: Optional[float] = None  # where a venue exposes it
    open_interest_usd: Optional[float] = None

    # --- Trend & Momentum ---
    trend_alignment_score: Optional[float] = None    # 0-100, precomputed EMA-stack/SuperTrend/ADX composite
    adx: Optional[float] = None                        # fallback if trend_alignment_score absent
    plus_di: Optional[float] = None
    minus_di: Optional[float] = None
    rsi_1h: Optional[float] = None
    rsi_4h: Optional[float] = None
    rsi_1d: Optional[float] = None
    macd_hist: Optional[float] = None
    macd_hist_prev: Optional[float] = None
    roc: Optional[float] = None
    stochastic_k: Optional[float] = None
    bullish_divergence: Optional[bool] = None
    bearish_divergence: Optional[bool] = None

    # --- Social ---
    social_volume_change: Optional[float] = None      # % vs baseline (blended 7d/30d upstream), e.g. 1.8 = +180%
    sentiment: Optional[float] = None                  # 0-100
    sentiment_shift: Optional[float] = None             # percentage-point change
    galaxy_score: Optional[float] = None
    galaxy_score_prior: Optional[float] = None
    alt_rank: Optional[int] = None
    engagement_score: Optional[float] = None              # 0-100, precomputed weighted engagement
    kol_score: Optional[float] = None                       # 0-100 if tracked, else no boost applied


class FinalResult(BaseModel):
    symbol: str
    opportunity_score: float = Field(..., ge=0, le=100)
    confidence: float = Field(..., ge=0, le=100)
    grade: Grade
    pillar_scores: PillarScores
    thesis: List[str] = []
    flags: List[str] = []
    metrics: CoinMetrics
    regime_note: Optional[str] = None


# ---------------------------------------------------------------- helpers

def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return float(max(lo, min(hi, v)))


def _normalize(v: float, lo: float, hi: float) -> float:
    if hi == lo:
        return 50.0
    return _clamp((v - lo) / (hi - lo) * 100.0)


def _blend(parts: List[Tuple[Optional[float], float]]) -> Optional[float]:
    """Weighted average over the parts that are actually present (not None),
    renormalizing weight over what's available. Returns None if nothing's available."""
    available = [(v, w) for v, w in parts if v is not None]
    if not available:
        return None
    total_w = sum(w for _, w in available)
    if total_w <= 0:
        return None
    return sum(v * w for v, w in available) / total_w


def _healthy_band(value: Optional[float], lo: float, hi: float, exhaustion_hi: float, exhaustion_lo: float) -> Optional[float]:
    if value is None:
        return None
    if lo <= value <= hi:
        return 50 + _normalize(value, lo, hi) * 0.5
    if value > hi:
        overshoot = value - hi
        span = max(exhaustion_hi - hi, 1e-6)
        return _clamp(100 - _normalize(overshoot, 0, span) * 0.7)
    overshoot = lo - value
    span = max(lo - exhaustion_lo, 1e-6)
    return _clamp(_normalize(value, exhaustion_lo, lo) * 0.7)


class ScoringEngine:
    def __init__(self, weights: Dict[str, float] = None, hard_filters: HardFilters = None):
        self.weights = weights or {
            "strength": 0.22,
            "open_interest": 0.28,
            "trend_momentum": 0.25,
            "social_virality": 0.25,
        }
        total = sum(self.weights.values())
        self.weights = {k: v / total for k, v in self.weights.items()}
        self.filters = hard_filters or HardFilters()

    # ---------------------------------------------------------- Pillar 1

    def calculate_strength(self, metrics: CoinMetrics) -> float:
        def rs_component(btc: Optional[float], sector: Optional[float]) -> Optional[float]:
            if btc is None:
                return None
            if sector is not None:
                return btc * 0.65 + sector * 0.35
            return btc

        rs_1h = rs_component(metrics.rs_vs_btc_1h, metrics.rs_vs_sector_1h)
        rs_4h = rs_component(metrics.rs_vs_btc_4h, metrics.rs_vs_sector_4h)
        rs_24h = rs_component(metrics.rs_vs_btc_24h, metrics.rs_vs_sector_24h)
        rs_blended = _blend([(rs_1h, 0.20), (rs_4h, 0.35), (rs_24h, 0.45)])
        rs_score = _normalize(rs_blended, -20, 20) if rs_blended is not None else 50.0

        vol_parts: List[Tuple[Optional[float], float]] = []
        if metrics.volume_surge is not None:
            vol_parts.append((_normalize(metrics.volume_surge, -0.3, 2.0), 0.4))
        if metrics.volume_profile_pct is not None:
            vol_parts.append((_normalize(metrics.volume_profile_pct, 0, 1), 0.3))
        if metrics.obv_slope is not None:
            vol_parts.append((_normalize(metrics.obv_slope, -1, 1), 0.3))
        vol_score = _blend(vol_parts)
        vol_score = vol_score if vol_score is not None else 50.0

        structure_score = 50.0
        if metrics.higher_high is True and metrics.higher_low is True:
            structure_score = 85.0
        elif metrics.higher_high is False and metrics.higher_low is False:
            structure_score = 15.0
        elif metrics.break_of_structure_bullish:
            structure_score = 70.0
        elif metrics.break_of_structure_bearish:
            structure_score = 30.0

        liq_parts: List[Tuple[Optional[float], float]] = []
        if metrics.market_cap and metrics.market_cap > 0:
            liq_parts.append((_normalize(metrics.volume_24h / metrics.market_cap, 0, 0.35), 0.6))
        if metrics.bid_ask_spread_pct is not None and metrics.bid_ask_spread_pct > 0:
            liq_parts.append((_normalize(1 / max(metrics.bid_ask_spread_pct, 0.01), 0, 100), 0.4))
        liq_score = _blend(liq_parts)
        liq_score = liq_score if liq_score is not None else 50.0

        composite = rs_score * 0.35 + vol_score * 0.25 + structure_score * 0.25 + liq_score * 0.15
        return round(_clamp(composite), 2)

    # ---------------------------------------------------------- Pillar 2

    def calculate_open_interest(self, metrics: CoinMetrics) -> float:
        oi_blended = _blend([
            (metrics.oi_change_1h, 0.20), (metrics.oi_change_4h, 0.35), (metrics.oi_change_24h, 0.45),
        ])
        oi_change_score = _normalize(oi_blended, -25, 25) if oi_blended is not None else None

        price_chg = metrics.price_change_24h if metrics.price_change_24h is not None else (
            metrics.price_change_4h if metrics.price_change_4h is not None else metrics.price_change_1h
        )
        oi_chg_for_div = metrics.oi_change_24h if metrics.oi_change_24h is not None else (
            metrics.oi_change_4h if metrics.oi_change_4h is not None else metrics.oi_change_1h
        )
        divergence_score = None
        if price_chg is not None and oi_chg_for_div is not None:
            aligned = (price_chg >= 0) == (oi_chg_for_div >= 0)
            magnitude = _normalize(abs(oi_chg_for_div), 0, 20)
            divergence_score = _clamp(60 + magnitude * 0.4) if aligned else _clamp(40 - magnitude * 0.4)

        funding_score = None
        if metrics.funding_rate is not None:
            abs_bps = abs(metrics.funding_rate) * 10000
            extreme = _clamp(100 - _normalize(abs_bps, 0, 50))
            trend_component = 50.0
            if metrics.funding_rate_prev is not None:
                moving_toward_zero = abs(metrics.funding_rate) < abs(metrics.funding_rate_prev)
                trend_component = 70.0 if moving_toward_zero else 30.0
            funding_score = extreme * 0.6 + trend_component * 0.4

        ls_parts: List[Tuple[Optional[float], float]] = []
        if metrics.top_trader_long_short_ratio is not None:
            ls_parts.append((_clamp(100 - abs(metrics.top_trader_long_short_ratio - 1.0) * 50), 0.6))
        if metrics.long_short_ratio is not None:
            w = 0.4 if metrics.top_trader_long_short_ratio is not None else 1.0
            ls_parts.append((_clamp(100 - abs(metrics.long_short_ratio - 1.0) * 50), w))
        ls_score = _blend(ls_parts)

        dampener = 0.0
        if metrics.open_interest_usd and metrics.market_cap and metrics.market_cap > 0:
            ratio_score = _normalize(metrics.open_interest_usd / metrics.market_cap, 0, 0.5)
            dampener = max(0.0, ratio_score - 70) * 0.10

        parts: List[Tuple[Optional[float], float]] = []
        if oi_change_score is not None:
            parts.append((oi_change_score, 0.25))
        if divergence_score is not None:
            parts.append((divergence_score, 0.35))
        if funding_score is not None:
            parts.append((funding_score, 0.20))
        if ls_score is not None:
            parts.append((ls_score, 0.15))

        composite = _blend(parts)
        if composite is None:
            return 50.0
        return round(_clamp(composite - dampener), 2)

    # ---------------------------------------------------------- Pillar 3

    def calculate_trend_momentum(self, metrics: CoinMetrics) -> float:
        trend_score = metrics.trend_alignment_score
        if trend_score is None and metrics.adx is not None:
            adx_strength = _normalize(metrics.adx, 15, 40)
            if metrics.plus_di is not None and metrics.minus_di is not None:
                di_direction = 100.0 if metrics.plus_di > metrics.minus_di else 0.0
            else:
                di_direction = 50.0
            trend_score = adx_strength * 0.5 + di_direction * 0.5

        rsi_candidates = [r for r in [metrics.rsi_1h, metrics.rsi_4h, metrics.rsi_1d] if r is not None]
        rsi_score = _healthy_band(sum(rsi_candidates) / len(rsi_candidates), 50, 75, 90, 10) if rsi_candidates else None

        macd_score = None
        if metrics.macd_hist is not None:
            if metrics.macd_hist_prev is not None:
                if metrics.macd_hist > 0 and metrics.macd_hist > metrics.macd_hist_prev:
                    macd_score = 80.0
                elif metrics.macd_hist > 0:
                    macd_score = 60.0
                elif metrics.macd_hist <= 0 and metrics.macd_hist < metrics.macd_hist_prev:
                    macd_score = 20.0
                else:
                    macd_score = 40.0
            else:
                macd_score = 70.0 if metrics.macd_hist > 0 else 30.0

        roc_score = _normalize(metrics.roc, -15, 15) if metrics.roc is not None else None
        stoch_score = _healthy_band(metrics.stochastic_k, 40, 80, 95, 5)

        quality_parts: List[Tuple[Optional[float], float]] = []
        if rsi_score is not None:
            quality_parts.append((rsi_score, 0.3))
        if macd_score is not None:
            quality_parts.append((macd_score, 0.3))
        if roc_score is not None:
            quality_parts.append((roc_score, 0.2))
        if stoch_score is not None:
            quality_parts.append((stoch_score, 0.2))
        momentum_quality = _blend(quality_parts)

        if trend_score is None and momentum_quality is None:
            return 50.0
        if trend_score is None:
            base = momentum_quality
        elif momentum_quality is None:
            base = trend_score
        else:
            base = trend_score * 0.6 + momentum_quality * 0.4

        divergence_score = 50.0
        if metrics.bearish_divergence:
            divergence_score = 25.0
        elif metrics.bullish_divergence:
            divergence_score = 75.0

        composite = base * 0.85 + divergence_score * 0.15
        return round(_clamp(composite), 2)

    # ---------------------------------------------------------- Pillar 4

    def calculate_social_virality(self, metrics: CoinMetrics) -> float:
        velocity_score = _normalize(metrics.social_volume_change, -0.5, 2.0) if metrics.social_volume_change is not None else None

        mindshare_parts: List[Tuple[Optional[float], float]] = []
        if metrics.galaxy_score is not None:
            w = 0.5 if metrics.alt_rank is not None else 1.0
            mindshare_parts.append((_clamp(metrics.galaxy_score), w))
        if metrics.alt_rank is not None:
            w = 0.5 if metrics.galaxy_score is not None else 1.0
            mindshare_parts.append((_normalize(300 - metrics.alt_rank, 0, 300), w))
        mindshare_level = _blend(mindshare_parts)

        mindshare_score = mindshare_level
        if mindshare_level is not None and metrics.galaxy_score is not None and metrics.galaxy_score_prior is not None:
            growth = _normalize(metrics.galaxy_score - metrics.galaxy_score_prior, -20, 20)
            mindshare_score = mindshare_level * 0.5 + growth * 0.5

        sentiment_score = None
        if metrics.sentiment is not None:
            level = _normalize(metrics.sentiment, 30, 80)
            if metrics.sentiment_shift is not None:
                shift = _normalize(metrics.sentiment_shift, -20, 20)
                sentiment_score = level * 0.4 + shift * 0.6
            else:
                sentiment_score = level

        engagement_score = metrics.engagement_score

        parts: List[Tuple[Optional[float], float]] = []
        if velocity_score is not None:
            parts.append((velocity_score, 0.35))
        if mindshare_score is not None:
            parts.append((mindshare_score, 0.30))
        if sentiment_score is not None:
            parts.append((sentiment_score, 0.20))
        if engagement_score is not None:
            parts.append((engagement_score, 0.15))

        composite = _blend(parts)
        if composite is None:
            return 50.0

        kol_boost = 0.0
        if metrics.kol_score is not None:
            kol_boost = min(_normalize(metrics.kol_score, 0, 100) * 0.15, 15.0)

        return round(_clamp(composite + kol_boost), 2)

    # ---------------------------------------------------------- confidence

    _STRENGTH_FIELDS = [
        "rs_vs_btc_1h", "rs_vs_btc_4h", "rs_vs_btc_24h", "volume_surge",
        "volume_profile_pct", "obv_slope", "higher_high", "higher_low", "bid_ask_spread_pct",
    ]
    _OI_FIELDS = [
        "oi_change_1h", "oi_change_4h", "oi_change_24h", "price_change_24h",
        "funding_rate", "long_short_ratio", "open_interest_usd",
    ]
    _MOMENTUM_FIELDS = ["trend_alignment_score", "adx", "rsi_1h", "macd_hist", "roc", "stochastic_k"]
    _SOCIAL_FIELDS = ["social_volume_change", "sentiment", "galaxy_score", "alt_rank", "engagement_score"]

    def _pillar_completeness(self, metrics: CoinMetrics) -> Dict[str, float]:
        def frac(fields: List[str]) -> float:
            present = sum(1 for f in fields if getattr(metrics, f, None) is not None)
            return present / len(fields) if fields else 0.0

        return {
            "strength": frac(self._STRENGTH_FIELDS),
            "open_interest": frac(self._OI_FIELDS),
            "trend_momentum": frac(self._MOMENTUM_FIELDS),
            "social_virality": frac(self._SOCIAL_FIELDS),
        }

    def calculate_confidence(self, pillar_scores: PillarScores, metrics: CoinMetrics) -> float:
        """
        Two components, per ARCHITECTURE.md 5.1:
          completeness — weighted by pillar weight, how much of the intended
                         input data is actually present for this coin
          agreement    — do the four pillar scores broadly agree, or
                         contradict each other?
        """
        scores = [pillar_scores.strength, pillar_scores.open_interest,
                  pillar_scores.trend_momentum, pillar_scores.social_virality]
        agreement = 1.0 - _normalize(float(np.std(scores)), 0, 35) / 100.0

        completeness_by_pillar = self._pillar_completeness(metrics)
        completeness = sum(completeness_by_pillar[k] * self.weights[k] for k in self.weights)

        confidence = (completeness * 0.5 + agreement * 0.5) * 100.0
        return round(_clamp(confidence), 2)

    # ---------------------------------------------------------------- grade

    def get_grade(self, score: float) -> Grade:
        if score >= 85:
            return Grade.STRONG_OPPORTUNITY
        elif score >= 70:
            return Grade.OPPORTUNITY
        elif score >= 55:
            return Grade.WATCHLIST
        return Grade.IGNORE

    # ------------------------------------------------------------ thesis/flags

    def generate_thesis(self, result: FinalResult) -> List[str]:
        m = result.metrics
        contributions = sorted(
            [
                ("strength", result.pillar_scores.strength),
                ("open_interest", result.pillar_scores.open_interest),
                ("trend_momentum", result.pillar_scores.trend_momentum),
                ("social_virality", result.pillar_scores.social_virality),
            ],
            key=lambda kv: self.weights[kv[0]] * kv[1],
            reverse=True,
        )

        thesis = []
        for name, score in contributions:
            if name == "strength" and m.rs_vs_btc_24h is not None:
                thesis.append(f"Relative strength vs BTC (24h): {m.rs_vs_btc_24h:+.1f}pp (strength pillar {score:.0f}/100)")
            elif name == "open_interest" and m.oi_change_24h is not None:
                direction = "confirming" if (m.price_change_24h or 0) * m.oi_change_24h >= 0 else "diverging from"
                thesis.append(f"OI {m.oi_change_24h:+.1f}% (24h), {direction} price action (OI pillar {score:.0f}/100)")
            elif name == "trend_momentum" and m.trend_alignment_score is not None:
                thesis.append(f"Trend alignment {m.trend_alignment_score:.0f}/100 (momentum pillar {score:.0f}/100)")
            elif name == "social_virality" and m.social_volume_change is not None:
                thesis.append(f"Social mentions {m.social_volume_change*100:+.0f}% vs baseline (social pillar {score:.0f}/100)")
            else:
                thesis.append(f"{name.replace('_', ' ').title()} pillar scored {score:.0f}/100")

        return thesis[:5]

    def generate_flags(self, metrics: CoinMetrics) -> List[str]:
        flags: List[str] = []

        if metrics.price_change_24h is not None and metrics.oi_change_24h is not None:
            aligned = (metrics.price_change_24h >= 0) == (metrics.oi_change_24h >= 0)
            if aligned and metrics.oi_change_24h > 5:
                flags.append("OI Confirms Move" if metrics.price_change_24h >= 0 else "OI Confirms Downtrend")
            elif not aligned and abs(metrics.oi_change_24h) > 5:
                flags.append("Short Covering, Not Fresh Longs" if metrics.price_change_24h >= 0 else "Long Liquidation Pressure")

        if metrics.funding_rate is not None:
            bps = metrics.funding_rate * 10000
            if bps > 15:
                flags.append("Crowded Longs (elevated funding)")
            elif bps < -15:
                flags.append("Crowded Shorts (elevated negative funding)")

        if metrics.bullish_divergence:
            flags.append("Bullish Momentum Divergence")
        if metrics.bearish_divergence:
            flags.append("Bearish Momentum Divergence")

        if metrics.break_of_structure_bullish:
            flags.append("Break of Structure (Bullish)")
        if metrics.break_of_structure_bearish:
            flags.append("Break of Structure (Bearish)")

        if metrics.social_volume_change is not None and metrics.social_volume_change > 1.0:
            flags.append("Mention Spike (>100% vs baseline)")

        if metrics.top_trader_long_short_ratio is not None and abs(metrics.top_trader_long_short_ratio - 1.0) > 0.5:
            flags.append("Lopsided Top-Trader Positioning")

        return flags

    # ---------------------------------------------------------------- filters

    def passes_hard_filters(self, metrics: CoinMetrics) -> Tuple[bool, List[str]]:
        reasons = []
        ok = True
        if metrics.volume_24h < self.filters.min_volume_24h_usd:
            ok = False
            reasons.append(f"24h volume ${metrics.volume_24h:,.0f} below minimum ${self.filters.min_volume_24h_usd:,.0f}")
        if metrics.market_cap is not None and metrics.market_cap < self.filters.min_market_cap_usd:
            ok = False
            reasons.append(f"Market cap ${metrics.market_cap:,.0f} below minimum ${self.filters.min_market_cap_usd:,.0f}")
        if metrics.exchange_listings < self.filters.min_exchange_listings:
            ok = False
            reasons.append(f"Listed on {metrics.exchange_listings} exchange(s), below minimum {self.filters.min_exchange_listings}")
        if metrics.bid_ask_spread_pct is not None and metrics.bid_ask_spread_pct > self.filters.max_bid_ask_spread_pct:
            ok = False
            reasons.append(f"Spread {metrics.bid_ask_spread_pct:.2f}% wider than maximum {self.filters.max_bid_ask_spread_pct:.2f}%")
        return ok, reasons

    # ---------------------------------------------------------------- score

    def score(
        self,
        metrics: CoinMetrics,
        regime_label: Optional[str] = None,
        regime_score: Optional[float] = None,
        is_btc: bool = False,
        regime_dampener_points: float = 12.0,
        dampen_above_score: float = 50.0,
    ) -> FinalResult:
        strength = self.calculate_strength(metrics)
        oi = self.calculate_open_interest(metrics)
        trend = self.calculate_trend_momentum(metrics)
        social = self.calculate_social_virality(metrics)

        pillar_scores = PillarScores(
            strength=strength, open_interest=oi, trend_momentum=trend, social_virality=social,
        )

        opportunity_score = (
            strength * self.weights["strength"]
            + oi * self.weights["open_interest"]
            + trend * self.weights["trend_momentum"]
            + social * self.weights["social_virality"]
        )

        confidence = self.calculate_confidence(pillar_scores, metrics)

        regime_note = None
        if regime_label == "Risk-Off" and not is_btc and opportunity_score > dampen_above_score:
            pre = opportunity_score
            opportunity_score = _clamp(opportunity_score - regime_dampener_points)
            regime_note = (
                f"Dampened {regime_dampener_points:.0f}pts ({pre:.1f}\u2192{opportunity_score:.1f}): "
                f"BTC regime is Risk-Off"
                + (f" (regime score {regime_score:.0f}/100)" if regime_score is not None else "")
                + " — bullish signals need extra scrutiny"
            )

        result = FinalResult(
            symbol=metrics.symbol,
            opportunity_score=round(opportunity_score, 2),
            confidence=confidence,
            grade=self.get_grade(opportunity_score),
            pillar_scores=pillar_scores,
            metrics=metrics,
            flags=self.generate_flags(metrics),
            regime_note=regime_note,
        )
        result.thesis = self.generate_thesis(result)
        if regime_note:
            result.thesis.insert(0, regime_note)

        return result

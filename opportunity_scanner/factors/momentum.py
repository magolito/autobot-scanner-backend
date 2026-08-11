"""
Pillar 3: Trend & Momentum (v2)
---------------------------------
Multi-timeframe (15m/1h/4h/1d) trend + momentum quality.

Per timeframe:
  - EMA 9/21/50/200 stack alignment
  - SuperTrend (manual ATR-based implementation — not in the `ta` library)
  - ADX / +DI / -DI (trend strength + direction confirmation)
  - RSI, MACD histogram slope, Rate of Change, Stochastic %K (momentum quality)

Then blended across timeframes by config weight, plus a momentum
divergence check (price vs RSI/MACD) applied once at the end.
"""

from __future__ import annotations
from typing import Dict, Optional
import pandas as pd
import ta

from ..config import TimeframeConfig
from ..models import MarketSnapshot, FactorResult


def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def _normalize(v: float, lo: float, hi: float) -> float:
    if hi == lo:
        return 50.0
    return _clamp((v - lo) / (hi - lo) * 100.0)


def _healthy_band_score(value: float, lo: float, hi: float, exhaustion_hi: float, exhaustion_lo: float) -> float:
    """Reward a value inside [lo, hi] as 'healthy momentum'; fade toward 0
    as it pushes past exhaustion thresholds in either direction."""
    if pd.isna(value):
        return 50.0
    if lo <= value <= hi:
        return 50 + _normalize(value, lo, hi) * 0.5
    if value > hi:
        overshoot = value - hi
        span = max(exhaustion_hi - hi, 1e-6)
        return _clamp(100 - _normalize(overshoot, 0, span) * 0.7)
    overshoot = lo - value
    span = max(lo - exhaustion_lo, 1e-6)
    return _clamp(_normalize(value, exhaustion_lo, lo) * 0.7)


# ------------------------------------------------------------- SuperTrend

def _supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.Series:
    """
    Manual SuperTrend implementation (not shipped in the `ta` library).
    Returns a boolean Series: True where price is in an uptrend (close
    above the SuperTrend line), False where in a downtrend.
    """
    atr = ta.volatility.average_true_range(df["high"], df["low"], df["close"], window=period)
    hl2 = (df["high"] + df["low"]) / 2
    upper_basic = hl2 + multiplier * atr
    lower_basic = hl2 - multiplier * atr

    upper = upper_basic.copy()
    lower = lower_basic.copy()
    trend_up = pd.Series(True, index=df.index)

    for i in range(1, len(df)):
        if upper_basic.iloc[i] < upper.iloc[i - 1] or df["close"].iloc[i - 1] > upper.iloc[i - 1]:
            upper.iloc[i] = upper_basic.iloc[i]
        else:
            upper.iloc[i] = upper.iloc[i - 1]
        if lower_basic.iloc[i] > lower.iloc[i - 1] or df["close"].iloc[i - 1] < lower.iloc[i - 1]:
            lower.iloc[i] = lower_basic.iloc[i]
        else:
            lower.iloc[i] = lower.iloc[i - 1]
        if trend_up.iloc[i - 1] and df["close"].iloc[i] < lower.iloc[i]:
            trend_up.iloc[i] = False
        elif (not trend_up.iloc[i - 1]) and df["close"].iloc[i] > upper.iloc[i]:
            trend_up.iloc[i] = True
        else:
            trend_up.iloc[i] = trend_up.iloc[i - 1]

    return trend_up


# ------------------------------------------------------------- divergence

def _detect_divergence(price: pd.Series, indicator: pd.Series, window: int = 14) -> str:
    if len(price) < window + 1 or len(indicator) < window + 1:
        return "none"
    recent_price = price.iloc[-window:]
    recent_ind = indicator.iloc[-window:]

    price_new_high = recent_price.iloc[-1] >= recent_price.iloc[:-1].max()
    ind_lower_high = recent_ind.iloc[-1] < recent_ind.iloc[:-1].max()
    if price_new_high and ind_lower_high:
        return "bearish"

    price_new_low = recent_price.iloc[-1] <= recent_price.iloc[:-1].min()
    ind_higher_low = recent_ind.iloc[-1] > recent_ind.iloc[:-1].min()
    if price_new_low and ind_higher_low:
        return "bullish"

    return "none"


# ------------------------------------------------------- per-timeframe

def _single_timeframe_score(df: pd.DataFrame) -> Optional[tuple[float, str, dict]]:
    if df is None or len(df) < 210:  # need runway for EMA200
        return None

    close = df["close"]

    ema9 = ta.trend.ema_indicator(close, window=9)
    ema21 = ta.trend.ema_indicator(close, window=21)
    ema50 = ta.trend.ema_indicator(close, window=50)
    ema200 = ta.trend.ema_indicator(close, window=200)

    rsi = ta.momentum.rsi(close, window=14)
    macd = ta.trend.MACD(close)
    macd_hist = macd.macd_diff()
    roc = ta.momentum.roc(close, window=14)
    stoch = ta.momentum.StochasticOscillator(df["high"], df["low"], close, window=14, smooth_window=3)
    stoch_k = stoch.stoch()

    adx_ind = ta.trend.ADXIndicator(df["high"], df["low"], close, window=14)
    adx = adx_ind.adx()
    plus_di = adx_ind.adx_pos()
    minus_di = adx_ind.adx_neg()

    trend_up_series = _supertrend(df)

    e9, e21, e50, e200 = ema9.iloc[-1], ema21.iloc[-1], ema50.iloc[-1], ema200.iloc[-1]
    pairs = [(e9, e21), (e9, e50), (e9, e200), (e21, e50), (e21, e200), (e50, e200)]
    bullish_pairs = sum(1 for a, b in pairs if a > b)
    if bullish_pairs == 6:
        ema_score, ema_note = 100.0, "full bullish EMA9/21/50/200 stack"
    elif bullish_pairs == 0:
        ema_score, ema_note = 0.0, "full bearish EMA9/21/50/200 stack"
    else:
        ema_score = _normalize(bullish_pairs, 0, 6)
        ema_note = f"partial EMA stack alignment ({bullish_pairs}/6 pairs bullish)"

    st_up = bool(trend_up_series.iloc[-1])
    st_score = 100.0 if st_up else 0.0
    st_note = "above SuperTrend (uptrend)" if st_up else "below SuperTrend (downtrend)"

    last_adx = adx.iloc[-1]
    last_pdi, last_mdi = plus_di.iloc[-1], minus_di.iloc[-1]
    adx_strength = _normalize(last_adx, 15, 40) if not pd.isna(last_adx) else 50.0
    di_direction = 100.0 if (not pd.isna(last_pdi) and not pd.isna(last_mdi) and last_pdi > last_mdi) else 0.0
    adx_di_score = adx_strength * 0.5 + di_direction * 0.5
    adx_note = f"ADX {last_adx:.0f} ({'trending' if last_adx > 25 else 'weak/ranging'}), " + (
        "+DI>-DI (bullish)" if di_direction == 100 else "-DI>+DI (bearish)"
    )

    tf_trend_score = ema_score * 0.4 + st_score * 0.3 + adx_di_score * 0.3

    last_rsi = rsi.iloc[-1]
    rsi_score = _healthy_band_score(last_rsi, 50, 75, exhaustion_hi=90, exhaustion_lo=10)

    last_hist, prev_hist = macd_hist.iloc[-1], (macd_hist.iloc[-2] if len(macd_hist) > 1 else macd_hist.iloc[-1])
    if not pd.isna(last_hist) and not pd.isna(prev_hist):
        if last_hist > 0 and last_hist > prev_hist:
            macd_score = 80.0
        elif last_hist > 0:
            macd_score = 60.0
        elif last_hist <= 0 and last_hist < prev_hist:
            macd_score = 20.0
        else:
            macd_score = 40.0
    else:
        macd_score = 50.0

    last_roc = roc.iloc[-1]
    roc_score = _normalize(last_roc, -15, 15) if not pd.isna(last_roc) else 50.0

    last_stoch = stoch_k.iloc[-1]
    stoch_score = _healthy_band_score(last_stoch, 40, 80, exhaustion_hi=95, exhaustion_lo=5) if not pd.isna(last_stoch) else 50.0

    momentum_quality = rsi_score * 0.3 + macd_score * 0.3 + roc_score * 0.2 + stoch_score * 0.2

    tf_score = tf_trend_score * 0.6 + momentum_quality * 0.4

    note = f"{ema_note}; {st_note}; {adx_note}; RSI {last_rsi:.0f}" if not pd.isna(last_rsi) else f"{ema_note}; {st_note}"

    raw = {
        "ema_score": ema_score, "supertrend_score": st_score, "adx_di_score": adx_di_score,
        "rsi_score": rsi_score, "macd_score": macd_score, "roc_score": roc_score, "stoch_score": stoch_score,
        "rsi_series": rsi, "macd_hist_series": macd_hist, "close_series": close,
    }
    return _clamp(tf_score), note, raw


def _classify_direction(score: float, bullish_threshold: float = 55, bearish_threshold: float = 45) -> str:
    if score > bullish_threshold:
        return "bullish"
    if score < bearish_threshold:
        return "bearish"
    return "neutral"


def _compute_alignment(per_tf_scores: Dict[str, float], timeframe_weights: Dict[str, float]) -> dict:
    """
    The actual "is this really strong or just noise on one timeframe"
    signal — directly implementing the specification: strength on 15m
    alone means little, but 15m AND 1h AND 4h all agreeing together is
    real conviction. Graded and weighted, not binary — a coin aligned
    across 3 of 4 timeframes gets more credit than one aligned on 2,
    and agreement on longer timeframes (1d/4h) counts for more than 15m
    alone, reusing the same timeframe_weights already configured for
    the composite blend (1d=0.35, 4h=0.30, 1h=0.25, 15m=0.10 by
    default) — so this naturally matches "if it's ALSO showing strength
    on the 4h, that gives more conviction," not an arbitrary new scale.

    Also directional both ways deliberately — a strongly bearish-aligned
    coin is exactly as real a signal as a bullish one (a genuine short
    setup, not just an absence of a long setup), matching the explicit
    ask to flag "good for short" too, not only "good for long."
    """
    directions = {tf: _classify_direction(score) for tf, score in per_tf_scores.items()}
    bullish_weight = sum(timeframe_weights.get(tf, 0) for tf, d in directions.items() if d == "bullish")
    bearish_weight = sum(timeframe_weights.get(tf, 0) for tf, d in directions.items() if d == "bearish")
    total_weight = sum(timeframe_weights.get(tf, 0) for tf in per_tf_scores)

    if total_weight <= 0 or (bullish_weight == 0 and bearish_weight == 0):
        return {"alignment_score": 0.0, "dominant_direction": "mixed", "aligned_timeframes": [], "directions": directions}

    if bullish_weight >= bearish_weight:
        dominant, aligned_weight = "bullish", bullish_weight
    else:
        dominant, aligned_weight = "bearish", bearish_weight

    alignment_score = round((aligned_weight / total_weight) * 100.0, 1)
    aligned_timeframes = sorted(
        [tf for tf, d in directions.items() if d == dominant],
        key=lambda tf: timeframe_weights.get(tf, 0), reverse=True,
    )
    return {
        "alignment_score": alignment_score, "dominant_direction": dominant,
        "aligned_timeframes": aligned_timeframes, "directions": directions,
    }


def compute_momentum(snap: MarketSnapshot, tf_config: TimeframeConfig) -> FactorResult:
    per_tf_scores: Dict[str, float] = {}
    reasons: list[str] = []
    daily_raw = None

    for tf in tf_config.timeframes:
        df = snap.ohlcv.get(tf)
        result = _single_timeframe_score(df)
        if result is None:
            continue
        score, note, raw = result
        per_tf_scores[tf] = score
        reasons.append(f"[{tf}] {note} (score {score:.0f})")
        if tf == "1d":
            daily_raw = raw

    if not per_tf_scores:
        return FactorResult(
            name="momentum",
            score=50.0,
            reasons=["Not enough OHLCV history on any timeframe (need 200+ candles for EMA200) — neutral default"],
            available=False,
        )

    used_weight = sum(tf_config.timeframe_weights.get(tf, 0) for tf in per_tf_scores)
    if used_weight <= 0:
        blended = sum(per_tf_scores.values()) / len(per_tf_scores)
    else:
        blended = sum(
            per_tf_scores[tf] * (tf_config.timeframe_weights.get(tf, 0) / used_weight)
            for tf in per_tf_scores
        )

    divergence_score = 50.0
    if daily_raw is not None:
        rsi_div = _detect_divergence(daily_raw["close_series"], daily_raw["rsi_series"])
        macd_div = _detect_divergence(daily_raw["close_series"], daily_raw["macd_hist_series"])
        if "bearish" in (rsi_div, macd_div):
            divergence_score = 25.0
            reasons.append(f"Bearish divergence detected (price making highs, {'RSI' if rsi_div=='bearish' else 'MACD'} isn't confirming)")
        elif "bullish" in (rsi_div, macd_div):
            divergence_score = 75.0
            reasons.append(f"Bullish divergence detected (price making lows, {'RSI' if rsi_div=='bullish' else 'MACD'} isn't confirming)")
        else:
            reasons.append("No momentum divergence detected")

    composite = blended * 0.85 + divergence_score * 0.15

    # 15m deliberately excluded from the ALIGNMENT/direction calculation
    # specifically, though it still contributes to the overall momentum
    # magnitude via `blended` above. Standard professional practice:
    # higher timeframes decide direction, lower timeframes only help
    # with entry timing — they don't get a vote on WHETHER to trade at
    # all. At 15m you're largely reading order-flow microstructure, not
    # trend; letting it participate in the alignment score that gates
    # "Ready" classification meant noise on the shortest timeframe could
    # either falsely help trigger a Ready call, or drag down genuine
    # agreement on the timeframes that actually matter for conviction.
    alignment_tf_scores = {tf: score for tf, score in per_tf_scores.items() if tf != "15m"}
    alignment = _compute_alignment(alignment_tf_scores, tf_config.timeframe_weights)

    if alignment["dominant_direction"] == "bullish":
        boost = (alignment["alignment_score"] / 100.0) * 15.0
        composite = _clamp(composite + boost)
        reasons.append(
            f"Aligned bullish across {', '.join(alignment['aligned_timeframes'])} "
            f"({alignment['alignment_score']:.0f}% weighted agreement) — real multi-timeframe conviction, not one timeframe alone"
        )
    elif alignment["dominant_direction"] == "bearish":
        boost = (alignment["alignment_score"] / 100.0) * 15.0
        composite = _clamp(composite - boost)
        reasons.append(
            f"Aligned bearish across {', '.join(alignment['aligned_timeframes'])} "
            f"({alignment['alignment_score']:.0f}% weighted agreement) — real multi-timeframe conviction on the downside, a genuine short setup, not just an absent long"
        )
    else:
        # Timeframes disagree with each other — deliberately dampen toward
        # neutral rather than let one strong timeframe carry the score,
        # matching "don't show me coins for no reason": a coin with
        # conflicting signals across timeframes deserves LESS confidence,
        # not the same confidence as one with genuine agreement.
        pre_dampen = composite
        composite = composite * 0.85 + 50 * 0.15
        if abs(pre_dampen - composite) > 1.0:
            reasons.append("Mixed signals across timeframes — no clear multi-timeframe agreement, conviction dampened toward neutral")

    return FactorResult(
        name="momentum",
        score=round(_clamp(composite), 1),
        reasons=reasons,
        raw={
            "per_timeframe": per_tf_scores, "divergence_score": divergence_score,
            "alignment_score": alignment["alignment_score"], "dominant_direction": alignment["dominant_direction"],
            "aligned_timeframes": alignment["aligned_timeframes"],
        },
        available=True,
    )

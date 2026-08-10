"""
Pillar 1: Strength (v2)
--------------------------
Relative strength vs BTC AND vs sector peers, volume quality (surge +
value-area profile + OBV slope), market structure (higher-highs/lows +
break of structure), and liquidity depth.

Uses the daily ('1d') OHLCV series as the primary timeframe for
structure/volume reads, and 1h/4h/1d for the relative-strength blend —
strength is a slower-moving read than momentum, so leaning on daily data
avoids injecting short-timeframe noise into what should be a stable signal.
"""

from __future__ import annotations
from typing import Optional
import numpy as np
import pandas as pd
import ta

from ..models import MarketSnapshot, FactorResult


def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def _normalize(v: float, lo: float, hi: float) -> float:
    if hi == lo:
        return 50.0
    return _clamp((v - lo) / (hi - lo) * 100.0)


def _cum_return_pct(df: Optional[pd.DataFrame], periods: int) -> Optional[float]:
    if df is None or len(df) <= periods:
        return None
    end = df["close"].iloc[-1]
    start = df["close"].iloc[-1 - periods]
    if start == 0:
        return None
    return (end / start - 1.0) * 100.0


# ---------------------------------------------------- relative strength

_TF_PERIODS = {"1h": 1, "4h": 4, "1d": 1}   # periods-back within each tf's own candles for a ~1-candle return
_TF_BLEND_WEIGHTS = {"1h": 0.20, "4h": 0.35, "1d": 0.45}


def _relative_strength(
    snap: MarketSnapshot,
    btc_snap: Optional[MarketSnapshot],
    sector_bases: list[str],
    sector_snapshots: dict,
) -> tuple[float, list[str]]:
    reasons: list[str] = []
    per_tf_rel: dict[str, float] = {}

    for tf, periods in _TF_PERIODS.items():
        coin_df = snap.ohlcv.get(tf)
        coin_ret = _cum_return_pct(coin_df, periods)
        if coin_ret is None:
            continue

        btc_df = btc_snap.ohlcv.get(tf) if btc_snap else None
        btc_ret = _cum_return_pct(btc_df, periods) if btc_df is not None else None

        sector_ret = None
        if sector_bases:
            peer_returns = []
            for peer_base in sector_bases:
                peer_snap = sector_snapshots.get(peer_base)
                if peer_snap is None:
                    continue
                r = _cum_return_pct(peer_snap.ohlcv.get(tf), periods)
                if r is not None:
                    peer_returns.append(r)
            if peer_returns:
                sector_ret = sum(peer_returns) / len(peer_returns)

        if btc_ret is None:
            continue

        rs_btc = coin_ret - btc_ret
        if sector_ret is not None:
            rs_sector = coin_ret - sector_ret
            rs = rs_btc * 0.65 + rs_sector * 0.35
        else:
            rs = rs_btc  # no sector data available — fall back to BTC-only for this tf

        per_tf_rel[tf] = rs

    if not per_tf_rel:
        return 50.0, ["Insufficient data for relative strength — neutral default"]

    used_weight = sum(_TF_BLEND_WEIGHTS.get(tf, 0) for tf in per_tf_rel)
    if used_weight <= 0:
        blended = sum(per_tf_rel.values()) / len(per_tf_rel)
    else:
        blended = sum(per_tf_rel[tf] * (_TF_BLEND_WEIGHTS.get(tf, 0) / used_weight) for tf in per_tf_rel)

    score = _normalize(blended, -20, 20)
    tf_str = ", ".join(f"{tf}: {v:+.1f}pp" for tf, v in per_tf_rel.items())
    sector_note = "vs BTC+sector blend" if sector_bases else "vs BTC only (no sector peers configured)"
    reasons.append(f"Relative strength {sector_note} — {tf_str}")
    return score, reasons


# ---------------------------------------------------------- volume quality

def _volume_quality(df: Optional[pd.DataFrame], market_cap_usd: Optional[float]) -> tuple[float, list[str]]:
    reasons = []
    if df is None or len(df) < 25:
        return 40.0, ["Not enough volume history — neutral-low default"]

    recent20 = df.tail(20)

    # 1. Volume surge: current 24h (last candle) vs 20-candle average
    avg_vol = recent20["volume"].iloc[:-1].mean()
    last_vol = recent20["volume"].iloc[-1]
    surge_pct = (last_vol / avg_vol - 1.0) if avg_vol else 0.0
    surge_score = _normalize(surge_pct, -0.3, 2.0)
    reasons.append(f"Volume surge {surge_pct*100:+.0f}% vs 20-period average")

    # 2. Volume profile (value-area proxy): % of recent volume transacted
    #    within ±1 ATR of the current close — a lightweight stand-in for
    #    a full volume-profile value-area calculation
    atr = ta.volatility.average_true_range(df["high"], df["low"], df["close"], window=14).iloc[-1]
    last_close = df["close"].iloc[-1]
    if atr and not pd.isna(atr):
        in_value_area = recent20[(recent20["close"] >= last_close - atr) & (recent20["close"] <= last_close + atr)]
        profile_pct = in_value_area["volume"].sum() / recent20["volume"].sum() if recent20["volume"].sum() else 0
    else:
        profile_pct = 0.5
    profile_score = _normalize(profile_pct, 0, 1)
    reasons.append(f"{profile_pct*100:.0f}% of recent volume concentrated near current price (value area)")

    # 3. OBV slope: linear regression slope of On-Balance Volume, normalized
    obv = ta.volume.on_balance_volume(df["close"], df["volume"])
    obv_recent = obv.tail(14).values
    if len(obv_recent) >= 2 and obv_recent.std() > 0:
        x = np.arange(len(obv_recent))
        slope = np.polyfit(x, obv_recent, 1)[0]
        slope_normalized = slope / (abs(obv_recent).mean() + 1e-9)  # scale-independent
        obv_score = _normalize(slope_normalized, -0.1, 0.1)
    else:
        obv_score = 50.0
    reasons.append(f"OBV slope score {obv_score:.0f}/100 (rising OBV = accumulation)")

    combined = surge_score * 0.4 + profile_score * 0.3 + obv_score * 0.3
    return _clamp(combined), reasons


# --------------------------------------------------------- market structure

def _market_structure(df: Optional[pd.DataFrame]) -> tuple[float, list[str]]:
    if df is None or len(df) < 25:
        return 50.0, ["Not enough candles for structure read — neutral default"]

    window = df.tail(20)
    mid = len(window) // 2
    first_half, second_half = window.iloc[:mid], window.iloc[mid:]

    prior_high = first_half["high"].max()
    prior_low = first_half["low"].min()
    higher_high = second_half["high"].max() > prior_high
    higher_low = second_half["low"].min() > prior_low
    lower_high = second_half["high"].max() < prior_high
    lower_low = second_half["low"].min() < prior_low

    # Break of structure: most recent close breaking beyond the prior swing extreme
    last_close = df["close"].iloc[-1]
    bos_bullish = last_close > prior_high
    bos_bearish = last_close < prior_low

    if higher_high and higher_low:
        return 85.0, ["Structure: higher highs AND higher lows — healthy uptrend"]
    if lower_high and lower_low:
        return 15.0, ["Structure: lower highs AND lower lows — downtrend intact"]
    if bos_bullish:
        return 70.0, ["Break of structure (bullish): close broke above the prior swing high"]
    if bos_bearish:
        return 30.0, ["Break of structure (bearish): close broke below the prior swing low"]
    if higher_high and not higher_low:
        return 55.0, ["Structure: higher highs but lows not confirming — choppy strength"]
    if higher_low and not higher_high:
        return 60.0, ["Structure: higher lows, base building — early-stage strength"]
    return 40.0, ["Structure: mixed / range-bound"]


# ------------------------------------------------------------- liquidity

def _liquidity_score(snap: MarketSnapshot) -> tuple[float, list[str]]:
    depth_score = 50.0
    reasons = []
    if snap.market_cap_usd and snap.market_cap_usd > 0:
        ratio = snap.volume_24h_usd / snap.market_cap_usd
        depth_score = _normalize(ratio, 0, 0.35)
        reasons.append(f"24h volume is {ratio*100:.1f}% of market cap")

    spread_score = 50.0
    if snap.bid_ask_spread_pct is not None and snap.bid_ask_spread_pct > 0:
        spread_score = _normalize(1 / max(snap.bid_ask_spread_pct, 0.01), 0, 100)
        reasons.append(f"Bid/ask spread {snap.bid_ask_spread_pct:.3f}%")

    combined = depth_score * 0.6 + spread_score * 0.4
    return _clamp(combined), reasons


def compute_strength(
    snap: MarketSnapshot,
    btc_snap: Optional[MarketSnapshot],
    sector_bases: Optional[list[str]] = None,
    sector_snapshots: Optional[dict] = None,
) -> FactorResult:
    df = snap.ohlcv.get("1d")
    reasons: list[str] = []

    rs_score, rs_reasons = _relative_strength(snap, btc_snap, sector_bases or [], sector_snapshots or {})
    vol_score, vol_reasons = _volume_quality(df, snap.market_cap_usd)
    struct_score, struct_reasons = _market_structure(df)
    liq_score, liq_reasons = _liquidity_score(snap)

    reasons.extend(rs_reasons)
    reasons.extend(vol_reasons)
    reasons.extend(struct_reasons)
    reasons.extend(liq_reasons)

    composite = rs_score * 0.35 + vol_score * 0.25 + struct_score * 0.25 + liq_score * 0.15

    return FactorResult(
        name="strength",
        score=round(_clamp(composite), 1),
        reasons=reasons,
        raw={"rs_score": rs_score, "vol_score": vol_score, "struct_score": struct_score, "liq_score": liq_score},
        available=True,
    )

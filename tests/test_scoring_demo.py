"""
Synthetic-data pipeline test.

No network access needed — this builds fake but internally-consistent
OHLCV/social data for a "strong" coin and a "weak" coin, runs them through
every pillar and the composite scorer, and asserts the strong one scores
meaningfully higher. This is what to run first after cloning, before
plugging in real API keys, to confirm the scoring logic itself is sound.

Run: python -m tests.test_scoring_demo   (from the project root)
"""

from __future__ import annotations
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import zlib


def _stable_seed(*parts) -> int:
    """Deterministic seed from string parts. Python's built-in hash() is
    randomized per-process for strings (a security feature, PYTHONHASHSEED),
    which made earlier versions of this test flaky across runs — this fixes
    that by using a stable hash instead."""
    return zlib.crc32("_".join(str(p) for p in parts).encode()) % 1000

from opportunity_scanner.config import ScannerConfig
from opportunity_scanner.models import MarketSnapshot
from opportunity_scanner.filters import passes_quality_filters
from opportunity_scanner.factors import compute_strength, compute_oi_dynamics, compute_momentum, compute_social
from opportunity_scanner.scoring import combine_factors
from opportunity_scanner.risk import classify_risk_tier


def make_ohlcv(n=260, trend_pct_per_candle=0.3, noise=1.0, start_price=100.0, seed=0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    prices = [start_price]
    for _ in range(n - 1):
        drift = trend_pct_per_candle / 100.0
        shock = rng.normal(0, noise / 100.0)
        prices.append(prices[-1] * (1 + drift + shock))
    close = np.array(prices)
    high = close * (1 + np.abs(rng.normal(0, 0.004, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.004, n)))
    open_ = close * (1 + rng.normal(0, 0.002, n))
    volume = np.abs(rng.normal(1_000_000, 150_000, n))
    ts = pd.date_range(end=pd.Timestamp.now('UTC'), periods=n, freq="D")
    return pd.DataFrame({"timestamp": ts, "open": open_, "high": high, "low": low, "close": close, "volume": volume})


def make_oi_history(n=48, trend_pct_total=15.0, seed=1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    base = 10_000_000
    steps = np.linspace(0, trend_pct_total / 100.0, n) + rng.normal(0, 0.01, n).cumsum() * 0.05
    oi = base * (1 + steps)
    ts = pd.date_range(end=pd.Timestamp.now('UTC'), periods=n, freq="h")
    return pd.DataFrame({"ts": ts, "oi_usd": oi})


def make_snapshot(name: str, trend_pct: float, oi_trend_pct: float, social_velocity_pct: float, market_cap=500_000_000) -> MarketSnapshot:
    ohlcv = {tf: make_ohlcv(n=260, trend_pct_per_candle=trend_pct, seed=_stable_seed(name, tf)) for tf in ["15m", "1h", "4h", "1d"]}
    oi_hist = make_oi_history(trend_pct_total=oi_trend_pct, seed=_stable_seed(name))
    social = {
        "galaxy_score": 70 if social_velocity_pct > 0 else 40,
        "galaxy_score_7d_ago": 55 if social_velocity_pct > 0 else 50,
        "alt_rank": 50 if social_velocity_pct > 0 else 400,
        "alt_rank_7d_ago": 90 if social_velocity_pct > 0 else 380,
        "social_volume_24h": 1000 * (1 + social_velocity_pct / 100),
        "social_volume_baseline": 1000,
        "social_volume_baseline_30d": 900,
        "sentiment": 65 if social_velocity_pct > 0 else 40,
        "sentiment_prev": 50,
        "interactions_24h": 8000,
        "interactions_baseline": 6000,
        "likes_24h": 5000, "replies_24h": 1200, "retweets_24h": 900, "quote_tweets_24h": 300,
    }
    last_close = ohlcv["1d"]["close"].iloc[-1]
    return MarketSnapshot(
        symbol=f"{name}/USDT",
        base=name,
        price=last_close,
        market_cap_usd=market_cap,
        volume_24h_usd=5_000_000,
        bid_ask_spread_pct=0.1,
        exchange_listings=3,
        ohlcv=ohlcv,
        open_interest_history=oi_hist,
        open_interest_usd=oi_hist["oi_usd"].iloc[-1],
        funding_rate=0.0001,
        long_short_ratio=1.1,
        social=social,
    )


def run_pipeline(snap: MarketSnapshot, btc_snap: MarketSnapshot, config: ScannerConfig):
    passed, notes = passes_quality_filters(snap, config.filters)

    daily = snap.ohlcv["1d"]
    price_change_24h = (daily["close"].iloc[-1] / daily["close"].iloc[-2] - 1) * 100

    factors = {
        "strength": compute_strength(snap, btc_snap, sector_bases=[], sector_snapshots={}),
        "oi_dynamics": compute_oi_dynamics(snap, price_change_24h),
        "momentum": compute_momentum(snap, config.timeframe_config),
        "social": compute_social(snap),
    }
    composite, confidence, confidence_label, signal, weights_used, reasons = combine_factors(
        factors, config.weights, config.signal_bands, config.confidence_bands
    )
    risk = classify_risk_tier(market_cap_rank=50, market_cap_usd=snap.market_cap_usd, volume_24h_usd=snap.volume_24h_usd)

    print(f"\n=== {snap.base} ===")
    print(f"Filters passed: {passed} ({notes[0] if notes else ''})")
    for name, f in factors.items():
        print(f"  {name:12s}: {f.score:5.1f}  | {f.reasons[0] if f.reasons else ''}")
    print(f"  COMPOSITE   : {composite:5.1f}  -> {signal}  (confidence: {confidence:.0f} [{confidence_label}], risk tier: {risk})")
    print("  Top reasons:")
    for r in reasons:
        print(f"    - {r}")
    return composite, signal


def main():
    config = ScannerConfig()  # default weights: strength .30, oi .20, momentum .30, social .20

    btc = make_snapshot("BTC", trend_pct=0.15, oi_trend_pct=5, social_velocity_pct=10, market_cap=1_800_000_000_000)
    strong_coin = make_snapshot("STRONGCOIN", trend_pct=1.2, oi_trend_pct=25, social_velocity_pct=180, market_cap=400_000_000)
    weak_coin = make_snapshot("WEAKCOIN", trend_pct=-0.9, oi_trend_pct=-10, social_velocity_pct=-30, market_cap=150_000_000)

    strong_score, strong_signal = run_pipeline(strong_coin, btc, config)
    weak_score, weak_signal = run_pipeline(weak_coin, btc, config)

    assert strong_score > weak_score, (
        f"Expected strong coin to outscore weak coin: {strong_score} vs {weak_score}"
    )
    assert strong_signal in ("Strong Buy", "Buy"), f"Expected strong coin to grade well, got {strong_signal}"
    assert weak_signal in ("Caution", "Strong Avoid"), f"Expected weak coin to grade poorly, got {weak_signal}"

    print("\n✅ Pipeline sanity check passed: strong coin scored higher and graded correctly.")


if __name__ == "__main__":
    main()

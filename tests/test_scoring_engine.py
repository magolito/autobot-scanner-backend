"""
Scoring engine test — synthetic CoinMetrics, no network needed.

Checks:
  1. A "strong" metrics set outscores a "weak" one and grades correctly
  2. Hard filters correctly reject a low-volume coin
  3. calculate_* methods don't crash on a mostly-empty CoinMetrics (missing-data robustness)
  4. Confidence is lower when fewer fields are populated
  5. Regime dampening reduces a bullish score under Risk-Off, and never touches BTC
  6. Flags fire on the conditions they're meant to catch
"""

from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.scoring_engine import ScoringEngine, CoinMetrics, Grade, HardFilters


def strong_metrics() -> CoinMetrics:
    return CoinMetrics(
        symbol="STRONGCOIN", price=12.50, volume_24h=40_000_000, market_cap=400_000_000,
        exchange_listings=4, bid_ask_spread_pct=0.05,
        rs_vs_btc_1h=3.0, rs_vs_btc_4h=6.5, rs_vs_btc_24h=9.0,
        volume_surge=1.4, volume_profile_pct=0.75, obv_slope=0.6,
        higher_high=True, higher_low=True,
        oi_change_1h=4.0, oi_change_4h=12.0, oi_change_24h=22.0,
        price_change_1h=1.2, price_change_4h=3.5, price_change_24h=7.0,
        funding_rate=0.0003, funding_rate_prev=0.0006,
        long_short_ratio=1.05, open_interest_usd=30_000_000,
        trend_alignment_score=88.0, adx=34.0, plus_di=30.0, minus_di=12.0,
        rsi_1h=68.0, rsi_4h=64.0, macd_hist=0.8, macd_hist_prev=0.5, roc=9.0, stochastic_k=72.0,
        bullish_divergence=False, bearish_divergence=False,
        social_volume_change=1.9, sentiment=72.0, sentiment_shift=15.0,
        galaxy_score=75.0, galaxy_score_prior=58.0, alt_rank=40, engagement_score=18.0,
    )


def weak_metrics() -> CoinMetrics:
    return CoinMetrics(
        symbol="WEAKCOIN", price=0.85, volume_24h=8_000_000, market_cap=150_000_000,
        exchange_listings=2, bid_ask_spread_pct=0.3,
        rs_vs_btc_1h=-1.0, rs_vs_btc_4h=-4.0, rs_vs_btc_24h=-8.0,
        volume_surge=-0.2, volume_profile_pct=0.3, obv_slope=-0.5,
        higher_high=False, higher_low=False,
        oi_change_1h=-2.0, oi_change_4h=-6.0, oi_change_24h=-11.0,
        price_change_1h=-0.8, price_change_4h=-2.5, price_change_24h=-5.0,
        funding_rate=-0.0009, funding_rate_prev=-0.0004,
        long_short_ratio=0.6, open_interest_usd=5_000_000,
        trend_alignment_score=15.0, adx=28.0, plus_di=10.0, minus_di=29.0,
        rsi_1h=22.0, rsi_4h=25.0, macd_hist=-0.6, macd_hist_prev=-0.3, roc=-8.0, stochastic_k=15.0,
        bullish_divergence=False, bearish_divergence=True,
        social_volume_change=-0.35, sentiment=32.0, sentiment_shift=-12.0,
        galaxy_score=30.0, galaxy_score_prior=42.0, alt_rank=380, engagement_score=4.0,
    )


def low_volume_metrics() -> CoinMetrics:
    return CoinMetrics(symbol="TINYCOIN", price=0.001, volume_24h=200_000, market_cap=2_000_000, exchange_listings=1)


def sparse_metrics() -> CoinMetrics:
    """Only the required fields — everything else missing. Should not crash."""
    return CoinMetrics(symbol="SPARSECOIN", price=1.0, volume_24h=6_000_000)


def main():
    engine = ScoringEngine()

    # 1. Strong vs weak
    strong = engine.score(strong_metrics())
    weak = engine.score(weak_metrics())
    print(f"\nSTRONGCOIN -> {strong.opportunity_score} ({strong.grade.value}), confidence {strong.confidence}")
    print(f"  pillars: {strong.pillar_scores}")
    print(f"  thesis: {strong.thesis}")
    print(f"  flags: {strong.flags}")
    print(f"\nWEAKCOIN -> {weak.opportunity_score} ({weak.grade.value}), confidence {weak.confidence}")
    print(f"  pillars: {weak.pillar_scores}")
    print(f"  flags: {weak.flags}")

    assert strong.opportunity_score > weak.opportunity_score
    assert strong.grade in (Grade.STRONG_OPPORTUNITY, Grade.OPPORTUNITY)
    assert weak.grade in (Grade.WATCHLIST, Grade.IGNORE)

    # 2. Hard filters
    ok, reasons = engine.passes_hard_filters(low_volume_metrics())
    print(f"\nLow-volume coin passes filters: {ok} ({reasons})")
    assert ok is False

    ok2, _ = engine.passes_hard_filters(strong_metrics())
    assert ok2 is True

    # 3. Missing-data robustness — should not raise
    sparse_result = engine.score(sparse_metrics())
    print(f"\nSPARSECOIN (mostly empty) -> {sparse_result.opportunity_score} ({sparse_result.grade.value}), confidence {sparse_result.confidence}")
    for pillar_val in sparse_result.pillar_scores.model_dump().values():
        assert pillar_val == 50.0, "Expected neutral 50 default when a pillar has zero data"

    # 4. Confidence should be lower for sparse data than for rich data
    assert sparse_result.confidence < strong.confidence, "Expected lower confidence with far less data available"

    # 5. Regime dampening
    dampened = engine.score(strong_metrics(), regime_label="Risk-Off", regime_score=30.0, is_btc=False)
    print(f"\nSTRONGCOIN under Risk-Off -> {dampened.opportunity_score} (was {strong.opportunity_score}), note: {dampened.regime_note}")
    assert dampened.opportunity_score < strong.opportunity_score
    assert dampened.regime_note is not None

    btc_metrics = strong_metrics()
    btc_metrics.symbol = "BTC"
    btc_undamped = engine.score(btc_metrics, regime_label="Risk-Off", regime_score=30.0, is_btc=True)
    btc_normal = engine.score(btc_metrics)
    print(f"BTC under its own Risk-Off regime -> {btc_undamped.opportunity_score} (undamped, should equal {btc_normal.opportunity_score})")
    assert btc_undamped.opportunity_score == btc_normal.opportunity_score, "BTC should never dampen itself"

    # 6. Flags
    assert any("Divergence" in f for f in weak.flags), f"Expected a divergence flag on WEAKCOIN, got {weak.flags}"

    print("\n✅ Scoring engine test passed: ranks correctly, filters correctly, degrades gracefully on missing data, confidence reflects completeness, regime dampens correctly and exempts BTC, flags fire as expected.")


if __name__ == "__main__":
    main()

"""
Regime awareness test — synthetic data, no network needed.

Verifies:
  1. A strongly-trending-up BTC classifies as Risk-On (or at least not Risk-Off)
  2. A strongly-trending-down, high-volatility BTC classifies as Risk-Off
  3. Under Risk-Off, a bullish alt score gets dampened
  4. Under Risk-Off, a bearish alt score passes through UNCHANGED
  5. BTC never dampens itself
"""

from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.config import ScannerConfig
from opportunity_scanner.regime import compute_market_regime, apply_regime_filter
from opportunity_scanner.models import MarketSnapshot
from tests.test_scoring_demo import make_snapshot, make_ohlcv, _stable_seed


def make_volatile_bearish_btc(market_cap=1_800_000_000_000) -> MarketSnapshot:
    """
    A real market crash has elevated realized volatility, not just smooth
    decay — this generates a bearish trend WITH the higher noise a real
    selloff has, so the regime's volatility component reads correctly.
    """
    ohlcv = {
        tf: make_ohlcv(n=260, trend_pct_per_candle=-1.2, noise=4.0, seed=_stable_seed("BTC_CRASH", tf))
        for tf in ["15m", "1h", "4h", "1d"]
    }
    last_close = ohlcv["1d"]["close"].iloc[-1]
    return MarketSnapshot(
        symbol="BTC/USDT", base="BTC", price=last_close, market_cap_usd=market_cap,
        volume_24h_usd=5_000_000, bid_ask_spread_pct=0.1, exchange_listings=3, ohlcv=ohlcv,
        open_interest_history=None, funding_rate=0.0001, long_short_ratio=0.8, social=None,
    )


def main():
    config = ScannerConfig()

    btc_bullish = make_snapshot("BTC", trend_pct=0.25, oi_trend_pct=10, social_velocity_pct=20, market_cap=1_800_000_000_000)
    btc_bearish = make_volatile_bearish_btc()

    regime_on = compute_market_regime(btc_bullish, config.timeframe_config, config.regime_config)
    regime_off = compute_market_regime(btc_bearish, config.timeframe_config, config.regime_config)

    print(f"Bullish BTC regime: {regime_on.label} (score {regime_on.score})")
    print(f"Bearish BTC regime: {regime_off.label} (score {regime_off.score})")

    assert regime_on.label != "Risk-Off", f"Expected bullish BTC to NOT be Risk-Off, got {regime_on.label}"
    assert regime_off.label == "Risk-Off", f"Expected bearish/volatile BTC to be Risk-Off, got {regime_off.label}"

    # Bullish alt score under Risk-Off regime should be dampened
    bullish_alt_score = 78.0
    adjusted, note = apply_regime_filter(bullish_alt_score, regime_off, config.regime_config, is_btc_itself=False)
    print(f"\nBullish alt score {bullish_alt_score} under Risk-Off -> {adjusted}  ({note})")
    assert adjusted < bullish_alt_score, "Expected bullish score to be dampened under Risk-Off"
    assert note is not None, "Expected an explanatory note when dampening occurs"

    # Bearish alt score under Risk-Off should pass through unchanged
    bearish_alt_score = 22.0
    adjusted2, note2 = apply_regime_filter(bearish_alt_score, regime_off, config.regime_config, is_btc_itself=False)
    print(f"Bearish alt score {bearish_alt_score} under Risk-Off -> {adjusted2}  (note: {note2})")
    assert adjusted2 == bearish_alt_score, "Expected bearish score to pass through unchanged"
    assert note2 is None

    # BTC itself should never be dampened, even at a high score under its own Risk-Off regime
    btc_self_score = 85.0
    adjusted3, note3 = apply_regime_filter(btc_self_score, regime_off, config.regime_config, is_btc_itself=True)
    print(f"BTC's own score {btc_self_score} under its own Risk-Off regime -> {adjusted3}  (note: {note3})")
    assert adjusted3 == btc_self_score, "BTC should never dampen itself"
    assert note3 is None

    # Bullish alt score under Risk-On should NOT be dampened
    adjusted4, note4 = apply_regime_filter(bullish_alt_score, regime_on, config.regime_config, is_btc_itself=False)
    print(f"Bullish alt score {bullish_alt_score} under {regime_on.label} -> {adjusted4}  (note: {note4})")
    assert adjusted4 == bullish_alt_score, "Expected no dampening under a healthy regime"

    print("\n✅ Regime awareness test passed: dampens bullish alt scores only under Risk-Off, never touches BTC itself or bearish calls.")


if __name__ == "__main__":
    main()

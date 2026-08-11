"""
Risk classification liquidity/volatility test — the real fix from a
direct quant-style review: market cap rank alone is a weak proxy for
actual risk. A $500M coin with deep order books is genuinely different
from a $500M coin nobody's trading — rank doesn't distinguish them.
`classify_risk_tier` used to accept a volume_24h_usd parameter and never
actually use it. Now liquidity (volume relative to the coin's own size)
and realized volatility can downgrade a tier — never upgrade one.
"""

from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from opportunity_scanner.risk import classify_risk_tier, compute_realized_volatility


def main():
    # 1. Healthy liquidity, no volatility data -> unaffected, matches old rank-only behavior
    tier1 = classify_risk_tier(market_cap_rank=50, market_cap_usd=10_000_000_000, volume_24h_usd=800_000_000)
    assert tier1 == "core", f"Healthy liquidity (8% of market cap) should not be downgraded, got {tier1}"
    print(f"1. Healthy liquidity ratio (8% of market cap) -> 'core' unaffected, matches original rank-based behavior: OK")

    # 2. THE ACTUAL FIX: same rank, same market cap, but genuinely thin volume -> downgraded to high_risk
    tier2 = classify_risk_tier(market_cap_rank=50, market_cap_usd=10_000_000_000, volume_24h_usd=50_000_000)  # 0.5% of mcap
    assert tier2 == "high_risk", f"Thin liquidity (0.5% of market cap) should downgrade to high_risk regardless of rank, got {tier2}"
    print(f"2. THE ACTUAL FIX: same rank/market cap, but genuinely thin volume (0.5% of mcap) -> correctly downgraded to 'high_risk': OK")

    # 3. Exactly at the liquidity boundary
    tier3 = classify_risk_tier(market_cap_rank=50, market_cap_usd=10_000_000_000, volume_24h_usd=200_000_000)  # exactly 2%
    assert tier3 == "core", "Exactly at the 2% liquidity floor should NOT be downgraded (>=, boundary inclusive on the healthy side)"
    print("3. Exactly at the liquidity boundary (2%) correctly NOT downgraded — inclusive on the healthy side: OK")

    # 4. Extreme volatility downgrades core -> small_cap (one tier, not straight to high_risk)
    tier4 = classify_risk_tier(market_cap_rank=50, market_cap_usd=10_000_000_000, volume_24h_usd=800_000_000, realized_volatility_annualized=2.5)
    assert tier4 == "small_cap", f"Extreme volatility should downgrade 'core' by one tier to 'small_cap', not straight to high_risk, got {tier4}"
    print(f"4. Extreme volatility (250% annualized) downgrades 'core' -> 'small_cap' (one tier, proportionate), not straight to high_risk: OK")

    # 5. Extreme volatility on an already-small_cap coin -> high_risk
    tier5 = classify_risk_tier(market_cap_rank=200, market_cap_usd=200_000_000, volume_24h_usd=20_000_000, realized_volatility_annualized=2.5)
    assert tier5 == "high_risk", f"Extreme volatility on an already-small_cap coin should downgrade to high_risk, got {tier5}"
    print("5. Extreme volatility on an already-small_cap coin correctly downgrades further to 'high_risk': OK")

    # 6. Normal (even if elevated-for-non-crypto) volatility does NOT trigger the downgrade —
    # crypto is volatile by nature, the threshold should tolerate normal altcoin conditions
    tier6 = classify_risk_tier(market_cap_rank=50, market_cap_usd=10_000_000_000, volume_24h_usd=800_000_000, realized_volatility_annualized=1.0)  # 100% annualized, normal for crypto
    assert tier6 == "core", f"Normal crypto volatility (100% annualized) should NOT trigger a downgrade, got {tier6}"
    print("6. Normal crypto-typical volatility (100% annualized) correctly does NOT trigger a downgrade — the threshold tolerates real market conditions, not just calm ones: OK")

    # 7. Never upgrades — a coin genuinely outside the ranked tiers stays high_risk
    # regardless of how good its liquidity/volatility numbers look
    tier7 = classify_risk_tier(market_cap_rank=None, market_cap_usd=None, volume_24h_usd=1_000_000_000, realized_volatility_annualized=0.1)
    assert tier7 == "high_risk", "A coin with no rank data should stay high_risk regardless of how good other numbers look — this is a downgrade-only mechanism, never an upgrade"
    print("7. Missing rank data stays 'high_risk' even with excellent liquidity/volatility numbers — genuinely downgrade-only, never an upgrade path: OK")

    # 8. compute_realized_volatility — the actual helper, tested directly
    # Build a genuinely calm price series (small consistent daily moves)
    rng = np.random.default_rng(42)
    calm_returns = rng.normal(0, 0.01, 30)  # ~1% daily std dev -> modest annualized vol
    calm_prices = 100 * np.cumprod(1 + calm_returns)
    calm_df = pd.DataFrame({"close": calm_prices})
    calm_vol = compute_realized_volatility(calm_df)
    assert calm_vol is not None and calm_vol < 0.5, f"Expected a low realized volatility for a calm series, got {calm_vol}"
    print(f"8. compute_realized_volatility correctly computes a low annualized vol ({calm_vol:.2f}) for a genuinely calm price series: OK")

    # 9. A genuinely volatile series produces a much higher reading
    volatile_returns = rng.normal(0, 0.08, 30)  # ~8% daily std dev -> high annualized vol
    volatile_prices = 100 * np.cumprod(1 + volatile_returns)
    volatile_df = pd.DataFrame({"close": volatile_prices})
    volatile_vol = compute_realized_volatility(volatile_df)
    assert volatile_vol is not None and volatile_vol > calm_vol * 3, f"Expected the volatile series to read meaningfully higher than the calm one: {volatile_vol} vs {calm_vol}"
    print(f"9. A genuinely volatile price series correctly produces a much higher realized volatility reading ({volatile_vol:.2f} vs {calm_vol:.2f} calm): OK")

    # 10. Insufficient data degrades gracefully to None, not a crash
    short_df = pd.DataFrame({"close": [100, 101, 99]})
    assert compute_realized_volatility(short_df) is None
    assert compute_realized_volatility(None) is None
    print("10. Insufficient OHLCV history (or None) correctly degrades to None, not a crash: OK")

    print("\n✅ Risk liquidity/volatility test passed: the actual fix verified — volume_24h_usd (previously accepted but silently ignored) now genuinely affects classification, realized volatility is a real input, both are strictly downgrade-only, and normal crypto volatility is correctly distinguished from genuinely extreme conditions.")


if __name__ == "__main__":
    main()

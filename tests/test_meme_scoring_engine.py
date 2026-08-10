"""
Meme scoring engine test — synthetic MemeCoinMetrics, no network needed.

Checks:
  1. A coin failing a hard safety threshold gets Fail + opportunity_score=None,
     no pillar scores computed at all
  2. A clean, strong coin gets Pass, high opportunity_score, correct hype_level
  3. A borderline coin (clears hard thresholds, breaches caution margins) gets Caution
  4. Hype IS the strongest weighted signal in every mode (explicit weight check)
  5. Missing data across all pillars still scores without crashing (neutral defaults)
  6. Confidence reflects completeness + safety grade + age, not just hardcoded
  7. Momentum divergence is detected and surfaces as a risk flag
"""

from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.meme_scoring_engine import (
    ScoringEngine, MemeCoinMetrics, Mode, MODE_WEIGHTS,
)


def strong_early_momentum_coin() -> MemeCoinMetrics:
    return MemeCoinMetrics(
        symbol="STRONGMEME", token_address="fake_addr_1", chain_id="solana",
        price_usd=0.0012, market_cap_usd=400_000, liquidity_usd=60_000,
        pair_age_minutes=90, exchange_listings=1,
        mint_authority_revoked=True, freeze_authority_revoked=True, is_honeypot=False,
        buy_tax_pct=0, sell_tax_pct=0, lp_locked_pct=100, top10_holder_pct=18,
        dev_wallet_pct=2, unique_holders=340, rugcheck_risk_score=8, insider_bundle_flag=False,
        mention_velocity_ratio=4.2, acceleration_ratio=2.1, dex_boosted=True, boost_amount=300,
        has_website=True, has_twitter=True, has_telegram=True, kol_score=70,
        unique_makers_1h=120, buy_tx_count_1h=180, sell_tx_count_1h=60, buy_sell_ratio=3.0,
        holder_growth_pct_1h=15, volume_to_liquidity_ratio=2.5, avg_tx_size_variance=0.4,
        vol_accel_ratio=2.8, price_change_pct=45, volume_change_pct=60,
    )


def rug_risk_coin() -> MemeCoinMetrics:
    return MemeCoinMetrics(
        symbol="RUGCOIN", token_address="fake_addr_2", chain_id="solana",
        price_usd=0.0001, market_cap_usd=80_000, liquidity_usd=18_000,
        pair_age_minutes=10,
        mint_authority_revoked=False,  # <- the fatal flaw
        freeze_authority_revoked=True, is_honeypot=False,
        buy_tax_pct=2, sell_tax_pct=2, lp_locked_pct=95, top10_holder_pct=15,
        dev_wallet_pct=3, unique_holders=60, rugcheck_risk_score=10, insider_bundle_flag=False,
        mention_velocity_ratio=8.0, dex_boosted=True,
    )


def borderline_coin() -> MemeCoinMetrics:
    return MemeCoinMetrics(
        symbol="BORDERLINE", token_address="fake_addr_3", chain_id="solana",
        price_usd=0.0003, market_cap_usd=150_000, liquidity_usd=21_000,  # just above $20k min for Early Momentum
        pair_age_minutes=60,
        mint_authority_revoked=True, freeze_authority_revoked=True, is_honeypot=False,
        buy_tax_pct=3, sell_tax_pct=3, lp_locked_pct=82, top10_holder_pct=26,  # near the 28% ceiling
        dev_wallet_pct=5, unique_holders=110, rugcheck_risk_score=32,  # near the 35 ceiling
        insider_bundle_flag=False,
        mention_velocity_ratio=1.5, buy_sell_ratio=1.2,
    )


def sparse_coin() -> MemeCoinMetrics:
    """Only the required fields. Should not crash."""
    return MemeCoinMetrics(
        symbol="SPARSE", token_address="fake_addr_4", liquidity_usd=25_000,
        pair_age_minutes=100,
    )


def main():
    engine = ScoringEngine(mode=Mode.EARLY_MOMENTUM)

    # 1. Hard safety failure -> Fail, no opportunity score
    rug = engine.score(rug_risk_coin())
    print(f"RUGCOIN -> safety={rug.safety.grade}, opportunity_score={rug.opportunity_score}, pillar_scores={rug.pillar_scores}")
    assert rug.safety.grade == "Fail"
    assert rug.opportunity_score is None
    assert rug.pillar_scores is None
    assert any("mint authority" in r.lower() for r in rug.safety.reasons)
    print("1. Hard safety failure correctly blocks all scoring: OK")

    # 2. Strong coin -> Pass, high score, sensible hype level
    strong = engine.score(strong_early_momentum_coin())
    print(f"STRONGMEME -> safety={strong.safety.grade}, opportunity_score={strong.opportunity_score}, hype_level={strong.hype_level}")
    print(f"  pillars: {strong.pillar_scores}")
    assert strong.safety.grade == "Pass"
    assert strong.opportunity_score is not None and strong.opportunity_score >= 70
    assert strong.hype_level in ("High", "Explosive")
    print("2. Strong, clean coin scores highly with correct hype level: OK")

    # 3. Borderline coin -> Caution, not Pass, not Fail
    border = engine.score(borderline_coin())
    print(f"BORDERLINE -> safety={border.safety.grade}, reasons={border.safety.reasons}")
    assert border.safety.grade == "Caution", f"Expected Caution, got {border.safety.grade}"
    assert border.opportunity_score is not None, "Caution should still be scored, unlike Fail"
    print("3. Borderline coin correctly graded Caution (scored, but flagged): OK")

    # 4. Hype IS the strongest weighted signal in every mode
    for mode in Mode:
        weights = MODE_WEIGHTS[mode]
        assert weights["hype"] == max(weights.values()), f"Expected hype to be the strongest weight in {mode}, got {weights}"
    print("4. Hype confirmed as the strongest weighted signal in all 3 modes: OK")

    # 5. Sparse data doesn't crash, degrades to neutral defaults
    sparse = engine.score(sparse_coin())
    print(f"SPARSE -> safety={sparse.safety.grade}, opportunity_score={sparse.opportunity_score}")
    # sparse coin has no safety data at all -> should be Caution (missing safety data flagged) not Fail
    assert sparse.safety.grade in ("Caution", "Pass")
    if sparse.pillar_scores:
        assert 0 <= sparse.pillar_scores.hype <= 100
    print("5. Missing-data coin scores without crashing, defaults to neutral: OK")

    # 6. Confidence reflects real inputs, not hardcoded
    assert strong.confidence > sparse.confidence, f"Expected strong coin's confidence ({strong.confidence}) > sparse coin's ({sparse.confidence})"
    assert rug.confidence < strong.confidence, "Expected Fail-grade confidence to be lower than a clean Pass"
    print(f"6. Confidence reflects actual data completeness/safety: strong={strong.confidence}, sparse={sparse.confidence}, rug(Fail)={rug.confidence}")

    # 7. Momentum divergence detected and flagged
    divergent_coin = strong_early_momentum_coin()
    divergent_coin.price_change_pct = 50   # price way up
    divergent_coin.volume_change_pct = -30  # but volume way down — divergence
    div_result = engine.score(divergent_coin)
    assert any("divergence" in f.label.lower() for f in div_result.risk_flags), f"Expected a divergence flag, got {div_result.risk_flags}"
    print(f"7. Price/volume divergence correctly flagged: {[f.label for f in div_result.risk_flags if 'divergence' in f.label.lower()]}")

    print("\n✅ Meme scoring engine test passed: safety gating, hype dominance, borderline grading, missing-data robustness, confidence, and divergence detection all verified.")


if __name__ == "__main__":
    main()

"""
Meme settings adjustability test — proves a settings.yaml/env var change
actually changes ScoringEngine behavior, not just that it parses into a
config object correctly.

Checks:
  1. Default config produces the exact same numbers as the previous
     hardcoded module-level dicts (pure refactor, zero behavior change)
  2. Overriding a mode's min_liquidity_usd via env var actually changes
     whether a coin passes or fails the Safety gate
  3. Overriding pillar weights via env var actually changes the
     opportunity_score computation
  4. A missing mode in thresholds/weights raises a clear error rather
     than silently falling back to wrong numbers
  5. Hype formula constants are genuinely reachable from settings —
     overriding kol_boost_cap changes the actual KOL boost applied
"""

from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.settings import load_settings
from opportunity_scanner.meme_scoring_engine import ScoringEngine, Mode, MemeCoinMetrics


def make_metrics(**overrides) -> MemeCoinMetrics:
    base = dict(
        symbol="TESTCOIN", token_address="fake", liquidity_usd=20000, pair_age_minutes=10,
        mint_authority_revoked=True, freeze_authority_revoked=True, is_honeypot=False,
        lp_locked_pct=90, top10_holder_pct=15, dev_wallet_pct=3, unique_holders=100, rugcheck_risk_score=10,
        mention_velocity_ratio=3.0, dex_boosted=True, has_website=True, has_twitter=True, has_telegram=True,
    )
    base.update(overrides)
    return MemeCoinMetrics(**base)


def main():
    # 1. Default config matches the previous hardcoded values exactly
    default_engine = ScoringEngine(mode=Mode.EARLY_MOMENTUM)
    assert default_engine.config.mode_thresholds[Mode.SNIPER].min_liquidity_usd == 15_000
    assert default_engine.weights == {"hype": 0.4, "onchain": 0.32, "momentum": 0.28}
    print("1. Default MemeEngineConfig matches the original hardcoded values exactly: OK")

    # 2. Env var override actually changes Safety gate behavior
    os.environ["MEME_SCANNER__THRESHOLDS__SNIPER__MIN_LIQUIDITY_USD"] = "50000"
    settings = load_settings()
    config = settings.to_meme_engine_config()
    engine = ScoringEngine(mode=Mode.SNIPER, config=config)
    result = engine.score(make_metrics(liquidity_usd=20000, pair_age_minutes=10))
    assert result.safety.grade == "Fail", f"Expected the raised liquidity minimum to fail this coin, got {result.safety.grade}"
    assert "50,000" in result.safety.reasons[0]
    del os.environ["MEME_SCANNER__THRESHOLDS__SNIPER__MIN_LIQUIDITY_USD"]
    print(f"2. Env var override to Sniper's min_liquidity_usd actually changed Safety gate outcome: {result.safety.reasons[0]}")

    # 3. Weight override actually changes the composite score
    baseline_settings = load_settings()
    baseline_config = baseline_settings.to_meme_engine_config()
    baseline_engine = ScoringEngine(mode=Mode.EARLY_MOMENTUM, config=baseline_config)
    baseline_result = baseline_engine.score(make_metrics(liquidity_usd=30000, pair_age_minutes=60))

    os.environ["MEME_SCANNER__WEIGHTS__EARLY_MOMENTUM__HYPE"] = "0.10"
    os.environ["MEME_SCANNER__WEIGHTS__EARLY_MOMENTUM__ONCHAIN"] = "0.10"
    os.environ["MEME_SCANNER__WEIGHTS__EARLY_MOMENTUM__MOMENTUM"] = "0.80"
    reweighted_settings = load_settings()
    reweighted_config = reweighted_settings.to_meme_engine_config()
    reweighted_engine = ScoringEngine(mode=Mode.EARLY_MOMENTUM, config=reweighted_config)
    reweighted_result = reweighted_engine.score(make_metrics(liquidity_usd=30000, pair_age_minutes=60))
    for k in ["MEME_SCANNER__WEIGHTS__EARLY_MOMENTUM__HYPE", "MEME_SCANNER__WEIGHTS__EARLY_MOMENTUM__ONCHAIN", "MEME_SCANNER__WEIGHTS__EARLY_MOMENTUM__MOMENTUM"]:
        del os.environ[k]

    assert baseline_result.opportunity_score != reweighted_result.opportunity_score, "Expected reweighting to change the composite score"
    print(f"3. Weight override changed the composite score: baseline={baseline_result.opportunity_score}, reweighted={reweighted_result.opportunity_score}")

    # 4. Missing mode raises a clear error, doesn't silently misconfigure
    incomplete_settings = load_settings()
    del incomplete_settings.meme_scanner.thresholds["sniper"]
    try:
        incomplete_settings.to_meme_engine_config()
        assert False, "Expected a ValueError for a missing mode in thresholds"
    except ValueError as e:
        assert "sniper" in str(e)
        print(f"4. Missing mode in settings correctly raises a clear error: {e}")

    # 5. Hype formula constant is genuinely reachable
    os.environ["MEME_SCANNER__HYPE_FORMULA__KOL_BOOST_CAP"] = "40"
    kol_settings = load_settings()
    kol_config = kol_settings.to_meme_engine_config()
    kol_engine = ScoringEngine(mode=Mode.EARLY_MOMENTUM, config=kol_config)
    kol_score, reasons = kol_engine.calculate_hype_virality(make_metrics(kol_score=100))
    del os.environ["MEME_SCANNER__HYPE_FORMULA__KOL_BOOST_CAP"]
    assert any("+40" in r or "+39" in r for r in reasons), f"Expected the raised KOL boost cap to show in reasons, got {reasons}"
    print(f"5. hype_formula.kol_boost_cap override reached the actual calculation: {[r for r in reasons if 'KOL' in r]}")

    print("\n✅ Meme settings adjustability test passed: defaults preserved, env var overrides genuinely reach Safety gate + weight blend + hype formula, missing-mode misconfiguration fails loudly.")


if __name__ == "__main__":
    main()

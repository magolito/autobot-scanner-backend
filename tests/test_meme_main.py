"""
Meme scanner CLI test — filter logic and discovery, synthetic data, no
live network needed.

Checks:
  1. Safety=Fail is ALWAYS excluded, even with a high opportunity_score
  2. Score below threshold is excluded even with Safety=Pass
  3. Caution-grade respects the show_caution_grade toggle
  4. Results are sorted by opportunity_score descending
  5. discover_candidates combines watchlist + boosted feed, de-duplicated
  6. discover_candidates respects max_candidates_per_scan
  7. render_results doesn't crash on empty results or populated results
"""

from __future__ import annotations
import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.meme_main import filter_high_quality, discover_candidates, render_results
from opportunity_scanner.meme_scoring_engine import FinalMemeResult, SafetyResult, PillarScores, Mode, RiskFlag, HypeEvent
from opportunity_scanner.settings import load_settings
from opportunity_scanner.data_sources.dexscreener import DexScreenerProvider


def make_result(symbol, safety_grade, score, hype_level="Medium") -> FinalMemeResult:
    pillar_scores = PillarScores(hype=60, onchain_health=60, momentum=60) if score is not None else None
    return FinalMemeResult(
        symbol=symbol, token_address=f"fake_{symbol}", mode="early_momentum",
        safety=SafetyResult(grade=safety_grade, reasons=[]),
        opportunity_score=score, hype_level=hype_level if score is not None else None,
        pillar_scores=pillar_scores, confidence=75.0,
        risk_flags=[RiskFlag(label="Test risk flag", severity="warning")],  # non-empty — actually exercises the rendering code path
        hype_events=[HypeEvent(label="Test hype event", severity="notable")],
        thesis="test thesis",
    )


def main():
    settings = load_settings()
    settings.meme_scanner.min_opportunity_score_to_show = 60.0
    settings.meme_scanner.show_caution_grade = True

    # 1. Fail always excluded, even with a suspiciously high score
    results = [
        make_result("SNEAKYFAIL", "Fail", 95.0),
        make_result("GOODPASS", "Pass", 85.0),
    ]
    filtered = filter_high_quality(results, settings)
    symbols = [r.symbol for r in filtered]
    assert "SNEAKYFAIL" not in symbols, "Fail-grade result leaked through the filter despite a high score"
    assert "GOODPASS" in symbols
    print("1. Safety=Fail always excluded, regardless of score: OK")

    # 2. Below threshold excluded even with Pass
    results2 = [make_result("LOWSCORE", "Pass", 45.0), make_result("HIGHSCORE", "Pass", 75.0)]
    filtered2 = filter_high_quality(results2, settings)
    symbols2 = [r.symbol for r in filtered2]
    assert "LOWSCORE" not in symbols2
    assert "HIGHSCORE" in symbols2
    print("2. Below-threshold score correctly excluded: OK")

    # 3. Caution toggle
    results3 = [make_result("CAUTIONCOIN", "Caution", 70.0)]
    settings.meme_scanner.show_caution_grade = True
    assert len(filter_high_quality(results3, settings)) == 1
    settings.meme_scanner.show_caution_grade = False
    assert len(filter_high_quality(results3, settings)) == 0
    settings.meme_scanner.show_caution_grade = True
    print("3. show_caution_grade toggle correctly includes/excludes Caution results: OK")

    # 4. Sorted descending by score
    results4 = [make_result("MID", "Pass", 70.0), make_result("TOP", "Pass", 90.0), make_result("LOW", "Pass", 61.0)]
    filtered4 = filter_high_quality(results4, settings)
    scores = [r.opportunity_score for r in filtered4]
    assert scores == sorted(scores, reverse=True), f"Expected descending order, got {scores}"
    print(f"4. Results sorted descending by opportunity_score: {[r.symbol for r in filtered4]}")

    # 5 & 6. Discovery: watchlist + boosted, deduplicated, capped
    async def discovery_test():
        settings5 = load_settings()
        settings5.meme_scanner.discovery.watchlist = ["addr_A", "addr_B"]
        settings5.meme_scanner.discovery.max_candidates_per_scan = 3
        dex = DexScreenerProvider()
        dex.get_boosted_token_addresses = lambda chain_id="solana": asyncio.sleep(0, result=["addr_B", "addr_C", "addr_D"])
        candidates = await discover_candidates(settings5, dex)
        print(f"5. Discovered candidates (watchlist + boosted, deduped, capped at 3): {candidates}")
        assert candidates == ["addr_A", "addr_B", "addr_C"], f"Expected dedup + cap to give [A, B, C], got {candidates}"
        await dex.close()

    asyncio.run(discovery_test())
    print("5-6. Discovery correctly combines watchlist + boosted feed, dedupes, and respects the cap: OK")

    # 7. render_results doesn't crash — empty and populated
    render_results([], Mode.EARLY_MOMENTUM, total_scanned=5)
    render_results(filtered4, Mode.EARLY_MOMENTUM, total_scanned=10)
    print("7. render_results runs without exceptions on both empty and populated result sets: OK")

    print("\n✅ Meme scanner CLI test passed: high-quality filter (Fail-exclusion, threshold, Caution toggle, sort order), discovery combination/dedup/cap, and rendering all verified.")


if __name__ == "__main__":
    main()

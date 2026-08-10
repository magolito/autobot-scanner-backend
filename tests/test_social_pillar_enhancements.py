"""
Social pillar enhancement test — covers the real bug fix (mindshare's
growth signal was using fields the data source never populated) plus
every new metric this session added: social_dominance, spike detection,
and the narrative signal.
"""

from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.models import MarketSnapshot
from opportunity_scanner.factors.social import compute_social, _mindshare_score, _spike_detection, _narrative_signal


def make_snapshot(social: dict) -> MarketSnapshot:
    return MarketSnapshot(symbol="TEST/USDT", base="TEST", price=1.0, social=social)


def main():
    # 1. THE BUG FIX: mindshare growth now uses galaxy_score_previous (real field),
    # not galaxy_score_7d_ago (never populated — this sub-signal silently always failed before)
    score_with_growth, note = _mindshare_score({"galaxy_score": 70, "galaxy_score_previous": 55, "alt_rank": 50})
    score_without_growth, note2 = _mindshare_score({"galaxy_score": 70, "alt_rank": 50})
    assert score_with_growth != score_without_growth, "galaxy_score_previous should genuinely change the result now"
    assert "growing" in note
    print(f"1. Mindshare growth signal now genuinely activates via galaxy_score_previous (the real field) — {score_with_growth:.1f} vs {score_without_growth:.1f} without it: OK")

    # 2. social_dominance contributes to mindshare
    score_no_dominance, _ = _mindshare_score({"galaxy_score": 60})
    score_with_dominance, note3 = _mindshare_score({"galaxy_score": 60, "social_dominance": 8.0})
    assert score_with_dominance != score_no_dominance
    assert "dominance" in note3
    print(f"2. social_dominance (a real LunarCrush field, previously unused) now contributes to mindshare: OK")

    # 3. Spike detection: high velocity + still-climbing recent points -> True
    is_spike, spike_note = _spike_detection({"recent_volume_points": [100, 150, 220, 310]}, velocity_score=80)
    assert is_spike is True
    assert "climbing" in spike_note
    print(f"3. Spike correctly detected: still-climbing recent points + high velocity: '{spike_note}': OK")

    # 4. Spike detection: high velocity but recent points already flattened -> False
    is_spike2, _ = _spike_detection({"recent_volume_points": [300, 310, 290, 295]}, velocity_score=80)
    assert is_spike2 is False, "Elevated-but-flat recent points should NOT count as a spike, even with high velocity"
    print("4. High velocity but flattened recent points correctly NOT flagged as a spike (distinguishes 'elevated' from 'still accelerating'): OK")

    # 5. Spike detection: low velocity never spikes regardless of recent points shape
    is_spike3, _ = _spike_detection({"recent_volume_points": [10, 20, 30, 40]}, velocity_score=40)
    assert is_spike3 is False
    print("5. Low velocity never flagged as a spike, even with climbing recent points: OK")

    # 6. Narrative signal: Heating requires spike AND non-negative sentiment
    assert _narrative_signal(velocity_score=85, sentiment_score=60, mindshare_score=70, is_spike=True) == "🔥 Heating"
    assert _narrative_signal(velocity_score=85, sentiment_score=10, mindshare_score=70, is_spike=True) != "🔥 Heating", \
        "A spike with deeply negative sentiment shouldn't be labeled Heating"
    print("6. Narrative 'Heating' correctly requires both a spike AND non-negative sentiment, not spike alone: OK")

    # 7. Narrative signal: Building, Cooling, Neutral
    assert _narrative_signal(velocity_score=70, sentiment_score=50, mindshare_score=60, is_spike=False) == "📈 Building"
    assert _narrative_signal(velocity_score=20, sentiment_score=50, mindshare_score=30, is_spike=False) == "❄️ Cooling"
    assert _narrative_signal(velocity_score=50, sentiment_score=50, mindshare_score=50, is_spike=False) == "Neutral"
    print("7. Building / Cooling / Neutral narrative labels correctly derived from sub-scores: OK")

    # 8. Full compute_social integration — rich metrics exposed via .raw for dashboard consumption
    social = {
        "galaxy_score": 75, "galaxy_score_previous": 60, "alt_rank": 40, "social_dominance": 4.5,
        "social_volume_24h": 5000, "social_volume_baseline": 2000, "recent_volume_points": [1800, 2200, 3500, 5000],
        "sentiment": 68, "sentiment_prev": 55, "interactions_24h": 40000, "interactions_baseline": 15000,
    }
    result = compute_social(make_snapshot(social))
    assert result.available is True
    assert "narrative_signal" in result.raw
    assert "is_spike" in result.raw
    assert "social_dominance" in result.raw
    assert result.raw["galaxy_score"] == 75
    print(f"8. Full compute_social() exposes all rich metrics via .raw for the dashboard: narrative='{result.raw['narrative_signal']}', spike={result.raw['is_spike']}, score={result.score}: OK")

    # 9. Still gracefully unavailable when snap.social is None entirely (unchanged behavior)
    result_none = compute_social(make_snapshot(None))
    assert result_none.available is False
    print("9. snap.social=None still correctly degrades to available=False (unchanged, no regression): OK")

    print("\n✅ Social pillar enhancement test passed: the dead-field bug is fixed, social_dominance is wired in, spike detection correctly distinguishes 'still accelerating' from 'merely elevated', and the narrative signal combines everything into one clear label.")


if __name__ == "__main__":
    main()

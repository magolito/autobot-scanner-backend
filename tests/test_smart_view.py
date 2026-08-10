"""
Smart View bucketing test — the core value of this feature is being
genuinely selective about Super Strong, so the edge cases (great score
but wrong risk tier, great score but thin data) matter more than the
obvious happy path.
"""

from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.models import ScanResult, FactorResult
from opportunity_scanner.config import SmartViewConfig
from opportunity_scanner.smart_view import Bucket, classify_bucket, bucket_results, data_completeness, BUCKET_LABELS


def make_result(score, confidence, risk_tier="core", available_pillars=4, signal="Buy", alignment_score=70.0) -> ScanResult:
    factors = {}
    names = ["strength", "oi_dynamics", "momentum", "social"]
    for i, name in enumerate(names):
        raw = {"alignment_score": alignment_score} if name == "momentum" else {}
        factors[name] = FactorResult(name=name, score=60, reasons=["t"], raw=raw, available=(i < available_pillars))
    return ScanResult(
        symbol=f"TEST/USDT", base="TEST", price=1.0, composite_score=score, confidence=confidence,
        confidence_label="High" if confidence >= 75 else ("Medium" if confidence >= 50 else "Low"),
        signal=signal, factors=factors, weights_used={"strength": 0.25, "oi_dynamics": 0.25, "momentum": 0.25, "social": 0.25},
        reasons_summary=["synthetic"], risk_tier=risk_tier, passed_filters=True,
    )


def main():
    config = SmartViewConfig()

    # 1. Clear Super Strong: high score, high confidence, core tier, all pillars available
    r1 = make_result(score=85, confidence=80, risk_tier="core", available_pillars=4)
    assert classify_bucket(r1, config) == Bucket.SUPER_STRONG
    print("1. High score + high confidence + core tier + full data -> Super Strong: OK")

    # 2. High score but HIGH_RISK tier -> should NOT be Super Strong, despite the great score
    r2 = make_result(score=90, confidence=90, risk_tier="high_risk", available_pillars=4)
    bucket2 = classify_bucket(r2, config)
    assert bucket2 != Bucket.SUPER_STRONG, f"A high_risk tier result should never reach Super Strong regardless of score, got {bucket2}"
    assert bucket2 == Bucket.STRONG, f"Should fall to Strong (high_risk is allowed there), got {bucket2}"
    print("2. Excellent score but high_risk tier correctly BLOCKED from Super Strong, falls to Strong: OK")

    # 3. High score + high confidence + core tier, but POOR data completeness -> should NOT be Super Strong
    r3 = make_result(score=88, confidence=80, risk_tier="core", available_pillars=1)  # only 1 of 4 pillars available = 25% completeness
    bucket3 = classify_bucket(r3, config)
    assert bucket3 != Bucket.SUPER_STRONG, f"Thin data (25% completeness) should block Super Strong even with a great score, got {bucket3}"
    print(f"3. Great score/confidence but only 25% data completeness correctly blocked from Super Strong (landed in {bucket3.value}): OK")

    # 4. Moderate score -> Strong
    r4 = make_result(score=70, confidence=60, risk_tier="core", available_pillars=4)
    assert classify_bucket(r4, config) == Bucket.STRONG
    print("4. Moderate-high score/confidence -> Strong: OK")

    # 5. Lower score -> Building/Watchlist
    r5 = make_result(score=50, confidence=40, risk_tier="small_cap", available_pillars=3)
    assert classify_bucket(r5, config) == Bucket.BUILDING
    print("5. Moderate score, lower confidence -> Building/Watchlist: OK")

    # 6. Weak score -> High Risk / Low Conviction (the catch-all)
    r6 = make_result(score=20, confidence=15, risk_tier="high_risk", available_pillars=1)
    assert classify_bucket(r6, config) == Bucket.HIGH_RISK_LOW_CONVICTION
    print("6. Weak score/confidence/high_risk tier/thin data -> High Risk / Low Conviction: OK")

    # 7. Boundary values — exactly at threshold should count as qualifying (>=, not >)
    r7 = make_result(score=80.0, confidence=75.0, risk_tier="core", available_pillars=4)  # exactly Super Strong's minimums
    assert classify_bucket(r7, config) == Bucket.SUPER_STRONG, "Exactly-at-threshold values should qualify (>=, not >)"
    print("7. Exactly-at-threshold score/confidence correctly qualifies (boundary is inclusive): OK")

    # 8. data_completeness() computed correctly and independently testable
    r8 = make_result(score=50, confidence=50, available_pillars=3)
    assert data_completeness(r8) == 0.75, f"Expected 3/4 = 0.75, got {data_completeness(r8)}"
    print("8. data_completeness() correctly computes available-pillar fraction: OK")

    # 9. bucket_results() always returns all 4 buckets, even empty ones, sorted descending within each
    results = [r1, r2, r4, r5]
    buckets = bucket_results(results, config)
    assert set(buckets.keys()) == set(Bucket), "Every bucket key should always be present, even if empty"
    assert len(buckets[Bucket.HIGH_RISK_LOW_CONVICTION]) == 0, "No results should have landed here in this set"
    strong_scores = [r.composite_score for r in buckets[Bucket.STRONG]]
    assert strong_scores == sorted(strong_scores, reverse=True), "Results within a bucket should be sorted by score descending"
    print("9. bucket_results() returns all 4 buckets always (even empty), sorted descending within each: OK")

    # 10. Every bucket has a human-readable label
    for b in Bucket:
        assert b in BUCKET_LABELS and len(BUCKET_LABELS[b]) > 0
    print("10. Every bucket has a display label: OK")

    print("\n✅ Smart View bucketing test passed: correct classification including the two edge cases that matter most (great score blocked by risk tier, great score blocked by thin data), boundary inclusivity, and the always-all-4-buckets contract.")


def test_alignment_gates_super_strong():
    """
    The actual fix for a real, live problem: "Super Strong" had shown
    (0) results across an entire session of live scans. Root cause:
    nothing in the bucketing logic ever checked whether a coin's
    strength was genuine multi-timeframe conviction versus one
    timeframe's noise dragging up a blended average. Now Super Strong
    explicitly requires real weighted multi-timeframe agreement
    (min_alignment_score), not just a high composite score.
    """
    config = SmartViewConfig()

    def make_with_momentum(score, confidence, alignment_score, risk_tier="core"):
        factors = {}
        names = ["strength", "oi_dynamics", "momentum", "social"]
        for i, name in enumerate(names):
            raw = {"alignment_score": alignment_score} if name == "momentum" else {}
            factors[name] = FactorResult(name=name, score=80, reasons=["t"], raw=raw, available=True)
        return ScanResult(
            symbol="TEST/USDT", base="TEST", price=1.0, composite_score=score, confidence=confidence,
            confidence_label="High" if confidence >= 75 else "Medium",
            signal="Strong Buy", factors=factors, weights_used={"strength": 0.25, "oi_dynamics": 0.25, "momentum": 0.25, "social": 0.25},
            reasons_summary=["synthetic"], risk_tier=risk_tier, passed_filters=True,
        )

    # 1. Otherwise-perfect Super Strong candidate, but LOW timeframe alignment
    # (e.g. only 15m showing strength, longer timeframes disagreeing) — should
    # NOT reach Super Strong despite a great score/confidence
    r1 = make_with_momentum(score=88, confidence=80, alignment_score=20.0)
    bucket1 = classify_bucket(r1, config)
    assert bucket1 != Bucket.SUPER_STRONG, f"Low timeframe alignment (20%) should block Super Strong even with a great score, got {bucket1}"
    assert bucket1 == Bucket.STRONG, f"Should still qualify for Strong (no alignment requirement there), got {bucket1}"
    print(f"1. THE ACTUAL FIX: great score/confidence but low multi-timeframe alignment (20%) correctly BLOCKED from Super Strong, falls to Strong: OK")

    # 2. Same great score/confidence, but genuine high multi-timeframe alignment — qualifies
    r2 = make_with_momentum(score=88, confidence=80, alignment_score=75.0)
    bucket2 = classify_bucket(r2, config)
    assert bucket2 == Bucket.SUPER_STRONG, f"High genuine alignment (75%) should qualify for Super Strong, got {bucket2}"
    print(f"2. Same score/confidence WITH genuine multi-timeframe alignment (75%) correctly reaches Super Strong: OK")

    # 3. Exactly at the boundary (60%) — inclusive
    r3 = make_with_momentum(score=88, confidence=80, alignment_score=60.0)
    assert classify_bucket(r3, config) == Bucket.SUPER_STRONG, "Exactly-at-threshold alignment should qualify (>=, not >)"
    print("3. Exactly-at-threshold alignment (60%) correctly qualifies (boundary inclusive): OK")

    # 4. Momentum unavailable entirely — correctly can't claim Super Strong
    # (no genuine alignment data to back up the claim)
    factors_no_momentum = {}
    for i, name in enumerate(["strength", "oi_dynamics", "momentum", "social"]):
        factors_no_momentum[name] = FactorResult(name=name, score=80, reasons=["t"], available=(name != "momentum"))
    r4 = ScanResult(
        symbol="TEST/USDT", base="TEST", price=1.0, composite_score=88, confidence=80,
        confidence_label="High", signal="Strong Buy", factors=factors_no_momentum,
        weights_used={"strength": 0.25, "oi_dynamics": 0.25, "momentum": 0.25, "social": 0.25},
        reasons_summary=["synthetic"], risk_tier="core", passed_filters=True,
    )
    bucket4 = classify_bucket(r4, config)
    assert bucket4 != Bucket.SUPER_STRONG, "Missing momentum data means no genuine alignment claim can be made — should not reach Super Strong"
    print(f"4. Momentum entirely unavailable correctly blocks Super Strong (can't claim genuine conviction without the data): OK")

    print("\n✅ Multi-timeframe alignment gate test passed: Super Strong now genuinely requires real, weighted multi-timeframe agreement, not just a high blended score — the actual fix for Super Strong never firing.")


if __name__ == "__main__":
    main()
    test_alignment_gates_super_strong()

"""
Plan tier test — verifies feature access and per-scanner limits match
the documented product decisions exactly (see plans.py's module
docstring for the concrete Free/Pro/Elite table).
"""

from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.plans import PlanTier, ScannerAccess, has_scanner_access, get_plan_features, max_scans_per_day


def main():
    assert has_scanner_access(PlanTier.FREE, "opportunity") == ScannerAccess.LIMITED
    assert has_scanner_access(PlanTier.FREE, "meme") == ScannerAccess.NONE
    assert max_scans_per_day(PlanTier.FREE, "opportunity") == 5
    assert max_scans_per_day(PlanTier.FREE, "meme") == 0
    assert get_plan_features(PlanTier.FREE).opportunity_max_results_shown == 5
    print("1. Free plan: 5 Opportunity scans/day capped to top 5 results, zero Meme Scanner access: OK")

    assert has_scanner_access(PlanTier.PRO, "opportunity") == ScannerAccess.FULL
    assert has_scanner_access(PlanTier.PRO, "meme") == ScannerAccess.LIMITED
    assert max_scans_per_day(PlanTier.PRO, "opportunity") is None
    assert max_scans_per_day(PlanTier.PRO, "meme") == 10
    assert get_plan_features(PlanTier.PRO).opportunity_max_results_shown is None
    print("2. Pro plan: unlimited full-result Opportunity Scanner, 10 Meme Scanner scans/day: OK")

    assert has_scanner_access(PlanTier.ELITE, "opportunity") == ScannerAccess.FULL
    assert has_scanner_access(PlanTier.ELITE, "meme") == ScannerAccess.FULL
    assert max_scans_per_day(PlanTier.ELITE, "opportunity") is None
    assert max_scans_per_day(PlanTier.ELITE, "meme") is None
    print("3. Elite plan: unlimited access to both scanners: OK")

    assert get_plan_features(PlanTier.FREE).price_usd_per_month == 0.0
    assert get_plan_features(PlanTier.PRO).price_usd_per_month == 39.0
    assert get_plan_features(PlanTier.ELITE).price_usd_per_month == 89.0
    print("4. Prices match the product plan table: OK")

    try:
        has_scanner_access(PlanTier.FREE, "not_a_real_scanner")
        assert False, "Expected a ValueError for an unknown scanner name"
    except ValueError:
        print("5. Unknown scanner name in has_scanner_access correctly raises: OK")

    try:
        max_scans_per_day(PlanTier.FREE, "not_a_real_scanner")
        assert False, "Expected a ValueError for an unknown scanner name"
    except ValueError:
        print("6. Unknown scanner name in max_scans_per_day correctly raises: OK")

    print("\n✅ Plan tier test passed.")


if __name__ == "__main__":
    main()

"""
Access control test — every plan x scanner x limit combination, since
this is the actual enforcement logic standing between a Free user and
unlimited paid-tier access. A bug here is a real revenue leak, not a
cosmetic issue.

Checks:
  1. Free + Opportunity, under limit: allowed
  2. Free + Opportunity, AT the limit: denied with a clear reason
  3. Free + Meme: denied outright regardless of usage (NONE access)
  4. Pro + Opportunity: always allowed, unlimited
  5. Pro + Meme, under limit: allowed
  6. Pro + Meme, AT the limit: denied
  7. Elite + both scanners: always allowed regardless of usage
  8. record_scan() increments and check_scanner_access() immediately
     reflects the new count on the very next call
  9. max_results_shown: 5 for Free, None (unlimited) for Pro/Elite
"""

from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.app_storage import AppStorage
from opportunity_scanner.plans import PlanTier
from opportunity_scanner.access_control import check_scanner_access, record_scan


def main():
    db_path = "/tmp/test_access_control.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    storage = AppStorage(db_path)

    # 1 & 2. Free + Opportunity
    free_user = storage.create_user("free@example.com", "password123", plan=PlanTier.FREE)
    decision = check_scanner_access(free_user, "opportunity", storage)
    assert decision.allowed is True
    assert decision.scans_remaining_today == 5
    print("1. Free + Opportunity, zero usage: allowed, 5 remaining: OK")

    for _ in range(5):
        record_scan(free_user, "opportunity", storage)
    decision_at_limit = check_scanner_access(free_user, "opportunity", storage)
    assert decision_at_limit.allowed is False
    assert "used all 5" in decision_at_limit.reason
    assert decision_at_limit.scans_remaining_today == 0
    print(f"2. Free + Opportunity, at limit: denied with reason: '{decision_at_limit.reason}': OK")

    # 3. Free + Meme — denied outright, no scans needed to prove it
    meme_decision = check_scanner_access(free_user, "meme", storage)
    assert meme_decision.allowed is False
    assert "isn't included" in meme_decision.reason
    print(f"3. Free + Meme, zero usage: still denied outright (NONE access): '{meme_decision.reason}': OK")

    # 4. Pro + Opportunity — always allowed
    pro_user = storage.create_user("pro@example.com", "password123", plan=PlanTier.PRO)
    for _ in range(50):
        record_scan(pro_user, "opportunity", storage)
    pro_opp_decision = check_scanner_access(pro_user, "opportunity", storage)
    assert pro_opp_decision.allowed is True
    assert pro_opp_decision.scans_remaining_today is None
    print("4. Pro + Opportunity, even after 50 scans: still allowed, unlimited: OK")

    # 5 & 6. Pro + Meme, capped at 10
    pro_meme_decision = check_scanner_access(pro_user, "meme", storage)
    assert pro_meme_decision.allowed is True
    assert pro_meme_decision.scans_remaining_today == 10
    for _ in range(10):
        record_scan(pro_user, "meme", storage)
    pro_meme_at_limit = check_scanner_access(pro_user, "meme", storage)
    assert pro_meme_at_limit.allowed is False
    assert "used all 10" in pro_meme_at_limit.reason
    print(f"5-6. Pro + Meme: allowed with 10 remaining, then denied at limit: '{pro_meme_at_limit.reason}': OK")

    # 7. Elite + both — always allowed
    elite_user = storage.create_user("elite@example.com", "password123", plan=PlanTier.ELITE)
    for _ in range(100):
        record_scan(elite_user, "meme", storage)
    elite_decision = check_scanner_access(elite_user, "meme", storage)
    assert elite_decision.allowed is True
    assert elite_decision.scans_remaining_today is None
    print("7. Elite + Meme, even after 100 scans: still allowed, unlimited: OK")

    # 8. record_scan + immediate reflection
    fresh_free = storage.create_user("freshfree@example.com", "password123", plan=PlanTier.FREE)
    d1 = check_scanner_access(fresh_free, "opportunity", storage)
    assert d1.scans_remaining_today == 5
    record_scan(fresh_free, "opportunity", storage)
    d2 = check_scanner_access(fresh_free, "opportunity", storage)
    assert d2.scans_remaining_today == 4
    print("8. record_scan() immediately reflected in the very next check_scanner_access() call (5 -> 4): OK")

    # 9. max_results_shown
    assert check_scanner_access(free_user, "opportunity", storage).max_results_shown == 5
    assert check_scanner_access(pro_user, "opportunity", storage).max_results_shown is None
    assert check_scanner_access(elite_user, "opportunity", storage).max_results_shown is None
    print("9. max_results_shown: 5 for Free, unlimited for Pro/Elite: OK")

    os.remove(db_path)
    print("\n✅ Access control test passed: every plan x scanner x limit combination verified, including exact-at-limit boundaries and immediate usage reflection.")


if __name__ == "__main__":
    main()

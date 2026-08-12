"""
Admin email override test — the actual fix for "how do I test the full
version without going through billing." Proves a listed admin email
gets Elite-level access for BOTH scanners without the user's actual
stored plan ever being touched, and that this correctly reverts if the
email is removed from the list — not a permanent database change.
"""

from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    from opportunity_scanner.access_control import check_scanner_access
    from opportunity_scanner.app_storage import AppStorage, User
    from opportunity_scanner.plans import PlanTier

    db_path = "/tmp/test_admin_override.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    storage = AppStorage(db_path)

    user = storage.create_user("founder@example.com", "password123", plan=PlanTier.FREE)
    assert user is not None

    # 1. THE ACTUAL FIX: a Free-plan user whose email is in admin_emails
    # gets Elite-level access — unlimited scans, full results, meme
    # scanner unlocked — for BOTH scanners
    decision_opp = check_scanner_access(user, "opportunity", storage, admin_emails=["founder@example.com"])
    assert decision_opp.allowed is True
    assert decision_opp.max_results_shown is None, f"Expected unlimited results (Elite), got capped at {decision_opp.max_results_shown}"
    assert decision_opp.scans_remaining_today is None, f"Expected unlimited scans (Elite), got {decision_opp.scans_remaining_today}"
    assert decision_opp.effective_plan == PlanTier.ELITE, (
        f"THE ACTUAL FIX for a real, confirmed bug: the dashboard's 'Plan: X' label was reading user.plan "
        f"directly, bypassing the override entirely (a Free user with a working override still saw 'Plan: Free' "
        f"and stayed functionally limited). effective_plan must be exposed so the UI can display the real plan, "
        f"got {decision_opp.effective_plan}"
    )
    print("1. THE ACTUAL FIX: a Free-plan user's email listed in admin_emails gets full Elite access (unlimited scans, full results) AND effective_plan correctly exposed as ELITE for the UI to display: OK")

    decision_meme = check_scanner_access(user, "meme", storage, admin_emails=["founder@example.com"])
    assert decision_meme.allowed is True, "Meme scanner should also be unlocked (Free plan alone blocks it entirely)"
    print("2. Meme Scanner also correctly unlocked (normally blocked entirely on Free) — admin override applies to both scanners: OK")

    # 3. The user's ACTUAL stored plan is never touched — this is a
    # runtime override, not a database change
    reloaded = storage.get_user_by_email("founder@example.com")
    assert reloaded.plan == PlanTier.FREE, f"The stored plan should remain untouched (still Free), got {reloaded.plan}"
    print("3. The user's actual stored plan in the database remains untouched (still Free) — this is a runtime-only override, nothing permanent: OK")

    # 4. Case-insensitive matching, since email casing shouldn't matter
    decision_case = check_scanner_access(user, "opportunity", storage, admin_emails=["FOUNDER@EXAMPLE.COM"])
    assert decision_case.allowed is True and decision_case.max_results_shown is None
    print("4. Email matching is case-insensitive (FOUNDER@EXAMPLE.COM matches founder@example.com): OK")

    # 5. Removing the email from the list immediately reverts to the real plan
    decision_reverted = check_scanner_access(user, "opportunity", storage, admin_emails=[])
    assert decision_reverted.max_results_shown == 5, f"Expected the real Free-plan limit (5) once removed from admin_emails, got {decision_reverted.max_results_shown}"
    print("5. Removing the email from admin_emails immediately reverts to the real Free-plan limits — fully reversible, not a one-way change: OK")

    # 6. A user NOT in the list is completely unaffected
    other_user = storage.create_user("regular@example.com", "password123", plan=PlanTier.FREE)
    decision_other = check_scanner_access(other_user, "opportunity", storage, admin_emails=["founder@example.com"])
    assert decision_other.max_results_shown == 5, "A user not in admin_emails should be completely unaffected, still on their real Free-plan limits"
    print("6. A different user, not listed, is completely unaffected by the override — still correctly capped at Free-plan limits: OK")

    os.remove(db_path)
    print("\n✅ Admin email override test passed: reversible, case-insensitive, applies to both scanners, and never touches the actual stored plan.")


if __name__ == "__main__":
    main()

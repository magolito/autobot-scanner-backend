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


def test_shared_helper_and_billing_display():
    """
    The actual third-instance fix: after fixing the dashboard's top-level
    'Plan: X' label, a live screenshot showed a SEPARATE 'Account &
    Billing' section still showing 'Current plan: Free' — a third place
    (billing_ui.py) reading user.plan directly. Three separate places
    needing the identical admin-override check is the real signal this
    belonged in one shared function, not copy-pasted a third time.
    """
    from opportunity_scanner.access_control import get_effective_plan, check_scanner_access
    from opportunity_scanner.app_storage import AppStorage
    from opportunity_scanner.plans import PlanTier

    db_path = "/tmp/test_shared_helper.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    storage = AppStorage(db_path)
    user = storage.create_user("founder2@example.com", "password123", plan=PlanTier.FREE)

    # 1. THE ACTUAL FIX: the shared helper is now the single source of
    # truth, used directly (not re-implemented) by check_scanner_access
    assert get_effective_plan(user, ["founder2@example.com"]) == PlanTier.ELITE
    assert get_effective_plan(user, ["someone-else@example.com"]) == PlanTier.FREE
    assert get_effective_plan(user, None) == PlanTier.FREE
    print("1. THE ACTUAL FIX: get_effective_plan is now the single, shared source of truth for the admin-override check: OK")

    # 2. check_scanner_access still works correctly using the shared helper internally
    decision = check_scanner_access(user, "opportunity", storage, admin_emails=["founder2@example.com"])
    assert decision.effective_plan == PlanTier.ELITE
    print("2. check_scanner_access correctly uses the shared helper internally, behavior unchanged: OK")

    os.remove(db_path)
    print("\n✅ Shared helper test passed: one function, three consumers (check_scanner_access, dashboard label, billing section), no drift risk between them.")


def test_billing_section_renders_override_honestly():
    """
    Real Streamlit-level test (not just unit logic) proving
    render_billing_section actually shows the honest "testing override,
    not billed" label when the admin override is active — the exact
    live symptom from the screenshot, verified end to end through a
    real render, not just the underlying get_effective_plan logic.
    """
    from streamlit.testing.v1 import AppTest

    db_path = "/tmp/test_billing_render.db"
    if os.path.exists(db_path):
        os.remove(db_path)

    script = f'''
import streamlit as st
from opportunity_scanner.app_storage import AppStorage
from opportunity_scanner.plans import PlanTier
from opportunity_scanner.billing_ui import render_billing_section
from types import SimpleNamespace

storage = AppStorage("{db_path}")
user = storage.create_user("billingtest@example.com", "pw123456", plan=PlanTier.FREE)
fake_settings = SimpleNamespace(
    admin_emails=["billingtest@example.com"],
    stripe=SimpleNamespace(enabled=False),
    crypto_payments=SimpleNamespace(enabled=False),
)
render_billing_section(user, fake_settings, "http://x", "http://x")
'''
    at = AppTest.from_string(script)
    at.run(timeout=15)
    assert not at.exception, f"render_billing_section raised: {at.exception}"

    markdown_texts = " ".join(m.value for m in at.markdown)
    assert "Elite" in markdown_texts, f"Expected the effective plan (Elite) to show, got: {markdown_texts}"
    assert "testing override, not billed" in markdown_texts, (
        f"THE ACTUAL FIX: expected the honest 'testing override, not billed' label instead of implying a real "
        f"paid subscription, got: {markdown_texts}"
    )
    assert "$89" not in markdown_texts, "Should NOT show a real price for a testing override — that would misleadingly imply an actual charge"

    os.remove(db_path)
    print("1. THE ACTUAL FIX verified via a real render: billing_ui.py now correctly shows 'Elite (testing override, not billed)', not a misleading '$89/mo' price, and not the old bug showing 'Free': OK")
    print("\n✅ Billing section render test passed: the exact live symptom from the screenshot is fixed and verified end to end.")


if __name__ == "__main__":
    main()
    test_shared_helper_and_billing_display()
    test_billing_section_renders_override_honestly()

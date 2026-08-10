"""
App storage test — users database, the Stage 1 foundation Stage 2 (login
UI) and Stage 3 (Stripe billing) will build on. Not yet wired into
either dashboard's actual login flow (both still use the shared
DASHBOARD_PASSWORD) — that wiring is Stage 2's job specifically.

Checks:
  1. User creation works, defaults to Free plan, no subscription
  2. Duplicate email returns None (expected outcome), not an exception
  3. Authenticate: correct password succeeds; wrong password and a
     nonexistent email both fail identically (no email-enumeration signal)
  4. Email lookup is case-insensitive
  5. Simulated Stripe webhook: update_subscription changes plan + status
  6. Lookup by Stripe customer ID (the actual pattern a webhook handler needs)
  7. Password change: new password works, old one no longer does
"""

from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.app_storage import AppStorage
from opportunity_scanner.plans import PlanTier


def main():
    db_path = "/tmp/test_app_storage_permanent.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    storage = AppStorage(db_path)

    user = storage.create_user("customer@example.com", "mypassword123")
    assert user is not None
    assert user.plan == PlanTier.FREE
    assert user.subscription_status == "none"
    print("1. User creation defaults to Free plan, no subscription: OK")

    dup = storage.create_user("customer@example.com", "differentpassword")
    assert dup is None
    print("2. Duplicate email correctly returns None, not a crash: OK")

    assert storage.authenticate("customer@example.com", "mypassword123") is not None
    assert storage.authenticate("customer@example.com", "wrongpassword") is None
    assert storage.authenticate("nobody@example.com", "anything") is None
    print("3. Authenticate: correct works, wrong password and nonexistent email both cleanly fail: OK")

    assert storage.authenticate("CUSTOMER@EXAMPLE.COM", "mypassword123") is not None
    print("4. Email lookup is case-insensitive: OK")

    updated = storage.update_subscription(user.id, plan=PlanTier.PRO, stripe_customer_id="cus_fake123", subscription_status="active")
    assert updated.plan == PlanTier.PRO
    assert updated.subscription_status == "active"
    print("5. Simulated Stripe webhook (update_subscription) correctly updates plan + status: OK")

    by_stripe = storage.get_user_by_stripe_customer_id("cus_fake123")
    assert by_stripe is not None and by_stripe.id == user.id
    print("6. Lookup by Stripe customer ID works (the actual webhook handler pattern): OK")

    storage.change_password(user.id, "newpassword456")
    assert storage.authenticate("customer@example.com", "newpassword456") is not None
    assert storage.authenticate("customer@example.com", "mypassword123") is None
    print("7. Password change: new password works, old password no longer does: OK")

    os.remove(db_path)
    print("\n✅ App storage test passed: user registration, auth, duplicate handling, and the Stripe webhook pattern all verified.")


if __name__ == "__main__":
    main()

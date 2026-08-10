"""
Plan refresh test — proves a plan change made by a SEPARATE process (a
Stripe/crypto webhook handled by the API service, not the dashboard)
reflects in the dashboard on the very next interaction, without
requiring the user to log out and back in. Simulates the webhook by
calling AppStorage.update_subscription() directly between two script
reruns of the same AppTest session — exactly what a real webhook does.
"""

from __future__ import annotations
import os
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
DASHBOARD_PATH = os.path.join(PROJECT_ROOT, "opportunity_scanner", "dashboard.py")

APP_DB = "/tmp/test_plan_refresh.db"


def main():
    from streamlit.testing.v1 import AppTest
    from opportunity_scanner.app_storage import AppStorage
    from opportunity_scanner.plans import PlanTier

    os.environ["APP_DB_PATH"] = APP_DB
    if os.path.exists(APP_DB):
        os.remove(APP_DB)

    try:
        at = AppTest.from_file(DASHBOARD_PATH)
        at.run(timeout=20)
        at.text_input[2].set_value("refreshtest@example.com")
        at.text_input[3].set_value("password123")
        at.text_input[4].set_value("password123")
        at.button[1].click().run(timeout=20)
        assert at.session_state["user"].plan == PlanTier.FREE
        print("1. New account starts on Free plan: OK")

        # Simulate a webhook (a completely separate process in reality)
        # upgrading this user to Elite, directly via storage
        storage = AppStorage(APP_DB)
        storage.update_subscription(at.session_state["user"].id, plan=PlanTier.ELITE, subscription_status="active")
        print("2. Simulated a Stripe webhook upgrading the user to Elite (via direct storage update, same as the real webhook handler does): OK")

        # No logout/login — just another interaction, same as clicking anything else in the dashboard
        at.run(timeout=20)
        refreshed_user = at.session_state["user"]
        assert refreshed_user.plan == PlanTier.ELITE, f"Expected the dashboard to pick up the Elite upgrade on the next rerun, still shows {refreshed_user.plan}"
        print("3. Dashboard picked up the plan change on the very next rerun — no logout/login needed: OK")

        print("\n✅ Plan refresh test passed: a webhook-driven plan change reflects immediately in an already-logged-in session.")

    finally:
        del os.environ["APP_DB_PATH"]
        if os.path.exists(APP_DB):
            os.remove(APP_DB)


if __name__ == "__main__":
    main()

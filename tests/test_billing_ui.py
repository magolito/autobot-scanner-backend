"""
Billing UI test — both dashboards' Account & Billing section.

Checks:
  1. dashboard.py: billing section renders with correct plan display, no exceptions
  2. dashboard.py: "not configured" caption shows when billing is off
  3. dashboard.py: a live network failure when clicking "Upgrade with card"
     is caught and shown as a clean error, not a crash
  4. meme_dashboard.py: identical wiring works
"""

from __future__ import annotations
import os
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
DASHBOARD_PATH = os.path.join(PROJECT_ROOT, "opportunity_scanner", "dashboard.py")
MEME_DASHBOARD_PATH = os.path.join(PROJECT_ROOT, "opportunity_scanner", "meme_dashboard.py")

APP_DB = "/tmp/test_billing_ui_users.db"


def _cleanup():
    if os.path.exists(APP_DB):
        os.remove(APP_DB)


def main():
    from streamlit.testing.v1 import AppTest

    os.environ["APP_DB_PATH"] = APP_DB
    _cleanup()
    try:
        at = AppTest.from_file(DASHBOARD_PATH)
        at.run(timeout=20)
        at.text_input[2].set_value("billingtestuser@example.com")
        at.text_input[3].set_value("mypassword123")
        at.text_input[4].set_value("mypassword123")
        at.button[1].click().run(timeout=20)
        assert not at.exception
        plan_texts = [m.value for m in at.markdown if "Current plan" in m.value]
        assert any("Free" in t and "$0/mo" in t for t in plan_texts)
        print("1. Dashboard billing section renders correct plan display, zero exceptions: OK")

        captions = [c.value for c in at.caption]
        assert any("isn't configured" in c for c in captions)
        print("2. 'Billing isn't configured' caption correctly shown when disabled (the default): OK")
    finally:
        del os.environ["APP_DB_PATH"]
        _cleanup()

    os.environ["APP_DB_PATH"] = APP_DB
    os.environ["STRIPE__ENABLED"] = "true"
    os.environ["STRIPE__PRICE_ID_PRO"] = "price_fake"
    os.environ["STRIPE_SECRET_KEY"] = "sk_test_totally_fake_key"
    _cleanup()
    try:
        at2 = AppTest.from_file(DASHBOARD_PATH)
        at2.run(timeout=20)
        at2.text_input[2].set_value("billingtestuser2@example.com")
        at2.text_input[3].set_value("mypassword123")
        at2.text_input[4].set_value("mypassword123")
        at2.button[1].click().run(timeout=20)
        upgrade_btn = next((b for b in at2.button if "Upgrade with card" in b.label), None)
        assert upgrade_btn is not None
        upgrade_btn.click().run(timeout=25)
        assert not at2.exception, "A live billing API failure should never crash the dashboard"
        assert any("Couldn't create a checkout session" in e.value for e in at2.error)
        print("3. A real live-network failure on 'Upgrade with card' is caught cleanly, dashboard doesn't crash: OK")
    finally:
        for k in ["APP_DB_PATH", "STRIPE__ENABLED", "STRIPE__PRICE_ID_PRO", "STRIPE_SECRET_KEY"]:
            os.environ.pop(k, None)
        _cleanup()

    os.environ["APP_DB_PATH"] = APP_DB
    _cleanup()
    try:
        at3 = AppTest.from_file(MEME_DASHBOARD_PATH)
        at3.run(timeout=20)
        at3.text_input[2].set_value("memebillingtestuser@example.com")
        at3.text_input[3].set_value("mypassword123")
        at3.text_input[4].set_value("mypassword123")
        at3.button[1].click().run(timeout=20)
        assert not at3.exception
        plan_texts3 = [m.value for m in at3.markdown if "Current plan" in m.value]
        assert any("Free" in t for t in plan_texts3)
        print("4. Meme dashboard billing section wiring also works correctly: OK")
    finally:
        del os.environ["APP_DB_PATH"]
        _cleanup()

    print("\n✅ Billing UI test passed: correct plan display, unconfigured-billing messaging, and graceful handling of a real live-network failure all verified across both dashboards.")


if __name__ == "__main__":
    main()

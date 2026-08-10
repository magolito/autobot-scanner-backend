"""
Meme dashboard auth wiring test — meme_dashboard.py shares the exact
same auth_ui.py module as dashboard.py (test_dashboard_auth.py covers
the full flow thoroughly there), so this just confirms the wiring point
itself is correct in this second file.
"""

from __future__ import annotations
import os
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
MEME_DASHBOARD_PATH = os.path.join(PROJECT_ROOT, "opportunity_scanner", "meme_dashboard.py")

TEST_APP_DB = "/tmp/test_meme_dashboard_auth_users.db"
TEST_MEME_DB = "/tmp/test_meme_dashboard_auth_scans.db"


def _cleanup():
    for p in (TEST_APP_DB, TEST_MEME_DB):
        if os.path.exists(p):
            os.remove(p)


def main():
    from streamlit.testing.v1 import AppTest
    os.environ["APP_DB_PATH"] = TEST_APP_DB
    os.environ["MEME_SCANNER__DB_PATH"] = TEST_MEME_DB
    _cleanup()

    try:
        at = AppTest.from_file(MEME_DASHBOARD_PATH)
        at.run(timeout=20)
        assert not at.exception
        tab_labels = [t.label for t in at.tabs] if at.tabs else []
        assert "Sign In" in tab_labels and "Create Account" in tab_labels
        print("1. Meme dashboard: unauthenticated state shows both auth tabs: OK")

        at.text_input[2].set_value("memedashuser@example.com")
        at.text_input[3].set_value("mypassword123")
        at.text_input[4].set_value("mypassword123")
        at.button[1].click().run(timeout=20)
        assert not at.exception
        assert at.session_state["user"] is not None
        assert at.session_state["user"].email == "memedashuser@example.com"

        # This test is specifically about auth wiring, not access control
        # (which has its own dedicated test suite — test_meme_dashboard_
        # access_enforcement.py). A fresh account defaults to Free, which
        # is now correctly blocked from the Meme Scanner entirely — upgrade
        # to Elite here so the rest of this test can verify what it's
        # actually meant to verify (Scan Now renders, logout works).
        from opportunity_scanner.app_storage import AppStorage
        from opportunity_scanner.plans import PlanTier
        storage = AppStorage(TEST_APP_DB)
        storage.update_subscription(at.session_state["user"].id, plan=PlanTier.ELITE, subscription_status="active")
        at.run(timeout=20)

        assert any("Scan Now" in b.label for b in at.button)
        print("2. Registration logs in and the meme dashboard's own content renders (Scan Now present, after upgrading to Elite for this auth-focused test): OK")

        logout_btn = next(b for b in at.button if "Sign out" in (b.help or ""))
        logout_btn.click().run(timeout=20)
        assert at.session_state["user"] is None
        print("3. Logout works on the meme dashboard too: OK")

        print("\n✅ Meme dashboard auth wiring test passed.")

    finally:
        del os.environ["APP_DB_PATH"]
        del os.environ["MEME_SCANNER__DB_PATH"]
        _cleanup()


if __name__ == "__main__":
    main()

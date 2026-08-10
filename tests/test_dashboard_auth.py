"""
Auth flow test — dashboard.py's real per-user login/register (Stage 2),
replacing the Stage 1 shared DASHBOARD_PASSWORD. Uses a temp app_users.db
per test run so this never touches a real database.

Checks:
  1. Unauthenticated load shows both Sign In and Create Account tabs, no dashboard content
  2. Registration creates an account and auto-logs in, full dashboard renders after
  3. Wrong password is rejected, user stays unauthenticated
  4. Correct password on a fresh session logs in successfully
  5. Duplicate email registration is rejected with a clear message
  6. Logout clears the session and returns to the login screen
  7. 5 failed login attempts trigger a lockout
"""

from __future__ import annotations
import os
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
DASHBOARD_PATH = os.path.join(PROJECT_ROOT, "opportunity_scanner", "dashboard.py")

TEST_DB = "/tmp/test_dashboard_auth_users.db"


def _fresh_db():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


def main():
    from streamlit.testing.v1 import AppTest
    os.environ["APP_DB_PATH"] = TEST_DB
    _fresh_db()

    try:
        at = AppTest.from_file(DASHBOARD_PATH)
        at.run(timeout=20)
        assert not at.exception
        tab_labels = [t.label for t in at.tabs] if at.tabs else []
        assert "Sign In" in tab_labels and "Create Account" in tab_labels
        assert len(at.slider) == 0, "Dashboard content should not render before authentication"
        print("1. Unauthenticated: both auth tabs present, no dashboard content leaked: OK")

        at.text_input[2].set_value("dashuser@example.com")
        at.text_input[3].set_value("mypassword123")
        at.text_input[4].set_value("mypassword123")
        at.button[1].click().run(timeout=20)
        assert not at.exception
        assert at.session_state["user"] is not None
        assert len(at.slider) == 5, "Expected the full dashboard (5 weight sliders) to render after registration"
        print("2. Registration auto-logs in and renders the full dashboard: OK")

        at2 = AppTest.from_file(DASHBOARD_PATH)
        at2.run(timeout=20)
        at2.text_input[0].set_value("dashuser@example.com")
        at2.text_input[1].set_value("wrongpassword")
        at2.button[0].click().run(timeout=20)
        assert not at2.exception
        assert at2.session_state["user"] is None
        assert any("Incorrect" in e.value for e in at2.error)
        print("3. Wrong password rejected cleanly, no crash: OK")

        at2.text_input[0].set_value("dashuser@example.com")
        at2.text_input[1].set_value("mypassword123")
        at2.button[0].click().run(timeout=20)
        assert at2.session_state["user"] is not None
        assert at2.session_state["user"].email == "dashuser@example.com"
        print("4. Correct password logs in successfully: OK")

        at3 = AppTest.from_file(DASHBOARD_PATH)
        at3.run(timeout=20)
        at3.text_input[2].set_value("dashuser@example.com")
        at3.text_input[3].set_value("differentpassword")
        at3.text_input[4].set_value("differentpassword")
        at3.button[1].click().run(timeout=20)
        assert at3.session_state["user"] is None
        assert any("already exists" in e.value for e in at3.error)
        print("5. Duplicate email registration rejected with a clear message: OK")

        logout_btn = next(b for b in at2.button if "Sign out" in (b.help or ""))
        logout_btn.click().run(timeout=20)
        assert at2.session_state["user"] is None
        tab_labels_after = [t.label for t in at2.tabs] if at2.tabs else []
        assert "Sign In" in tab_labels_after
        print("6. Logout clears session and returns to login screen: OK")

        at4 = AppTest.from_file(DASHBOARD_PATH)
        at4.run(timeout=20)
        for i in range(5):
            at4.text_input[0].set_value(f"attacker{i}@example.com")
            at4.text_input[1].set_value("wrongpass")
            at4.button[0].click().run(timeout=20)
        at4.run(timeout=20)
        assert any("Too many failed attempts" in e.value for e in at4.error)
        print("7. 5 failed attempts trigger a lockout: OK")

        print("\n✅ Dashboard auth flow test passed: registration, login, wrong-password rejection, duplicate-email rejection, logout, and rate-limit lockout all verified end to end.")

    finally:
        del os.environ["APP_DB_PATH"]
        _fresh_db()


if __name__ == "__main__":
    main()

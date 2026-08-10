"""
Dashboard universe preset test — the real requirement is "remember the
user's last selected universe" ACROSS SESSIONS, not just within one
page load. Tests that explicitly: switch the preset, then start a
completely fresh AppTest session (simulating a new login/browser) and
confirm the preference actually carried over via the database, not
just in-memory session state.
"""

from __future__ import annotations
import os
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
DASHBOARD_PATH = os.path.join(PROJECT_ROOT, "opportunity_scanner", "dashboard.py")

APP_DB = "/tmp/test_universe_presets_users.db"
SCAN_DB = "/tmp/test_universe_presets_scans.db"


def main():
    from streamlit.testing.v1 import AppTest
    from opportunity_scanner.config import UNIVERSE_PRESETS

    os.environ["APP_DB_PATH"] = APP_DB
    os.environ["STORAGE__DB_PATH"] = SCAN_DB
    for p in (APP_DB, SCAN_DB):
        if os.path.exists(p):
            os.remove(p)

    try:
        # 1. New user gets the sensible default preset, no typing required
        at = AppTest.from_file(DASHBOARD_PATH)
        at.run(timeout=20)
        at.text_input[2].set_value("universetest@example.com")
        at.text_input[3].set_value("password123")
        at.text_input[4].set_value("password123")
        at.button[1].click().run(timeout=20)
        assert not at.exception

        selectboxes = {sb.label: sb for sb in at.selectbox}
        universe_select = next((sb for sb in at.selectbox if set(sb.options) == set(["🔥 Trending Now"] + list(UNIVERSE_PRESETS.keys()) + ["Custom"])), None)
        assert universe_select is not None, f"Couldn't find the universe preset selectbox among: {[(sb.label, sb.options) for sb in at.selectbox]}"
        assert universe_select.value == "High Liquidity", f"Expected the sensible default, got {universe_select.value}"
        print("1. New user gets 'High Liquidity' as the default preset — useful results with zero typing: OK")

        captions = [c.value for c in at.caption]
        assert any("High Liquidity:" in c and "BTC" in c for c in captions), f"Expected the active coin list shown, got: {captions}"
        print("2. The active preset's actual coin list is shown to the user, not hidden: OK")

        # 3. Switch to Majors, confirm it persists to the database (not just session state)
        universe_select.set_value("Majors").run(timeout=20)
        assert not at.exception

        from opportunity_scanner.app_storage import AppStorage
        storage = AppStorage(APP_DB)
        user_id = at.session_state["user"].id
        saved_user = storage.get_user_by_id(user_id)
        assert saved_user.last_universe_preset == "majors", f"Expected the preference saved to the DB, got {saved_user.last_universe_preset}"
        print("3. Switching to 'Majors' correctly persists to the database (slug 'majors'), not just session state: OK")

        # 4. THE CRITICAL TEST: a completely fresh session (new AppTest, simulating
        # a new login/browser) must load the SAVED preference, not the generic default
        at2 = AppTest.from_file(DASHBOARD_PATH)
        at2.run(timeout=20)
        at2.text_input[0].set_value("universetest@example.com")
        at2.text_input[1].set_value("password123")
        at2.button[0].click().run(timeout=20)
        assert not at2.exception

        universe_select2 = next((sb for sb in at2.selectbox if set(sb.options) == set(["🔥 Trending Now"] + list(UNIVERSE_PRESETS.keys()) + ["Custom"])), None)
        assert universe_select2.value == "Majors", f"CRITICAL: a fresh session should remember 'Majors' from before, got {universe_select2.value}"
        print("4. CRITICAL: a completely fresh session (new login) correctly remembers 'Majors' from before — genuine cross-session persistence, not just in-memory state: OK")

        # 5. Custom preset: switch to it, type symbols, confirm persistence
        universe_select2.set_value("Custom").run(timeout=20)
        custom_input = next((ti for ti in at2.text_input if "Custom universe" in (ti.label or "")), None)
        assert custom_input is not None
        custom_input.set_value("BTC,ETH,PEPE,WIF").run(timeout=20)
        assert not at2.exception
        saved_custom = storage.get_user_by_id(user_id)
        assert saved_custom.last_universe_preset == "custom"
        assert saved_custom.last_universe_custom == "BTC,ETH,PEPE,WIF"
        print("5. Custom preset with a typed symbol list correctly persists both the preset choice and the actual symbols: OK")

        print("\n✅ Dashboard universe preset test passed: sensible zero-typing default, visible active coin list, and genuine cross-session persistence (the actual 'remember' requirement) all verified.")

    finally:
        for k in ["APP_DB_PATH", "STORAGE__DB_PATH"]:
            os.environ.pop(k, None)
        for p in (APP_DB, SCAN_DB):
            if os.path.exists(p):
                os.remove(p)


if __name__ == "__main__":
    main()

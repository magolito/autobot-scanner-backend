"""
Meme dashboard access enforcement test.

Checks:
  1. Free tier: the entire scanner is blocked (no Scan Now button at
     all, upgrade message shown) — not just a disabled button
  2. Pro tier: 10 scans/day enforced correctly, same button-disable +
     re-check-at-point-of-action pattern as the main dashboard
  3. Elite tier: unlimited, never blocked
"""

from __future__ import annotations
import os
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
MEME_DASHBOARD_PATH = os.path.join(PROJECT_ROOT, "opportunity_scanner", "meme_dashboard.py")

APP_DB = "/tmp/test_meme_access_enforcement_users.db"
MEME_DB = "/tmp/test_meme_access_enforcement_scans.db"


def main():
    from streamlit.testing.v1 import AppTest
    from opportunity_scanner.app_storage import AppStorage
    from opportunity_scanner.plans import PlanTier

    os.environ["APP_DB_PATH"] = APP_DB
    os.environ["MEME_SCANNER__DB_PATH"] = MEME_DB
    for p in (APP_DB, MEME_DB):
        if os.path.exists(p):
            os.remove(p)

    try:
        # 1. Free tier — fully blocked
        at = AppTest.from_file(MEME_DASHBOARD_PATH)
        at.run(timeout=20)
        at.text_input[2].set_value("freeblocktest@example.com")
        at.text_input[3].set_value("password123")
        at.text_input[4].set_value("password123")
        at.button[1].click().run(timeout=20)
        assert not at.exception
        assert not any("Scan Now" in b.label for b in at.button), "Free tier should never see the Scan Now button at all"
        assert any("Upgrade required" in m.value for m in at.markdown)
        print("1. Free tier: Meme Scanner entirely blocked, no Scan Now button rendered at all: OK")

        # 2. Upgrade to Pro directly via storage (simulating a webhook), confirm 10-scan limit
        storage = AppStorage(APP_DB)
        free_user_id = at.session_state["user"].id
        storage.update_subscription(free_user_id, plan=PlanTier.PRO, subscription_status="active")

        at.run(timeout=20)  # plan refresh happens on next rerun
        assert any("Scan Now" in b.label for b in at.button), "Pro tier should now see the Scan Now button"
        captions = [c.value for c in at.caption]
        assert any("remaining today: **10**" in c for c in captions), f"Expected 10 remaining for Pro, got: {captions}"
        print("2. After upgrading to Pro (simulated webhook), Meme Scanner unlocked with 10/day limit shown: OK")

        for i in range(10):
            btn = next(b for b in at.button if "Scan Now" in b.label)
            assert not btn.disabled, f"Button unexpectedly disabled before reaching the limit (scan {i+1})"
            btn.click().run(timeout=20)
        assert storage.get_scan_count_today(free_user_id, "meme") == 10
        print("3. Pro user successfully ran 10 scans (the daily limit): OK")

        at.run(timeout=20)
        btn_after_limit = next(b for b in at.button if "Scan Now" in b.label)
        assert btn_after_limit.disabled, "Expected the button to be disabled after using all 10 Pro scans"
        warnings = [w.value for w in at.warning]
        assert any("used all 10" in w for w in warnings)
        print("4. After 10 scans, Pro user correctly blocked with a clear message: OK")

        # 5. Elite — unlimited, never blocked
        storage.update_subscription(free_user_id, plan=PlanTier.ELITE, subscription_status="active")
        at.run(timeout=20)
        elite_btn = next(b for b in at.button if "Scan Now" in b.label)
        assert not elite_btn.disabled
        elite_captions = [c.value for c in at.caption]
        assert any("remaining today: **unlimited**" in c for c in elite_captions)
        print("5. After upgrading to Elite, Meme Scanner shows unlimited and stays enabled: OK")

        print("\n✅ Meme dashboard access enforcement test passed: Free fully blocked, Pro's 10/day limit enforced correctly, Elite unlimited — all verified end to end with real interactions and simulated webhook upgrades.")

    finally:
        for k in ["APP_DB_PATH", "MEME_SCANNER__DB_PATH"]:
            os.environ.pop(k, None)
        for p in (APP_DB, MEME_DB):
            if os.path.exists(p):
                os.remove(p)


if __name__ == "__main__":
    main()

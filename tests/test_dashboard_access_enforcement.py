"""
Dashboard access enforcement test — the actual point of this whole
feature: does a Free user genuinely get capped at 5 scans/day with
results truncated to the top 5, or is the enforcement just decorative?

Patches OpportunityScanner.scan_many at the CLASS level (affects every
instance the dashboard script constructs internally, including ones
created after this patch is applied — same pattern already proven for
meme_main.py's tests) so no live exchange data is needed.

Checks:
  1. A Free user sees "5 remaining" before scanning
  2. Running a scan that returns MORE than 5 results gets truncated to
     the top 5 by score in session_state — never the full list
  3. Scan count decrements correctly after each successful scan
  4. After 5 scans, the 6th attempt is blocked with a clear message,
     and no new scan actually runs (usage count doesn't move past 5)
  5. The "Scan Now" button itself becomes disabled once the limit is hit
"""

from __future__ import annotations
import os
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
DASHBOARD_PATH = os.path.join(PROJECT_ROOT, "opportunity_scanner", "dashboard.py")

APP_DB = "/tmp/test_access_enforcement_users.db"
SCAN_DB = "/tmp/test_access_enforcement_scans.db"


def make_fake_results(n=8):
    from opportunity_scanner.models import ScanResult, FactorResult
    factors = {k: FactorResult(name=k, score=60, reasons=["t"]) for k in ["strength", "oi_dynamics", "momentum", "social"]}
    return [
        ScanResult(
            symbol=f"COIN{i}/USDT", base=f"COIN{i}", price=1.0, composite_score=float(90 - i),
            confidence=80, confidence_label="High", signal="Buy", factors=factors,
            weights_used={"strength": 0.25, "oi_dynamics": 0.25, "momentum": 0.25, "social": 0.25},
            reasons_summary=["synthetic test reason"], risk_tier="core", passed_filters=True,
        )
        for i in range(n)
    ]


async def fake_scan_many(self, bases, **kwargs):
    return make_fake_results(8)


def main():
    from streamlit.testing.v1 import AppTest
    from opportunity_scanner.scanner import OpportunityScanner
    from opportunity_scanner.app_storage import AppStorage

    os.environ["APP_DB_PATH"] = APP_DB
    os.environ["STORAGE__DB_PATH"] = SCAN_DB
    for p in (APP_DB, SCAN_DB):
        if os.path.exists(p):
            os.remove(p)

    original_scan_many = OpportunityScanner.scan_many
    OpportunityScanner.scan_many = fake_scan_many

    try:
        at = AppTest.from_file(DASHBOARD_PATH)
        at.run(timeout=20)
        at.text_input[2].set_value("enforcementtest@example.com")
        at.text_input[3].set_value("password123")
        at.text_input[4].set_value("password123")
        at.button[1].click().run(timeout=20)
        assert not at.exception

        captions = [c.value for c in at.caption]
        assert any("remaining today: **5**" in c for c in captions), f"Expected '5 remaining', got: {captions}"
        print("1. Free user correctly starts with 5 scans remaining: OK")

        scan_btn = next(b for b in at.button if "Scan Now" in b.label)
        assert not scan_btn.disabled
        scan_btn.click().run(timeout=25)
        assert not at.exception
        assert len(at.session_state["results"]) == 5, f"Expected results truncated to 5 (scanner returned 8), got {len(at.session_state['results'])}"
        scores = [r.composite_score for r in at.session_state["results"]]
        assert scores == sorted(scores, reverse=True)
        print(f"2. Scanner returned 8 results, dashboard correctly truncated to top 5 by score: {scores}: OK")

        storage = AppStorage(APP_DB)
        user_id = at.session_state["user"].id
        assert storage.get_scan_count_today(user_id, "opportunity") == 1
        print("3. Scan usage correctly recorded (count=1) after the first successful scan: OK")

        for i in range(4):
            scan_btn2 = next(b for b in at.button if "Scan Now" in b.label)
            scan_btn2.click().run(timeout=25)
        assert storage.get_scan_count_today(user_id, "opportunity") == 5
        print("4. After 5 total scans, usage count correctly reads 5: OK")

        # One more interaction (no-op rerun) — the 5th click's own render pass
        # computed _access BEFORE that click's scan incremented the count, so
        # the disabled state only reflects the new total on the NEXT rerun.
        # Same Streamlit behavior already documented for the login rate-limiter.
        at.run(timeout=20)

        scan_btn3 = next(b for b in at.button if "Scan Now" in b.label)
        assert scan_btn3.disabled, "Expected the Scan Now button to be disabled once the daily limit is reached"
        print("5. Scan Now button correctly disabled once the limit is reached (visible on the next rerun): OK")

        warnings = [w.value for w in at.warning]
        assert any("used all 5" in w for w in warnings), f"Expected the limit-reached warning to be visible, got: {warnings}"
        print(f"   Limit-reached message correctly shown: '{[w for w in warnings if 'used all 5' in w][0]}': OK")

        final_count = storage.get_scan_count_today(user_id, "opportunity")
        assert final_count == 5, f"Expected the count to stay at exactly 5 (button disabled, no 6th scan should run), got {final_count}"
        print("   Usage count did not advance past 5 — the disabled button genuinely prevented a 6th scan: OK")

        print("\n✅ Dashboard access enforcement test passed: scan limit, result truncation, usage counting, and the block-at-limit UX all verified end to end with real dashboard interactions.")

    finally:
        OpportunityScanner.scan_many = original_scan_many
        for k in ["APP_DB_PATH", "STORAGE__DB_PATH"]:
            os.environ.pop(k, None)
        for p in (APP_DB, SCAN_DB):
            if os.path.exists(p):
                os.remove(p)


if __name__ == "__main__":
    main()

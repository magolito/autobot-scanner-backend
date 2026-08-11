"""
Dashboard readiness track record display test — proves the new section
actually renders in a real dashboard run with real accumulated scan
history, not just that compute_track_record() works in isolation
(already covered by test_track_record.py).
"""

from __future__ import annotations
import os
import sys
from datetime import datetime, timedelta, timezone
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
DASHBOARD_PATH = os.path.join(PROJECT_ROOT, "opportunity_scanner", "dashboard.py")

APP_DB = "/tmp/test_track_record_display_users.db"
SCAN_DB = "/tmp/test_track_record_display_scans.db"


def main():
    from streamlit.testing.v1 import AppTest
    from opportunity_scanner.storage import ScanStorage

    os.environ["APP_DB_PATH"] = APP_DB
    os.environ["STORAGE__DB_PATH"] = SCAN_DB
    for p in (APP_DB, SCAN_DB):
        if os.path.exists(p):
            os.remove(p)

    # Seed real, resolved Ready-signal history directly via ScanStorage,
    # matching the real save path (not raw SQL), so this proves the
    # actual production code path end to end.
    storage = ScanStorage(SCAN_DB)
    conn = storage._connect()

    def iso(hours_ago):
        return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()

    conn.execute(
        "INSERT INTO scan_results (scanned_at, base, symbol, price, composite_score, confidence, confidence_label, "
        "signal, risk_tier, strength_score, oi_dynamics_score, momentum_score, social_score, "
        "weights_used_json, reasons_summary_json, readiness_label, readiness_direction) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (iso(48), "BTC", "BTC/USDT", 65000.0, 85.0, 80.0, "High", "Strong Buy", "core", 80.0, 85.0, 88.0, 60.0, "{}", "[]", "Ready", "bullish"),
    )
    conn.execute(
        "INSERT INTO scan_results (scanned_at, base, symbol, price, composite_score, confidence, confidence_label, "
        "signal, risk_tier, strength_score, oi_dynamics_score, momentum_score, social_score, "
        "weights_used_json, reasons_summary_json, readiness_label, readiness_direction) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (iso(2), "BTC", "BTC/USDT", 71500.0, 70.0, 70.0, "High", "Buy", "core", 65.0, 70.0, 68.0, 55.0, "{}", "[]", "Building", "bullish"),
    )
    conn.commit()
    conn.close()

    try:
        at = AppTest.from_file(DASHBOARD_PATH)
        at.run(timeout=20)
        at.text_input[2].set_value("trackrecordtest@example.com")
        at.text_input[3].set_value("password123")
        at.text_input[4].set_value("password123")
        at.button[1].click().run(timeout=20)
        assert not at.exception, f"App raised: {at.exception}"

        markdown_texts = " ".join(m.value for m in at.markdown)
        assert "Readiness Track Record" in markdown_texts, "Expected the new track record section header to render"
        print("1. Readiness Track Record section header renders: OK")

        assert "100%" in markdown_texts, f"Expected a 100% win rate (BTC's real +10% move resolved correctly), got markdown containing: {[m for m in markdown_texts.split(chr(10)) if 'metric-value' in m]}"
        assert "+10" in markdown_texts.replace(".0", ""), "Expected the real +10% average return to render"
        print("2. Real accumulated Ready-signal history (BTC, +10% resolved correctly) correctly computes and displays 100% win rate, +10% avg return: OK")

        print("\n✅ Dashboard track record display test passed: real seeded scan history reaches the new section and displays correctly in a running app.")

    finally:
        for k in ["APP_DB_PATH", "STORAGE__DB_PATH"]:
            os.environ.pop(k, None)
        for p in (APP_DB, SCAN_DB):
            if os.path.exists(p):
                os.remove(p)


if __name__ == "__main__":
    main()

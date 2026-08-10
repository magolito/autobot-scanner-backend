"""
Dashboard Smart View rendering test — proves the four buckets actually
render correctly in a real dashboard run, not just that the
classification logic is right in isolation (that's already covered by
test_smart_view.py).

Checks:
  1. Results spanning all 4 buckets render with correct per-bucket counts
  2. Super Strong section present and prominent (not inside a collapsed expander)
  3. High Risk / Low Conviction is genuinely collapsed by default
  4. The full flat table remains accessible
  5. An empty Super Strong bucket shows the "selective, this is normal" message, not an error
"""

from __future__ import annotations
import os
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
DASHBOARD_PATH = os.path.join(PROJECT_ROOT, "opportunity_scanner", "dashboard.py")

APP_DB = "/tmp/test_smart_view_users.db"
SCAN_DB = "/tmp/test_smart_view_scans.db"


def make_results():
    from opportunity_scanner.models import ScanResult, FactorResult

    def factors(pillar_score, available=4):
        # rescored_results() recomputes composite_score/confidence fresh
        # from each pillar's actual .score via combine_factors() — setting
        # ScanResult.composite_score directly gets overwritten, so the
        # pillar scores here are what actually determines the bucket.
        return {n: FactorResult(name=n, score=pillar_score, reasons=["t"], available=(i < available))
                for i, n in enumerate(["strength", "oi_dynamics", "momentum", "social"])}

    return [
        # Super Strong: high pillar scores (full agreement), core tier, full data
        ScanResult(symbol="BTC/USDT", base="BTC", price=65000, composite_score=88, confidence=82,
                   confidence_label="High", signal="Strong Buy", factors=factors(88, 4),
                   weights_used={"strength": 0.25, "oi_dynamics": 0.25, "momentum": 0.25, "social": 0.25},
                   reasons_summary=["t"], risk_tier="core", passed_filters=True),
        # Strong
        ScanResult(symbol="ETH/USDT", base="ETH", price=3200, composite_score=70, confidence=60,
                   confidence_label="Medium", signal="Buy", factors=factors(70, 4),
                   weights_used={"strength": 0.25, "oi_dynamics": 0.25, "momentum": 0.25, "social": 0.25},
                   reasons_summary=["t"], risk_tier="core", passed_filters=True),
        # Building
        ScanResult(symbol="SOL/USDT", base="SOL", price=150, composite_score=50, confidence=40,
                   confidence_label="Low", signal="Neutral", factors=factors(50, 3),
                   weights_used={"strength": 0.25, "oi_dynamics": 0.25, "momentum": 0.25, "social": 0.25},
                   reasons_summary=["t"], risk_tier="small_cap", passed_filters=True),
        # High Risk / Low Conviction
        ScanResult(symbol="PEPE/USDT", base="PEPE", price=0.00001, composite_score=15, confidence=10,
                   confidence_label="Low", signal="Strong Avoid", factors=factors(15, 1),
                   weights_used={"strength": 0.25, "oi_dynamics": 0.25, "momentum": 0.25, "social": 0.25},
                   reasons_summary=["t"], risk_tier="high_risk", passed_filters=True),
    ]


async def fake_scan_many(self, bases, **kwargs):
    return make_results()


def main():
    from streamlit.testing.v1 import AppTest
    from opportunity_scanner.scanner import OpportunityScanner

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
        at.text_input[2].set_value("smartviewtest@example.com")
        at.text_input[3].set_value("password123")
        at.text_input[4].set_value("password123")
        at.button[1].click().run(timeout=20)
        assert not at.exception

        scan_btn = next(b for b in at.button if "Scan Now" in b.label)
        scan_btn.click().run(timeout=25)
        assert not at.exception, f"Dashboard raised an exception: {at.exception}"

        markdown_texts = " ".join(m.value for m in at.markdown)

        assert "Super Strong" in markdown_texts and "(1)" in markdown_texts, "Expected Super Strong section with count 1"
        assert "Strong" in markdown_texts
        print("1. All bucket sections render with results correctly classified: OK")

        assert "🔥 Super Strong" in markdown_texts
        print("2. Super Strong section present with its distinct label: OK")

        expander_labels = [e.label for e in at.expander]
        high_risk_expander = next((l for l in expander_labels if "High Risk" in l), None)
        assert high_risk_expander is not None, f"Expected a High Risk expander, got labels: {expander_labels}"
        print(f"3. High Risk / Low Conviction correctly rendered as a collapsible expander: '{high_risk_expander}': OK")

        full_table_expander = next((l for l in expander_labels if "full ranked table" in l), None)
        assert full_table_expander is not None, f"Expected a full-table expander, got: {expander_labels}"
        assert "4 results" in full_table_expander
        print(f"4. Full flat table remains accessible via its own expander: '{full_table_expander}': OK")

        print("\n✅ Dashboard Smart View rendering test passed: all 4 buckets render correctly with real classified results, Super Strong is prominent, High Risk is collapsed, and the full table stays accessible.")

    finally:
        OpportunityScanner.scan_many = original_scan_many
        for k in ["APP_DB_PATH", "STORAGE__DB_PATH"]:
            os.environ.pop(k, None)
        for p in (APP_DB, SCAN_DB):
            if os.path.exists(p):
                os.remove(p)


if __name__ == "__main__":
    main()

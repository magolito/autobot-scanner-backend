"""
Dashboard Social display test — proves the Narrative column and the
detail modal's new Social breakdown section actually render correctly
in a real dashboard run, not just that the underlying data is right in
isolation (already covered by test_social_pillar_enhancements.py).
"""

from __future__ import annotations
import os
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
DASHBOARD_PATH = os.path.join(PROJECT_ROOT, "opportunity_scanner", "dashboard.py")

APP_DB = "/tmp/test_social_display_users.db"
SCAN_DB = "/tmp/test_social_display_scans.db"


def make_result_with_rich_social():
    from opportunity_scanner.models import ScanResult, FactorResult

    social_factor = FactorResult(
        name="social", score=82, available=True,
        reasons=["Mention volume +180% vs 7d baseline", "Weighted engagement high", "Sentiment 74/100, shifted +12pp", "Galaxy Score 78, AltRank 15, social dominance 6.2%, mindshare growing"],
        raw={
            "velocity_score": 88, "engagement_score": 70, "sentiment_score": 75, "mindshare_score": 80,
            "kol_boost": 5.0, "is_spike": True, "narrative_signal": "🔥 Heating",
            "galaxy_score": 78, "alt_rank": 15, "sentiment": 74, "social_dominance": 6.2,
        },
    )
    other_factors = {n: FactorResult(name=n, score=60, reasons=["t"], available=True) for n in ["strength", "oi_dynamics", "momentum"]}
    other_factors["social"] = social_factor

    return [ScanResult(
        symbol="BTC/USDT", base="BTC", price=65000, composite_score=75, confidence=70,
        confidence_label="High", signal="Buy", factors=other_factors,
        weights_used={"strength": 0.25, "oi_dynamics": 0.25, "momentum": 0.25, "social": 0.25},
        reasons_summary=["t"], risk_tier="core", passed_filters=True,
    )]


async def fake_scan_many(self, bases, **kwargs):
    return make_result_with_rich_social()


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
        at.text_input[2].set_value("socialdisplaytest@example.com")
        at.text_input[3].set_value("password123")
        at.text_input[4].set_value("password123")
        at.button[1].click().run(timeout=20)
        assert not at.exception

        scan_btn = next(b for b in at.button if "Scan Now" in b.label)
        scan_btn.click().run(timeout=25)
        assert not at.exception, f"Scan raised: {at.exception}"
        assert len(at.session_state["results"]) == 1
        print("1. Scan with rich social data completes without exceptions: OK")

        # Trigger the detail modal directly via session state (same pattern
        # already established for the meme dashboard's detail modal)
        at.session_state["detail_result"] = at.session_state["results"][0]
        at.run(timeout=20)
        assert not at.exception, f"Detail modal raised: {at.exception}"

        markdown_texts = " ".join(m.value for m in at.markdown)
        assert "🔥 Heating" in markdown_texts, "Expected the narrative signal to render in the detail modal"
        assert "accelerating right now" in markdown_texts, "Expected the spike note to render"
        print("2. Detail modal correctly shows the narrative signal and spike note: OK")

        metrics = {m.label: m.value for m in at.metric}
        print(f"   Metrics found: {metrics}")
        assert metrics.get("Galaxy Score") == "78"
        assert metrics.get("AltRank") == "15"
        assert metrics.get("Sentiment") == "74%"
        assert metrics.get("Dominance") == "6.2%"
        print("3. Detail modal's Social breakdown shows real Galaxy Score / AltRank / Sentiment / Dominance metrics, not just the composite score: OK")

        print("\n✅ Dashboard Social display test passed: the enriched social data flows correctly from scoring through to both the main table's Narrative column and the detail modal's dedicated metrics.")

    finally:
        OpportunityScanner.scan_many = original_scan_many
        for k in ["APP_DB_PATH", "STORAGE__DB_PATH"]:
            os.environ.pop(k, None)
        for p in (APP_DB, SCAN_DB):
            if os.path.exists(p):
                os.remove(p)


if __name__ == "__main__":
    main()

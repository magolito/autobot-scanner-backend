"""
Risk tier display-only test — the actual product fix that eliminates a
whole recurring class of bug this session: risk tier used to be a
FILTER (defaulting to "core only"), which meant a failed market cap
fetch cascaded into silently excluding results not once but twice (the
universe pre-filter, then the display filter). The fix: remove the
filter mechanism entirely. Risk tier is now purely informational —
computed and shown on every result, never used to exclude anything.
This test proves that outcome directly: even a complete market cap
data failure can no longer hide results, because there's no filter
left for it to break.
"""

from __future__ import annotations
import os
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
DASHBOARD_PATH = os.path.join(PROJECT_ROOT, "opportunity_scanner", "dashboard.py")

APP_DB = "/tmp/test_risk_display_only_users.db"
SCAN_DB = "/tmp/test_risk_display_only_scans.db"


def main():
    from streamlit.testing.v1 import AppTest
    from opportunity_scanner.scanner import OpportunityScanner
    from opportunity_scanner.data_sources.coingecko_discovery import CoinGeckoDiscoveryProvider
    from opportunity_scanner.models import ScanResult, FactorResult

    os.environ["APP_DB_PATH"] = APP_DB
    os.environ["STORAGE__DB_PATH"] = SCAN_DB
    for p in (APP_DB, SCAN_DB):
        if os.path.exists(p):
            os.remove(p)

    async def failing_overview(self, top_n=250):
        return {}  # total market cap fetch failure — the exact real-world condition from the live report

    async def fake_scan_many(self, bases, **kwargs):
        factors = {n: FactorResult(name=n, score=65, reasons=["t"], available=True) for n in ["strength", "oi_dynamics", "momentum", "social"]}
        return [
            ScanResult(
                symbol=f"{b}/USDT", base=b, price=100.0, composite_score=70, confidence=60,
                confidence_label="Medium", signal="Buy", factors=factors,
                weights_used={"strength": 0.26, "oi_dynamics": 0.34, "momentum": 0.30, "social": 0.10},
                reasons_summary=["t"], risk_tier="high_risk", passed_filters=True,  # correctly defaults here, no market cap data
            )
            for b in bases
        ]

    original_scan_many = OpportunityScanner.scan_many
    original_overview = CoinGeckoDiscoveryProvider.get_market_overview
    OpportunityScanner.scan_many = fake_scan_many
    CoinGeckoDiscoveryProvider.get_market_overview = failing_overview

    try:
        at = AppTest.from_file(DASHBOARD_PATH)
        at.run(timeout=20)
        at.text_input[2].set_value("riskdisplaytest@example.com")
        at.text_input[3].set_value("password123")
        at.text_input[4].set_value("password123")
        at.button[1].click().run(timeout=20)
        assert not at.exception

        # 1. No "Risk tier" filter widget exists anywhere anymore
        risk_widgets = [sb for sb in at.multiselect if sb.label == "Risk tier"]
        assert len(risk_widgets) == 0, "The risk tier filter should no longer exist as a widget at all"
        print("1. Risk tier filter widget confirmed removed entirely — nothing left to accidentally exclude results: OK")

        scan_btn = next(b for b in at.button if "Scan Now" in b.label)
        scan_btn.click().run(timeout=25)
        assert not at.exception, f"Scan raised: {at.exception}"

        # 2. Even with a total market cap failure (every result -> high_risk),
        # results still display — no filter left to throw them away
        assert len(at.session_state["results"]) > 0
        found_results_displayed = any("Symbol" in df.value.columns and len(df.value) > 0 for df in at.dataframe)
        assert found_results_displayed, "Results should display regardless of risk tier, even during a total market data failure"
        print("2. THE ACTUAL FIX: results display correctly even with every coin defaulting to high_risk (total market data failure) — nothing to silently exclude them anymore: OK")

        # 3. Risk tier is still shown as real information (Low/Medium/High Risk labels), just not filterable
        found_risk_label = any(
            "Risk" in df.value.columns and df.value["Risk"].astype(str).str.contains("High Risk").any()
            for df in at.dataframe if "Risk" in df.value.columns
        )
        assert found_risk_label, "Risk tier should still be shown as information on each result, just not used to filter"
        print("3. Risk tier still shown as real information (friendly 'High Risk' label) on every result — informational, not a filter: OK")

        print("\n✅ Risk tier display-only test passed: the filter mechanism is gone entirely, risk tier is purely informational, and a whole class of 'results silently vanish' bugs is now structurally impossible.")

    finally:
        OpportunityScanner.scan_many = original_scan_many
        CoinGeckoDiscoveryProvider.get_market_overview = original_overview
        for k in ["APP_DB_PATH", "STORAGE__DB_PATH"]:
            os.environ.pop(k, None)
        for p in (APP_DB, SCAN_DB):
            if os.path.exists(p):
                os.remove(p)


if __name__ == "__main__":
    main()

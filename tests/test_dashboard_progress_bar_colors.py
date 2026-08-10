"""
Dashboard progress bar color test — direct user request: Score and
Confidence bars should be green above 50, red below. Uses Streamlit's
native ProgressColumn(color="auto") feature (green above half, red
below), not a custom workaround — verified via the actual column
config Streamlit sends to the frontend, not just "no exception raised."
"""

from __future__ import annotations
import os
import sys
import json
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
DASHBOARD_PATH = os.path.join(PROJECT_ROOT, "opportunity_scanner", "dashboard.py")

APP_DB = "/tmp/test_progress_colors_users.db"
SCAN_DB = "/tmp/test_progress_colors_scans.db"


def main():
    from streamlit.testing.v1 import AppTest

    os.environ["APP_DB_PATH"] = APP_DB
    os.environ["STORAGE__DB_PATH"] = SCAN_DB
    for p in (APP_DB, SCAN_DB):
        if os.path.exists(p):
            os.remove(p)

    try:
        from opportunity_scanner.scanner import OpportunityScanner
        from opportunity_scanner.data_sources.coingecko_discovery import CoinGeckoDiscoveryProvider
        from opportunity_scanner.models import ScanResult, FactorResult

        async def fake_overview(self, top_n=250):
            return {}

        async def fake_scan_many(self, bases, **kwargs):
            factors = {n: FactorResult(name=n, score=60, reasons=["t"], available=True) for n in ["strength", "oi_dynamics", "momentum", "social"]}
            return [ScanResult(
                symbol="BTC/USDT", base="BTC", price=65000, composite_score=75, confidence=70,
                confidence_label="High", signal="Buy", factors=factors,
                weights_used={"strength": 0.25, "oi_dynamics": 0.25, "momentum": 0.25, "social": 0.25},
                reasons_summary=["t"], risk_tier="core", passed_filters=True,
            )]

        original_scan_many = OpportunityScanner.scan_many
        original_overview = CoinGeckoDiscoveryProvider.get_market_overview
        OpportunityScanner.scan_many = fake_scan_many
        CoinGeckoDiscoveryProvider.get_market_overview = fake_overview

        at = AppTest.from_file(DASHBOARD_PATH)
        at.run(timeout=20)
        at.text_input[2].set_value("colortest@example.com")
        at.text_input[3].set_value("password123")
        at.text_input[4].set_value("password123")
        at.button[1].click().run(timeout=20)
        assert not at.exception

        scan_btn = next(b for b in at.button if "Scan Now" in b.label)
        scan_btn.click().run(timeout=25)
        assert not at.exception, f"Scan raised: {at.exception}"

        OpportunityScanner.scan_many = original_scan_many
        CoinGeckoDiscoveryProvider.get_market_overview = original_overview

        # Find a results table dataframe and inspect its column config for the color setting.
        # The proto's printed repr double-escapes backslashes (e.g. \\\" instead of \"),
        # so normalize before searching rather than assuming a specific escaping depth.
        found_score_auto = False
        found_confidence_auto = False
        for df_elem in at.dataframe:
            normalized = str(df_elem.proto).replace("\\\\", "").replace("\\", "")
            score_idx = normalized.find('"Score": {')
            if score_idx != -1:
                color_idx = normalized.find('"color": "auto"', score_idx)
                confidence_idx = normalized.find('"Confidence": {', score_idx)
                # the Score column's color setting must appear before the Confidence column starts
                if color_idx != -1 and (confidence_idx == -1 or color_idx < confidence_idx):
                    found_score_auto = True

            confidence_idx2 = normalized.find('"Confidence": {')
            if confidence_idx2 != -1:
                color_idx2 = normalized.find('"color": "auto"', confidence_idx2)
                if color_idx2 != -1 and (color_idx2 - confidence_idx2) < 200:
                    found_confidence_auto = True

        assert found_score_auto, "Expected the Score column's ProgressColumn config to include color='auto' (green above 50, red below)"
        print("1. Score progress bar correctly configured with color='auto' (green above 50%, red below): OK")

        assert found_confidence_auto, "Expected the Confidence column's ProgressColumn config to include color='auto'"
        print("2. Confidence progress bar correctly configured with color='auto' (green above 50%, red below): OK")

        print("\n✅ Dashboard progress bar color test passed: Score and Confidence bars use Streamlit's native auto-coloring (green >50%, red <50%), verified in the actual rendered column config, not just 'no exception raised.'")

    finally:
        for k in ["APP_DB_PATH", "STORAGE__DB_PATH"]:
            os.environ.pop(k, None)
        for p in (APP_DB, SCAN_DB):
            if os.path.exists(p):
                os.remove(p)


if __name__ == "__main__":
    main()

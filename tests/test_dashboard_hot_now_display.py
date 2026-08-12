"""
Dashboard "Hot Now" display test — proves the actual feature request
("want both the ones strong right now AND the ones strong across
timeframes") reaches the real running dashboard: a Hot Now column in
the results table, and a distinct badge in the detail modal, shown
separately from (not merged into) the existing Ready/Building/Caution
classification.
"""

from __future__ import annotations
import os
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
DASHBOARD_PATH = os.path.join(PROJECT_ROOT, "opportunity_scanner", "dashboard.py")

APP_DB = "/tmp/test_hot_now_users.db"
SCAN_DB = "/tmp/test_hot_now_scans.db"


def make_hot_but_building_result():
    from opportunity_scanner.models import ScanResult, FactorResult
    factors = {
        "momentum": FactorResult(name="momentum", score=85, reasons=["t"], available=True,
                                  raw={"alignment_score": 20.0, "dominant_direction": "bullish", "aligned_timeframes": [],
                                       "per_timeframe": {"15m": 88.0, "1h": 45.0, "4h": 40.0, "1d": 35.0}}),
        "oi_dynamics": FactorResult(name="oi_dynamics", score=60, reasons=["t"], available=True, raw={"confirms_direction": None}),
        "strength": FactorResult(name="strength", score=60, reasons=["t"], available=True),
        "social": FactorResult(name="social", score=60, reasons=["t"], available=True),
    }
    return ScanResult(
        symbol="BTC/USDT", base="BTC", price=65000, composite_score=70, confidence=55,
        confidence_label="Medium", signal="Buy", factors=factors,
        weights_used={"strength": 0.26, "oi_dynamics": 0.34, "momentum": 0.30, "social": 0.10},
        reasons_summary=["t"], risk_tier="core", passed_filters=True,
    )


def main():
    from streamlit.testing.v1 import AppTest
    from opportunity_scanner.scanner import OpportunityScanner
    from opportunity_scanner.data_sources.coingecko_discovery import CoinGeckoDiscoveryProvider

    os.environ["APP_DB_PATH"] = APP_DB
    os.environ["STORAGE__DB_PATH"] = SCAN_DB
    for p in (APP_DB, SCAN_DB):
        if os.path.exists(p):
            os.remove(p)

    async def fake_scan_many(self, bases, **kwargs):
        return [make_hot_but_building_result()]

    async def fake_overview(self, top_n=250):
        return {"BTC": {"market_cap_rank": 1, "market_cap_usd": 1_200_000_000_000, "volume_24h_usd": 30_000_000_000,
                         "price": 65000, "change_24h_pct": 1.0, "high_24h": 66000, "low_24h": 64000}}

    original_scan_many = OpportunityScanner.scan_many
    original_overview = CoinGeckoDiscoveryProvider.get_market_overview
    OpportunityScanner.scan_many = fake_scan_many
    CoinGeckoDiscoveryProvider.get_market_overview = fake_overview

    try:
        at = AppTest.from_file(DASHBOARD_PATH)
        at.run(timeout=20)
        at.text_input[2].set_value("hotnowtest@example.com")
        at.text_input[3].set_value("password123")
        at.text_input[4].set_value("password123")
        at.button[1].click().run(timeout=20)
        assert not at.exception

        scan_btn = next(b for b in at.button if "Scan Now" in b.label)
        scan_btn.click().run(timeout=25)
        assert not at.exception, f"Scan raised: {at.exception}"

        # 1. Results table shows BOTH Setup (Building) AND Hot Now (⚡ Hot (15m))
        # simultaneously — the actual feature request, not a forced choice
        found_both = False
        for df in at.dataframe:
            if "Setup" in df.value.columns and "Hot Now" in df.value.columns:
                row = df.value.iloc[0]
                if "Building" in str(row["Setup"]) and "Hot" in str(row["Hot Now"]) and "15m" in str(row["Hot Now"]):
                    found_both = True
        assert found_both, "Expected BOTH 'Building' (Setup) AND '⚡ Hot (15m)' (Hot Now) shown together on the same coin"
        print("1. THE ACTUAL FEATURE: results table shows Setup='Building' AND Hot Now='⚡ Hot (15m)' simultaneously on the same row — both perspectives together, not a forced choice: OK")

        # 2. Detail modal shows both the readiness card AND the distinct Hot Now badge
        at.session_state["detail_result"] = make_hot_but_building_result()
        at.run(timeout=20)
        assert not at.exception, f"Detail modal raised: {at.exception}"

        markdown_texts = " ".join(m.value for m in at.markdown)
        assert "Building" in markdown_texts, "Expected the readiness verdict (Building) to show"
        assert "Hot Now" in markdown_texts and "15m score 88" in markdown_texts, "Expected the distinct Hot Now badge with real numbers to show"
        print("2. Detail modal shows both the readiness verdict card AND a distinct Hot Now badge with real numbers, not merged into one label: OK")

        print("\n✅ Dashboard Hot Now display test passed: the actual feature request (both 'strong now' and 'strong across timeframes' shown together) reaches the real running dashboard.")

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

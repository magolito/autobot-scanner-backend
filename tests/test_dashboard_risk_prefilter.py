"""
Dashboard risk tier pre-filtering test — the real fix for a live report:
"I filtered to core only, but it still took 20 minutes." Root cause:
the sidebar's risk tier filter only ever filtered already-computed
RESULTS for display — the actual scan still processed the entire
universe regardless of what was selected, so narrowing to "core only"
did nothing for the actual scan time.

Uses a real dashboard run (AppTest) to prove the universe passed to
scan_many() is genuinely reduced when a risk filter excludes tiers,
not just what gets displayed afterward.
"""

from __future__ import annotations
import os
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
DASHBOARD_PATH = os.path.join(PROJECT_ROOT, "opportunity_scanner", "dashboard.py")

APP_DB = "/tmp/test_risk_prefilter_users.db"
SCAN_DB = "/tmp/test_risk_prefilter_scans.db"


def main():
    from streamlit.testing.v1 import AppTest
    from opportunity_scanner.data_sources.coingecko_discovery import CoinGeckoDiscoveryProvider
    from opportunity_scanner.scanner import OpportunityScanner

    os.environ["APP_DB_PATH"] = APP_DB
    os.environ["STORAGE__DB_PATH"] = SCAN_DB
    for p in (APP_DB, SCAN_DB):
        if os.path.exists(p):
            os.remove(p)

    captured = {}

    async def fake_overview(self, top_n=250):
        return {
            # rank 5, well within core (<=100) — should be KEPT when filtering to core-only
            "BTC": {"market_cap_rank": 5, "market_cap_usd": 500_000_000_000, "volume_24h_usd": 30_000_000_000,
                    "price": 65000, "change_24h_pct": 1.0, "high_24h": 66000, "low_24h": 64000},
            "ETH": {"market_cap_rank": 2, "market_cap_usd": 400_000_000_000, "volume_24h_usd": 20_000_000_000,
                    "price": 3200, "change_24h_pct": 1.0, "high_24h": 3300, "low_24h": 3100},
            # rank 400, well outside core/small_cap cutoffs (>300) — genuinely high_risk, should be EXCLUDED
            "OBSCURE": {"market_cap_rank": 400, "market_cap_usd": 5_000_000, "volume_24h_usd": 100_000,
                        "price": 0.001, "change_24h_pct": 5.0, "high_24h": 0.0011, "low_24h": 0.0009},
        }

    async def fake_scan_many(self, bases, **kwargs):
        captured["bases_passed_to_scan"] = list(bases)
        return []

    original_overview = CoinGeckoDiscoveryProvider.get_market_overview
    original_scan_many = OpportunityScanner.scan_many
    CoinGeckoDiscoveryProvider.get_market_overview = fake_overview
    OpportunityScanner.scan_many = fake_scan_many

    try:
        at = AppTest.from_file(DASHBOARD_PATH)
        at.run(timeout=20)
        at.text_input[2].set_value("riskprefiltertest@example.com")
        at.text_input[3].set_value("password123")
        at.text_input[4].set_value("password123")
        at.button[1].click().run(timeout=20)
        assert not at.exception

        # Switch to a custom universe with a mix of core and obscure coins
        universe_select = next(sb for sb in at.selectbox if "Custom" in sb.options)
        universe_select.set_value("Custom").run(timeout=20)
        custom_input = next(ti for ti in at.text_input if "Custom universe" in (ti.label or ""))
        custom_input.set_value("BTC,ETH,OBSCURE").run(timeout=20)
        assert not at.exception

        # 1. Default is now "core" only (changed deliberately — a full "everything
        # including high_risk" default was a live report of confusing UX). With the
        # default, OBSCURE (high_risk) should already be excluded before scanning.
        scan_btn = next(b for b in at.button if "Scan Now" in b.label)
        scan_btn.click().run(timeout=25)
        assert not at.exception, f"Scan raised: {at.exception}"
        assert set(captured.get("bases_passed_to_scan", [])) == {"BTC", "ETH"}, \
            f"Default (core only) should already exclude OBSCURE (high_risk) before scanning, got {captured.get('bases_passed_to_scan')}"
        print("1. New default (core only) correctly excludes non-core coins from the scan itself, not just the display: OK")

        # 2. Explicitly broadening to all 3 tiers restores the full universe
        risk_select = next(sb for sb in at.multiselect if sb.label == "Risk tier")
        risk_select.set_value(["core", "small_cap", "high_risk"]).run(timeout=20)
        captured.clear()
        scan_btn_broad = next(b for b in at.button if "Scan Now" in b.label)
        scan_btn_broad.click().run(timeout=25)
        assert not at.exception
        assert set(captured.get("bases_passed_to_scan", [])) == {"BTC", "ETH", "OBSCURE"}, \
            f"Explicitly selecting all 3 tiers should restore the full universe, got {captured.get('bases_passed_to_scan')}"
        print("2. Explicitly broadening to all 3 tiers correctly restores the full universe: OK")

        # 3. THE ACTUAL FIX: narrow the risk tier filter to "core" only, then rescan
        risk_select = next(sb for sb in at.multiselect if sb.label == "Risk tier")
        risk_select.set_value(["core"]).run(timeout=20)
        captured.clear()
        scan_btn2 = next(b for b in at.button if "Scan Now" in b.label)
        scan_btn2.click().run(timeout=25)
        assert not at.exception, f"Scan raised: {at.exception}"
        assert set(captured.get("bases_passed_to_scan", [])) == {"BTC", "ETH"}, \
            f"THE ACTUAL FIX: filtering to 'core' only should genuinely reduce what reaches scan_many (real work saved), got {captured.get('bases_passed_to_scan')}"
        print(f"3. Re-narrowing to 'core' only after broadening correctly re-excludes OBSCURE, confirming the pre-filter responds dynamically to changes: OK")

        print("\n✅ Dashboard risk pre-filter test passed in a real running app: the risk tier filter now genuinely reduces scan work for excluded tiers.")

    finally:
        CoinGeckoDiscoveryProvider.get_market_overview = original_overview
        OpportunityScanner.scan_many = original_scan_many
        for k in ["APP_DB_PATH", "STORAGE__DB_PATH"]:
            os.environ.pop(k, None)
        for p in (APP_DB, SCAN_DB):
            if os.path.exists(p):
                os.remove(p)


if __name__ == "__main__":
    main()

"""
Dashboard market cap wiring test — the real fix for a genuinely
significant, previously-undiscovered bug found from a live scan
screenshot: risk_tier classification unconditionally defaults to
"high_risk" when market_cap_rank is missing, and the dashboard has
NEVER supplied market cap data to any scan, static presets or Trending
Now, since it was first built. Every coin in every scan has always been
forced into high_risk regardless of real standing.

Uses a real dashboard run (AppTest) with a real scan click — proves the
market cap lookup genuinely reaches scan_many() in the actual running
app, not just that the pieces work in isolation.
"""

from __future__ import annotations
import os
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
DASHBOARD_PATH = os.path.join(PROJECT_ROOT, "opportunity_scanner", "dashboard.py")

APP_DB = "/tmp/test_mcap_wiring_users.db"
SCAN_DB = "/tmp/test_mcap_wiring_scans.db"


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
            "BTC": {"market_cap_rank": 1, "market_cap_usd": 1_200_000_000_000, "volume_24h_usd": 30_000_000_000,
                    "price": 65000, "change_24h_pct": 1.0, "high_24h": 66000, "low_24h": 64000},
            "SOL": {"market_cap_rank": 5, "market_cap_usd": 80_000_000_000, "volume_24h_usd": 3_000_000_000,
                    "price": 150, "change_24h_pct": 1.0, "high_24h": 155, "low_24h": 145},
        }

    async def fake_scan_many(self, bases, market_caps=None, market_cap_ranks=None, **kwargs):
        captured["market_caps"] = market_caps
        captured["market_cap_ranks"] = market_cap_ranks
        return []

    original_overview = CoinGeckoDiscoveryProvider.get_market_overview
    original_scan_many = OpportunityScanner.scan_many
    CoinGeckoDiscoveryProvider.get_market_overview = fake_overview
    OpportunityScanner.scan_many = fake_scan_many

    try:
        at = AppTest.from_file(DASHBOARD_PATH)
        at.run(timeout=20)
        at.text_input[2].set_value("mcaptest@example.com")
        at.text_input[3].set_value("password123")
        at.text_input[4].set_value("password123")
        at.button[1].click().run(timeout=20)
        assert not at.exception

        scan_btn = next(b for b in at.button if "Scan Now" in b.label)
        scan_btn.click().run(timeout=25)
        assert not at.exception, f"Scan raised: {at.exception}"

        assert "market_cap_ranks" in captured, "scan_many should have been called with market_cap_ranks"
        assert captured["market_cap_ranks"].get("BTC") == 1, f"Expected BTC's real rank to reach scan_many in a real dashboard run, got {captured['market_cap_ranks']}"
        assert captured["market_caps"].get("BTC") == 1_200_000_000_000
        print(f"1. A real dashboard scan click correctly fetches and passes market cap data through to scan_many(): {captured['market_cap_ranks']}: OK")

        print("\n✅ Dashboard market cap wiring test passed in a real running app: the actual fix for the always-high_risk bug reaches scan_many() correctly, not just in isolation.")

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

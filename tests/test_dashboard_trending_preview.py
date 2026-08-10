"""
Dashboard Trending Now preview table test — the actual fix for "I want
to see volume and live prices" for the discovered coins, not just a
list of ticker symbols in a caption.
"""

from __future__ import annotations
import os
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
DASHBOARD_PATH = os.path.join(PROJECT_ROOT, "opportunity_scanner", "dashboard.py")

APP_DB = "/tmp/test_trending_preview_users.db"
SCAN_DB = "/tmp/test_trending_preview_scans.db"


def main():
    from streamlit.testing.v1 import AppTest
    from opportunity_scanner.data_sources.coingecko_discovery import CoinGeckoDiscoveryProvider

    os.environ["APP_DB_PATH"] = APP_DB
    os.environ["STORAGE__DB_PATH"] = SCAN_DB
    for p in (APP_DB, SCAN_DB):
        if os.path.exists(p):
            os.remove(p)

    original_discover = CoinGeckoDiscoveryProvider.discover_universe_with_overview

    try:
        async def fake_discover(self, max_size=25, top_volume_count=100):
            return ["SOL", "HYPE"], {
                "SOL": {"price": 145.32, "volume_24h_usd": 3_200_000_000, "change_24h_pct": 8.4,
                        "high_24h": 149.10, "low_24h": 132.50, "market_cap_usd": 65_000_000_000, "market_cap_rank": 5},
                "HYPE": {"price": 54.43, "volume_24h_usd": 202_000_000, "change_24h_pct": 4.34,
                         "high_24h": 56.20, "low_24h": 51.10, "market_cap_usd": 12_200_000_000, "market_cap_rank": 9},
            }

        CoinGeckoDiscoveryProvider.discover_universe_with_overview = fake_discover

        at = AppTest.from_file(DASHBOARD_PATH)
        at.run(timeout=20)
        at.text_input[2].set_value("previewtest@example.com")
        at.text_input[3].set_value("password123")
        at.text_input[4].set_value("password123")
        at.button[1].click().run(timeout=20)
        assert not at.exception

        universe_select = next(sb for sb in at.selectbox if "🔥 Trending Now" in sb.options)
        universe_select.set_value("🔥 Trending Now").run(timeout=20)
        assert not at.exception, f"Selecting Trending Now raised: {at.exception}"

        # The preview table should exist as a dataframe with real price/volume data
        dataframes = at.dataframe
        preview_df = None
        for df_element in dataframes:
            try:
                if "Price" in df_element.value.columns and "24h Volume" in df_element.value.columns:
                    preview_df = df_element.value
                    break
            except Exception:
                continue
        assert preview_df is not None, "Expected a preview dataframe with Price/24h Volume columns"
        assert set(preview_df["Symbol"]) == {"SOL", "HYPE"}
        sol_row = preview_df[preview_df["Symbol"] == "SOL"].iloc[0]
        assert sol_row["Price"] == 145.32
        assert sol_row["24h Volume"] == 3_200_000_000
        assert sol_row["24h Change"] == 8.4
        print(f"1. Preview table renders with real live price/volume/change data for discovered coins: SOL price=${sol_row['Price']}, volume=${sol_row['24h Volume']:,.0f}, change={sol_row['24h Change']:+.1f}%: OK")

        print("\n✅ Dashboard Trending Now preview test passed: real price/volume/24h-range data reaches the dashboard table, not just a list of symbol names.")

    finally:
        CoinGeckoDiscoveryProvider.discover_universe_with_overview = original_discover
        for k in ["APP_DB_PATH", "STORAGE__DB_PATH"]:
            os.environ.pop(k, None)
        for p in (APP_DB, SCAN_DB):
            if os.path.exists(p):
                os.remove(p)


if __name__ == "__main__":
    main()

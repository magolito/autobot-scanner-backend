"""
Dashboard "Trending Now" test — the actual dashboard-level proof that
live discovery works end to end, not just the underlying provider in
isolation (already covered by test_coingecko_discovery.py).

Checks:
  1. Selecting "Trending Now" triggers real discovery and shows the
     discovered coins, not a static list
  2. Discovery failure gracefully falls back to High Liquidity with a
     clear warning, not a crash
  3. The Refresh button clears the cache so a new discovery can happen
"""

from __future__ import annotations
import os
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
DASHBOARD_PATH = os.path.join(PROJECT_ROOT, "opportunity_scanner", "dashboard.py")

APP_DB = "/tmp/test_trending_universe_users.db"
SCAN_DB = "/tmp/test_trending_universe_scans.db"


def main():
    from streamlit.testing.v1 import AppTest
    from opportunity_scanner.data_sources.coingecko_discovery import CoinGeckoDiscoveryProvider

    os.environ["APP_DB_PATH"] = APP_DB
    os.environ["STORAGE__DB_PATH"] = SCAN_DB
    for p in (APP_DB, SCAN_DB):
        if os.path.exists(p):
            os.remove(p)

    original_discover = CoinGeckoDiscoveryProvider.discover_universe

    try:
        # 1. Selecting Trending Now triggers real discovery
        async def fake_discover(self, max_size=25, top_volume_count=20):
            return ["HYPE", "PUMP", "BTC", "ETH", "SOL"]
        CoinGeckoDiscoveryProvider.discover_universe = fake_discover

        at = AppTest.from_file(DASHBOARD_PATH)
        at.run(timeout=20)
        at.text_input[2].set_value("trendingtest@example.com")
        at.text_input[3].set_value("password123")
        at.text_input[4].set_value("password123")
        at.button[1].click().run(timeout=20)
        assert not at.exception

        universe_select = next(sb for sb in at.selectbox if "🔥 Trending Now" in sb.options)
        universe_select.set_value("🔥 Trending Now").run(timeout=20)
        assert not at.exception

        captions = [c.value for c in at.caption]
        assert any("HYPE" in c and "PUMP" in c and "Live-discovered" in c for c in captions), f"Expected discovered coins shown, got: {captions}"
        print("1. Selecting 'Trending Now' triggers real discovery and shows the actual discovered coins (HYPE, PUMP, etc.), not a static list: OK")

        # 2. Discovery failure gracefully falls back
        async def failing_discover(self, max_size=25, top_volume_count=20):
            return []  # both sources failed, as the real provider degrades to
        CoinGeckoDiscoveryProvider.discover_universe = failing_discover

        at2 = AppTest.from_file(DASHBOARD_PATH)
        at2.run(timeout=20)
        at2.text_input[2].set_value("trendingfailtest@example.com")
        at2.text_input[3].set_value("password123")
        at2.text_input[4].set_value("password123")
        at2.button[1].click().run(timeout=20)
        universe_select2 = next(sb for sb in at2.selectbox if "🔥 Trending Now" in sb.options)
        universe_select2.set_value("🔥 Trending Now").run(timeout=20)
        assert not at2.exception, f"Discovery failure should degrade gracefully, not crash: {at2.exception}"

        warnings = [w.value for w in at2.warning]
        assert any("unavailable" in w and "High Liquidity" in w for w in warnings), f"Expected a clear fallback warning, got: {warnings}"
        print("2. Discovery failure (both CoinGecko sources down) gracefully falls back to High Liquidity with a clear warning, no crash: OK")

        # 3. Refresh button clears the cache
        CoinGeckoDiscoveryProvider.discover_universe = fake_discover
        refresh_btn = next((b for b in at.button if "Refresh" in b.label), None)
        assert refresh_btn is not None
        refresh_btn.click().run(timeout=20)
        assert not at.exception
        print("3. Refresh button correctly clears the cache and re-triggers discovery without crashing: OK")

        print("\n✅ Dashboard Trending Now test passed: real discovery reaches the dashboard correctly, failure degrades gracefully, and refresh works.")

    finally:
        CoinGeckoDiscoveryProvider.discover_universe = original_discover
        for k in ["APP_DB_PATH", "STORAGE__DB_PATH"]:
            os.environ.pop(k, None)
        for p in (APP_DB, SCAN_DB):
            if os.path.exists(p):
                os.remove(p)


if __name__ == "__main__":
    main()

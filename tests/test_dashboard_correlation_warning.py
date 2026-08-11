"""
Dashboard correlation warning test — the direct "these aren't
independent opportunities" fix, verified through a real dashboard run.
"""

from __future__ import annotations
import os
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
DASHBOARD_PATH = os.path.join(PROJECT_ROOT, "opportunity_scanner", "dashboard.py")

APP_DB = "/tmp/test_corr_warning_users.db"
SCAN_DB = "/tmp/test_corr_warning_scans.db"


def make_results(correlated: bool):
    from opportunity_scanner.models import ScanResult, FactorResult
    factors = lambda: {n: FactorResult(name=n, score=85, reasons=["t"], available=True) for n in ["strength", "oi_dynamics", "momentum", "social"]}
    peers = ["ETH", "SOL"] if correlated else []
    results = []
    for base in ["BTC", "ETH", "SOL"]:
        results.append(ScanResult(
            symbol=f"{base}/USDT", base=base, price=100, composite_score=85, confidence=80,
            confidence_label="High", signal="Strong Buy", factors=factors(),
            weights_used={"strength": 0.25, "oi_dynamics": 0.25, "momentum": 0.25, "social": 0.25},
            reasons_summary=["t"], risk_tier="core", passed_filters=True,
            correlated_peers=[p for p in ["BTC", "ETH", "SOL"] if p != base] if correlated else [],
        ))
    return results


def main():
    from streamlit.testing.v1 import AppTest
    from opportunity_scanner.scanner import OpportunityScanner
    from opportunity_scanner.data_sources.coingecko_discovery import CoinGeckoDiscoveryProvider

    os.environ["APP_DB_PATH"] = APP_DB
    os.environ["STORAGE__DB_PATH"] = SCAN_DB
    for p in (APP_DB, SCAN_DB):
        if os.path.exists(p):
            os.remove(p)

    async def fake_overview(self, top_n=250):
        return {b: {"market_cap_rank": 5, "market_cap_usd": 10_000_000_000, "volume_24h_usd": 1_000_000_000,
                    "price": 100, "change_24h_pct": 1.0, "high_24h": 105, "low_24h": 95} for b in ["BTC", "ETH", "SOL"]}

    original_overview = CoinGeckoDiscoveryProvider.get_market_overview
    original_scan_many = OpportunityScanner.scan_many
    CoinGeckoDiscoveryProvider.get_market_overview = fake_overview

    try:
        # 1. Correlated case — warning should appear
        async def fake_scan_many_correlated(self, bases, **kwargs):
            return make_results(correlated=True)
        OpportunityScanner.scan_many = fake_scan_many_correlated

        at = AppTest.from_file(DASHBOARD_PATH)
        at.run(timeout=20)
        at.text_input[2].set_value("corrwarntest@example.com")
        at.text_input[3].set_value("password123")
        at.text_input[4].set_value("password123")
        at.button[1].click().run(timeout=20)
        assert not at.exception

        scan_btn = next(b for b in at.button if "Scan Now" in b.label)
        scan_btn.click().run(timeout=25)
        assert not at.exception, f"Scan raised: {at.exception}"

        warnings = [w.value for w in at.warning]
        assert any("highly correlated" in w and "not" in w and "independent" in w for w in warnings), \
            f"Expected the correlation warning for 3 mutually correlated Ready-tier results, got warnings: {warnings}"
        print(f"1. Three mutually correlated high-conviction results correctly trigger the warning: '{[w for w in warnings if 'correlated' in w][0]}': OK")

        # 2. Uncorrelated case — no warning
        async def fake_scan_many_independent(self, bases, **kwargs):
            return make_results(correlated=False)
        OpportunityScanner.scan_many = fake_scan_many_independent

        at2 = AppTest.from_file(DASHBOARD_PATH)
        at2.run(timeout=20)
        at2.text_input[2].set_value("corrwarntest2@example.com")
        at2.text_input[3].set_value("password123")
        at2.text_input[4].set_value("password123")
        at2.button[1].click().run(timeout=20)
        scan_btn2 = next(b for b in at2.button if "Scan Now" in b.label)
        scan_btn2.click().run(timeout=25)
        assert not at2.exception

        warnings2 = [w.value for w in at2.warning]
        assert not any("highly correlated" in w for w in warnings2), f"Expected NO correlation warning for genuinely independent results, got: {warnings2}"
        print("2. Three genuinely independent (uncorrelated) high-conviction results correctly show NO warning: OK")

        print("\n✅ Dashboard correlation warning test passed: the actual 'these aren't independent bets' warning renders when it should, and stays silent when results are genuinely independent.")

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

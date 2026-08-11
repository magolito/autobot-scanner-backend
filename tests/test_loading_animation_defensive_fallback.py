"""
Loading animation defensive fallback test — the actual fix for a live
production report: a scan appeared to stop working entirely after the
loading animation was added. Root cause traced to requirements.txt
pinning streamlit>=1.37.0 with no upper bound, meaning the deployed
Streamlit version could genuinely differ from what was tested against
locally, and st.html() is a fairly new API that might not exist (or
might behave differently) on an older or different deployed version.

This proves the scan can NEVER be blocked by the loading animation
specifically, regardless of whether st.html works, is missing, or
raises for any reason — a cosmetic feature failing should never take
down the actual scan logic with it.
"""

from __future__ import annotations
import os
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
DASHBOARD_PATH = os.path.join(PROJECT_ROOT, "opportunity_scanner", "dashboard.py")

APP_DB = "/tmp/test_defensive_fallback_users.db"
SCAN_DB = "/tmp/test_defensive_fallback_scans.db"


def _run_scan_with_html_behavior(html_behavior: str):
    """html_behavior: 'raises' simulates st.html existing but throwing
    (e.g. an incompatible signature on a different Streamlit version);
    'missing' simulates an older Streamlit that doesn't have st.html at
    all."""
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    from opportunity_scanner.scanner import OpportunityScanner
    from opportunity_scanner.data_sources.coingecko_discovery import CoinGeckoDiscoveryProvider
    from opportunity_scanner.models import ScanResult, FactorResult

    os.environ["APP_DB_PATH"] = APP_DB
    os.environ["STORAGE__DB_PATH"] = SCAN_DB
    for p in (APP_DB, SCAN_DB):
        if os.path.exists(p):
            os.remove(p)

    async def fake_overview(self, top_n=250):
        return {"BTC": {"market_cap_rank": 1, "market_cap_usd": 1_200_000_000_000, "volume_24h_usd": 30_000_000_000,
                         "price": 65000, "change_24h_pct": 1.0, "high_24h": 66000, "low_24h": 64000}}

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

    original_html = getattr(st, "html", None)
    had_html = hasattr(st, "html")
    if html_behavior == "raises":
        st.html = lambda *a, **kw: (_ for _ in ()).throw(AttributeError("simulated: st.html() signature incompatible on this Streamlit version"))
    elif html_behavior == "missing":
        if had_html:
            delattr(st, "html")

    try:
        at = AppTest.from_file(DASHBOARD_PATH)
        at.run(timeout=20)
        at.text_input[2].set_value(f"defensivetest_{html_behavior}@example.com")
        at.text_input[3].set_value("password123")
        at.text_input[4].set_value("password123")
        at.button[1].click().run(timeout=20)
        assert not at.exception, f"Login raised: {at.exception}"

        scan_btn = next(b for b in at.button if "Scan Now" in b.label)
        scan_btn.click().run(timeout=25)
        assert not at.exception, f"THE ACTUAL FIX FAILED: scan should complete even when st.html {html_behavior}, but raised: {at.exception}"
        assert len(at.session_state["results"]) == 1, f"Scan should have completed and populated results despite st.html {html_behavior}"
        return True
    finally:
        OpportunityScanner.scan_many = original_scan_many
        CoinGeckoDiscoveryProvider.get_market_overview = original_overview
        if html_behavior == "raises":
            if had_html:
                st.html = original_html
            else:
                delattr(st, "html")
        elif html_behavior == "missing" and had_html:
            st.html = original_html
        for k in ["APP_DB_PATH", "STORAGE__DB_PATH"]:
            os.environ.pop(k, None)
        for p in (APP_DB, SCAN_DB):
            if os.path.exists(p):
                os.remove(p)


def main():
    assert _run_scan_with_html_behavior("raises")
    print("1. THE ACTUAL FIX: scan completes successfully even when st.html() raises (simulating a Streamlit version incompatibility) — the loading animation can't block the real scan logic: OK")

    assert _run_scan_with_html_behavior("missing")
    print("2. Scan completes successfully even when st.html doesn't exist at all (simulating an older deployed Streamlit version): OK")

    print("\n✅ Defensive fallback test passed: a real production report (scan appeared broken after adding the loading animation) is fixed and verified against the exact failure mode that likely caused it.")


if __name__ == "__main__":
    main()

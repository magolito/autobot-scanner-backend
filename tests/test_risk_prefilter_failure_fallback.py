"""
Risk pre-filter failure fallback test — the actual root cause of a live
report: a scan completing almost instantly with zero results and no
error shown, confirmed via a screenshot showing "LAST SCAN" set but
every other panel (results, regime, signal counts) simultaneously
showing "no scan yet."

Root cause: if the market cap overview fetch fails entirely (network
issue, CoinGecko rate limit, anything), every coin gets classified
"high_risk" (classify_risk_tier's correct, honest default with no
data), and with the default "core only" risk filter, EVERY coin got
silently excluded before the scan even started — scan_many([])
correctly returns [] almost instantly, with nothing telling the user
why. The exact same class of bug already fixed elsewhere this session
(a filter failing toward "exclude everything" instead of "we can't
verify, so don't block"), newly introduced by the risk pre-filter
feature itself.
"""

from __future__ import annotations
import os
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
DASHBOARD_PATH = os.path.join(PROJECT_ROOT, "opportunity_scanner", "dashboard.py")

APP_DB = "/tmp/test_prefilter_failure_users.db"
SCAN_DB = "/tmp/test_prefilter_failure_scans.db"


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

    captured_bases = {}

    # THE EXACT FAILURE CONDITION: market cap overview fetch fails
    # entirely, returning a completely empty dict — exactly what
    # get_market_overview() degrades to on a real network/API failure.
    async def failing_overview(self, top_n=250):
        return {}

    async def fake_scan_many(self, bases, **kwargs):
        captured_bases["bases_passed_to_scan"] = list(bases)
        factors = {n: FactorResult(name=n, score=60, reasons=["t"], available=True) for n in ["strength", "oi_dynamics", "momentum", "social"]}
        return [
            ScanResult(
                symbol=f"{b}/USDT", base=b, price=100.0, composite_score=70, confidence=65,
                confidence_label="Medium", signal="Buy", factors=factors,
                weights_used={"strength": 0.26, "oi_dynamics": 0.34, "momentum": 0.30, "social": 0.10},
                reasons_summary=["t"], risk_tier="core", passed_filters=True,
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
        at.text_input[2].set_value("prefilterfailuretest@example.com")
        at.text_input[3].set_value("password123")
        at.text_input[4].set_value("password123")
        at.button[1].click().run(timeout=20)
        assert not at.exception, f"Login raised: {at.exception}"

        # Risk tier filter defaults to ["core"] already — the exact
        # condition from the screenshot, no need to change it further.
        scan_btn = next(b for b in at.button if "Scan Now" in b.label)
        scan_btn.click().run(timeout=25)
        assert not at.exception, f"Scan raised: {at.exception}"

        # 1. THE ACTUAL FIX: the universe passed to scan_many should NOT
        # be empty, despite the market cap fetch having failed entirely
        # and the risk filter being set to "core only"
        bases_scanned = captured_bases.get("bases_passed_to_scan", [])
        assert len(bases_scanned) > 0, (
            f"THE ACTUAL BUG: with a failed market cap fetch and risk_filter=['core'], the universe "
            f"was silently reduced to empty — got {bases_scanned}. Should have fallen back to the full "
            f"unfiltered universe instead of scanning nothing."
        )
        print(f"1. THE ACTUAL FIX CONFIRMED: with a failed market cap fetch, the full universe ({len(bases_scanned)} coins) is scanned, not silently reduced to zero: OK")

        # 2. Real results should exist afterward, not an empty "no scans yet" state
        assert len(at.session_state["results"]) > 0, "Should have real results, not an empty list, matching the screenshot's contradictory 'scan happened but nothing shows' state"
        print(f"2. Real results ({len(at.session_state['results'])}) populate — no more of the 'LAST SCAN is set but everything shows empty' contradiction from the screenshot: OK")

        # 3. The user should SEE a real warning explaining what happened, not silence
        markdown_texts = " ".join(m.value for m in at.markdown)
        warning_texts = " ".join(str(w.value) if hasattr(w, "value") else str(w) for w in at.warning) if hasattr(at, "warning") else ""
        combined = markdown_texts + warning_texts
        assert "Couldn't verify market cap" in combined or "couldn't verify" in combined.lower(), \
            "Expected a real, visible warning explaining the market cap fetch failure, not silence"
        print("3. A real, visible warning explains what happened — not silent zero results: OK")

        print("\n✅ Risk pre-filter failure fallback test passed: the exact bug from the live screenshot is fixed and verified — a failed market cap fetch now degrades to permissive (scan everything, with a visible warning) instead of silently scanning nothing.")

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

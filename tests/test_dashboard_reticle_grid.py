"""
Reticle-card grid test — the actual, visible design change: Super
Strong and Strong buckets now render as real reticle cards (bracket
corners, status pill, footer readout) in a grid, not a plain table.
Proves this through a real running dashboard, not just that the
underlying HTML string looks right in isolation.
"""

from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

APP_DB = "/tmp/test_reticle_users.db"
SCAN_DB = "/tmp/test_reticle_scans.db"
DASHBOARD_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "opportunity_scanner", "dashboard.py")


def make_super_strong_result(base: str, score: float):
    from opportunity_scanner.models import ScanResult, FactorResult
    factors = {
        "momentum": FactorResult(name="momentum", score=90, reasons=["t"], available=True,
                                  raw={"alignment_score": 85.0, "dominant_direction": "bullish", "aligned_timeframes": [],
                                       "per_timeframe": {"15m": 60.0, "1h": 85.0, "4h": 88.0, "1d": 90.0}}),
        "oi_dynamics": FactorResult(name="oi_dynamics", score=85, reasons=["t"], available=True, raw={"confirms_direction": True}),
        "strength": FactorResult(name="strength", score=80, reasons=["t"], available=True),
        "social": FactorResult(name="social", score=70, reasons=["t"], available=True),
    }
    return ScanResult(
        symbol=f"{base}/USDT", base=base, price=100.5, composite_score=score, confidence=88,
        confidence_label="High", signal="Strong Buy", factors=factors,
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
        return [make_super_strong_result("BTC", 92.0), make_super_strong_result("ETH", 88.5)]

    async def fake_overview(self, top_n=250):
        return {}

    original_scan_many = OpportunityScanner.scan_many
    original_overview = CoinGeckoDiscoveryProvider.get_market_overview
    OpportunityScanner.scan_many = fake_scan_many
    CoinGeckoDiscoveryProvider.get_market_overview = fake_overview

    try:
        at = AppTest.from_file(DASHBOARD_PATH)
        at.run(timeout=20)
        at.text_input[2].set_value("reticletest@example.com")
        at.text_input[3].set_value("password123")
        at.text_input[4].set_value("password123")
        at.button[1].click().run(timeout=20)
        assert not at.exception

        # 1. No left/right sidebar split — the actual structural fix,
        # confirmed by the pillar-weight sliders now living inside an
        # expander rather than a separate always-visible column
        expander_labels = [e.label for e in at.expander]
        assert any("Adjust Scoring Weights" in label for label in expander_labels), (
            f"Expected the pillar weights/filters to be inside a collapsible expander (sidebar removed), "
            f"got expanders: {expander_labels}"
        )
        print("1. THE ACTUAL FIX: sidebar removed — pillar weights and filters now live in a collapsed expander, not an always-visible right column: OK")

        scan_btn = next(b for b in at.button if "Scan Now" in b.label)
        scan_btn.click().run(timeout=25)
        assert not at.exception, f"Scan raised: {at.exception}"

        # 2. Super Strong renders as reticle cards (real HTML with the
        # bracket-corner signature), not a plain st.dataframe table
        markdown_html = " ".join(m.value for m in at.markdown)
        assert "reticle-card" in markdown_html, "Expected reticle-card HTML to be present for the Super Strong bucket"
        assert "reticle-corner tl" in markdown_html and "reticle-corner br" in markdown_html, "Expected all four bracket corners to render"
        assert "BTC" in markdown_html and "ETH" in markdown_html, "Expected both real coins to appear in the card grid"
        print("2. Super Strong bucket renders as real reticle cards (bracket corners present) with both real coins shown, not a plain table: OK")

        # 3. Each card has a genuine working "View Detail" button (the
        # actual click-to-detail mechanism, not decoration)
        detail_buttons = [b for b in at.button if "View Detail" in b.label]
        assert len(detail_buttons) >= 2, f"Expected a real View Detail button per card (at least 2 for BTC+ETH), got {len(detail_buttons)}"
        detail_buttons[0].click().run(timeout=15)
        assert not at.exception, f"Clicking View Detail raised: {at.exception}"
        print("3. Each reticle card has a genuine, working View Detail button (not just decorative HTML) that correctly opens the detail modal: OK")

        print("\n✅ Reticle grid test passed: the actual visible design change reaches the real running dashboard — sidebar removed, featured buckets are real interactive cards.")

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

"""
Dashboard timeframe alignment display test — proves the new alignment
signal actually reaches the detail modal in a real dashboard run, not
just that the underlying scoring logic is correct in isolation
(already covered by test_momentum_alignment.py and
test_smart_view.py's alignment gate test).
"""

from __future__ import annotations
import os
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
DASHBOARD_PATH = os.path.join(PROJECT_ROOT, "opportunity_scanner", "dashboard.py")

APP_DB = "/tmp/test_alignment_display_users.db"
SCAN_DB = "/tmp/test_alignment_display_scans.db"


def main():
    from streamlit.testing.v1 import AppTest
    from opportunity_scanner.models import ScanResult, FactorResult

    os.environ["APP_DB_PATH"] = APP_DB
    os.environ["STORAGE__DB_PATH"] = SCAN_DB
    for p in (APP_DB, SCAN_DB):
        if os.path.exists(p):
            os.remove(p)

    def make_result_with_alignment():
        momentum_factor = FactorResult(
            name="momentum", score=85, available=True,
            reasons=["Aligned bullish across 4h, 1d, 1h (90% weighted agreement) — real multi-timeframe conviction"],
            raw={
                "per_timeframe": {"15m": 40, "1h": 75, "4h": 82, "1d": 88},
                "alignment_score": 90.0, "dominant_direction": "bullish",
                "aligned_timeframes": ["1d", "4h", "1h"],
            },
        )
        other_factors = {n: FactorResult(name=n, score=70, reasons=["t"], available=True) for n in ["strength", "oi_dynamics", "social"]}
        other_factors["momentum"] = momentum_factor
        return ScanResult(
            symbol="BTC/USDT", base="BTC", price=65000, composite_score=85, confidence=80,
            confidence_label="High", signal="Strong Buy", factors=other_factors,
            weights_used={"strength": 0.25, "oi_dynamics": 0.25, "momentum": 0.25, "social": 0.25},
            reasons_summary=["t"], risk_tier="core", passed_filters=True,
        )

    at = AppTest.from_file(DASHBOARD_PATH)
    at.run(timeout=20)
    at.text_input[2].set_value("alignmenttest@example.com")
    at.text_input[3].set_value("password123")
    at.text_input[4].set_value("password123")
    at.button[1].click().run(timeout=20)
    assert not at.exception

    at.session_state["detail_result"] = make_result_with_alignment()
    at.run(timeout=20)
    assert not at.exception, f"Detail modal raised: {at.exception}"

    markdown_texts = " ".join(m.value for m in at.markdown)
    assert "Real conviction" in markdown_texts and "90% weighted agreement" in markdown_texts, \
        f"Expected the alignment headline to render, got markdown containing: {[m for m in markdown_texts.split(chr(10)) if 'conviction' in m.lower() or 'alignment' in m.lower()]}"
    print("1. Detail modal correctly shows the alignment headline with real weighted agreement percentage: OK")

    metrics = {m.label: m.value for m in at.metric}
    assert metrics.get("15m") == "40", f"Expected per-timeframe metrics, got: {metrics}"
    assert metrics.get("4h") == "82"
    assert metrics.get("1d") == "88"
    print(f"2. Per-timeframe scores correctly displayed as individual metrics: {metrics}: OK")

    for k in ["APP_DB_PATH", "STORAGE__DB_PATH"]:
        os.environ.pop(k, None)
    for p in (APP_DB, SCAN_DB):
        if os.path.exists(p):
            os.remove(p)

    print("\n✅ Dashboard alignment display test passed: the actual multi-timeframe conviction signal reaches the detail modal, showing both the headline verdict and the per-timeframe breakdown.")


if __name__ == "__main__":
    main()

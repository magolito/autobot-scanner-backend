"""
Dashboard regime leadership flag test — proves the new "genuine relative
strength during Risk-Off, no dampening applied" note renders as a
distinct positive flag (green), not lumped in with the generic warning
styling used for an actual dampening event. Direct fix for treating
"real divergence from BTC" identically to "correlated beta lag" — they
now look visually different too, not just score differently.
"""

from __future__ import annotations
import os
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
DASHBOARD_PATH = os.path.join(PROJECT_ROOT, "opportunity_scanner", "dashboard.py")

APP_DB = "/tmp/test_regime_flag_users.db"
SCAN_DB = "/tmp/test_regime_flag_scans.db"


def main():
    from streamlit.testing.v1 import AppTest
    from opportunity_scanner.models import ScanResult, FactorResult

    os.environ["APP_DB_PATH"] = APP_DB
    os.environ["STORAGE__DB_PATH"] = SCAN_DB
    for p in (APP_DB, SCAN_DB):
        if os.path.exists(p):
            os.remove(p)

    factors = {n: FactorResult(name=n, score=70, reasons=["t"], available=True) for n in ["strength", "oi_dynamics", "momentum", "social"]}
    leader_result = ScanResult(
        symbol="SOL/USDT", base="SOL", price=150, composite_score=78, confidence=70,
        confidence_label="High", signal="Buy", factors=factors,
        weights_used={"strength": 0.25, "oi_dynamics": 0.25, "momentum": 0.25, "social": 0.25},
        reasons_summary=["t"], risk_tier="core", passed_filters=True,
        regime_label="Risk-Off", regime_score=20.0,
        regime_adjustment_note=(
            "Regime is Risk-Off, but relative strength vs BTC/sector is genuinely strong "
            "(rs_score 85/100) — this looks like real divergence, not correlated beta, so "
            "no dampening applied. Worth attention as a potential leader if the regime turns."
        ),
    )

    at = AppTest.from_file(DASHBOARD_PATH)
    at.run(timeout=20)
    at.text_input[2].set_value("regimeflagtest@example.com")
    at.text_input[3].set_value("password123")
    at.text_input[4].set_value("password123")
    at.button[1].click().run(timeout=20)
    assert not at.exception

    at.session_state["detail_result"] = leader_result
    at.run(timeout=20)
    assert not at.exception, f"Detail modal raised: {at.exception}"

    markdown_texts = " ".join(m.value for m in at.markdown)
    assert "flag-positive" in markdown_texts, f"Expected the leadership note to render with the distinct 'flag-positive' (green) styling, not lumped in with warnings"
    assert "flag-warning" not in [m.value for m in at.markdown if "no dampening applied" in m.value][0] if any("no dampening applied" in m.value for m in at.markdown) else True
    assert "potential leader" in markdown_texts
    print("1. Real relative-strength divergence during Risk-Off correctly renders with distinct positive (green) styling, not the generic warning treatment: OK")

    for k in ["APP_DB_PATH", "STORAGE__DB_PATH"]:
        os.environ.pop(k, None)
    for p in (APP_DB, SCAN_DB):
        if os.path.exists(p):
            os.remove(p)

    print("\n✅ Dashboard regime leadership flag test passed: the fix is visible, not just computed — a real divergence signal looks meaningfully different from a real warning.")


if __name__ == "__main__":
    main()

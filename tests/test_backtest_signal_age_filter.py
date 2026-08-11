"""
Backtest signal age-filter test — a real bug found while building the
readiness track record: _backtest_signal_sync had NO age filtering at
all, meaning a "Strong Buy" that fired 5 minutes ago got backtested
against essentially its own entry price (~0% return either way),
diluting the win rate/avg return numbers already shown on the
dashboard with noise from signals that hadn't had any real time to
play out.
"""

from __future__ import annotations
import sys, os
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.storage import ScanStorage


def _iso(hours_ago=0):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def main():
    db_path = "/tmp/test_backtest_age_filter.db"
    if os.path.exists(db_path):
        os.remove(db_path)

    storage = ScanStorage(db_path)
    conn = storage._connect()

    def insert(base, signal, price, hours_ago, prev_signal="Neutral"):
        conn.execute(
            "INSERT INTO scan_results (scanned_at, base, symbol, price, composite_score, confidence, "
            "confidence_label, signal, risk_tier, weights_used_json, reasons_summary_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (_iso(hours_ago), base, f"{base}/USDT", price, 80.0, 75.0, "High", signal, "core", "{}", "[]"),
        )

    # A genuinely OLD "Strong Buy" entry (48h ago) — should count
    insert("BTC", "Neutral", 100.0, hours_ago=50)
    insert("BTC", "Strong Buy", 100.0, hours_ago=48)  # entry point
    insert("BTC", "Strong Buy", 110.0, hours_ago=2)  # later resolving price

    # A "Strong Buy" that fired only 1 HOUR ago — under the 24h default
    # threshold, should be EXCLUDED, not counted as a near-0% result
    insert("ETH", "Neutral", 200.0, hours_ago=3)
    insert("ETH", "Strong Buy", 200.0, hours_ago=1)  # too recent — should be excluded
    conn.commit()
    conn.close()

    result = storage.backtest_signal_sync("Strong Buy", lookback_days=30)
    assert result["sample_size"] == 1, f"THE ACTUAL FIX: expected only the genuinely old BTC entry to count (ETH is too recent), got sample_size={result['sample_size']}"
    assert result["entries"][0]["base"] == "BTC"
    print(f"1. THE ACTUAL FIX: a 'Strong Buy' that fired only 1 hour ago is correctly EXCLUDED (too recent to judge), only the genuinely 48h-old BTC entry counts: OK")

    assert abs(result["avg_forward_return_pct"] - 10.0) < 0.01, f"Expected +10% (BTC's real move), got {result['avg_forward_return_pct']}"
    print(f"2. Avg return correctly reflects only the genuinely resolved entry (+10%), not diluted by the too-recent ETH signal: OK")

    os.remove(db_path)
    print("\n✅ Backtest age-filter test passed: signals too young to judge are correctly excluded, not silently counted as ~0% noise.")


if __name__ == "__main__":
    main()

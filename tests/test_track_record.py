"""
Track record test — the actual "does this signal mean anything" logic,
verified precisely. Uses a raw synthetic SQLite database with full
control over timestamps, since the age/lookback filtering is exactly
the kind of thing that's easy to get subtly wrong.
"""

from __future__ import annotations
import sqlite3
import sys, os
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.track_record import compute_track_record

SCHEMA = """
CREATE TABLE scan_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scanned_at TEXT NOT NULL, base TEXT NOT NULL, symbol TEXT NOT NULL, price REAL NOT NULL,
    composite_score REAL NOT NULL, confidence REAL NOT NULL, confidence_label TEXT NOT NULL,
    signal TEXT NOT NULL, risk_tier TEXT NOT NULL, strength_score REAL, oi_dynamics_score REAL,
    momentum_score REAL, social_score REAL, weights_used_json TEXT NOT NULL, reasons_summary_json TEXT NOT NULL,
    readiness_label TEXT, readiness_direction TEXT
);
"""


def _insert(conn, base, price, scanned_at, readiness_label="Ready", direction="bullish"):
    conn.execute(
        "INSERT INTO scan_results (scanned_at, base, symbol, price, composite_score, confidence, confidence_label, "
        "signal, risk_tier, weights_used_json, reasons_summary_json, readiness_label, readiness_direction) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (scanned_at, base, f"{base}/USDT", price, 80.0, 75.0, "High", "Strong Buy", "core", "{}", "[]", readiness_label, direction),
    )


def _iso(hours_ago=0, days_ago=0):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago, days=days_ago)).isoformat()


def main():
    db_path = "/tmp/test_track_record.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    # 1. A resolved WINNING bullish signal: signaled 48h ago at $100, later scan at $110 (+10%)
    _insert(conn, "BTC", 100.0, _iso(hours_ago=48), "Ready", "bullish")
    _insert(conn, "BTC", 110.0, _iso(hours_ago=2), "Building", "bullish")  # the later resolving scan

    # 2. A resolved LOSING bullish signal: signaled 48h ago at $100, later scan at $90 (-10%)
    _insert(conn, "ETH", 100.0, _iso(hours_ago=48), "Ready", "bullish")
    _insert(conn, "ETH", 90.0, _iso(hours_ago=2), "Building", "bullish")

    # 3. A resolved WINNING bearish signal: signaled 48h ago at $100, later scan at $85 (price fell, correct for a short)
    _insert(conn, "SOL", 100.0, _iso(hours_ago=48), "Ready", "bearish")
    _insert(conn, "SOL", 85.0, _iso(hours_ago=2), "Building", "bearish")

    # 4. An UNRESOLVED signal: signaled 48h ago, no later scan exists at all
    _insert(conn, "AVAX", 100.0, _iso(hours_ago=48), "Ready", "bullish")

    # 5. A signal too RECENT (only 2h old, under the 24h min_hours_since_signal) — should be excluded entirely
    _insert(conn, "LINK", 100.0, _iso(hours_ago=2), "Ready", "bullish")

    # 6. A signal OUTSIDE the lookback window (120 days ago, beyond default 90-day lookback) — excluded entirely
    _insert(conn, "DOT", 100.0, _iso(days_ago=120), "Ready", "bullish")
    conn.commit()
    conn.close()

    summary = compute_track_record(db_path, lookback_days=90, min_hours_since_signal=24.0)

    # Check 1: total_signals should include BTC, ETH, SOL, AVAX (4) — NOT LINK (too recent) or DOT (too old)
    assert summary.total_signals == 4, f"Expected 4 signals within the valid window (excluding too-recent and too-old), got {summary.total_signals}"
    print(f"1. Correctly includes only signals within the lookback window AND old enough to judge (4 of 6 inserted): OK")

    # Check 2: resolved should be 3 (BTC, ETH, SOL), unresolved 1 (AVAX)
    assert summary.resolved_signals == 3, f"Expected 3 resolved signals (have a later price), got {summary.resolved_signals}"
    assert summary.unresolved_signals == 1, f"Expected 1 unresolved signal (AVAX, no later scan), got {summary.unresolved_signals}"
    print(f"2. Resolved (3) vs unresolved (1, no later data available yet) correctly distinguished, not conflated: OK")

    # Check 3: win rate — 2 of 3 resolved were correct (BTC win, SOL win, ETH loss) = 66.7%
    assert summary.win_rate_pct is not None
    assert abs(summary.win_rate_pct - 66.7) < 0.5, f"Expected ~66.7% win rate (2 of 3 correct), got {summary.win_rate_pct}"
    print(f"3. Win rate correctly computed from RESOLVED signals only: {summary.win_rate_pct}%: OK")

    # Check 4: avg return — BTC +10%, ETH -10%, SOL bearish correct with price -15% (return_pct is the raw price
    # change, -15%, regardless of direction — "correct" is separate from the raw return sign)
    expected_avg = (10.0 + (-10.0) + (-15.0)) / 3
    assert summary.avg_return_pct is not None
    assert abs(summary.avg_return_pct - expected_avg) < 0.1, f"Expected avg return ~{expected_avg:.2f}%, got {summary.avg_return_pct}"
    print(f"4. Average return correctly computed as the raw price change across resolved signals: {summary.avg_return_pct}%: OK")

    # Check 5: individual outcomes contain the right data for a spot-check
    btc_outcome = next(o for o in summary.outcomes if o.base == "BTC")
    assert btc_outcome.correct is True
    assert abs(btc_outcome.return_pct - 10.0) < 0.01
    avax_outcome = next(o for o in summary.outcomes if o.base == "AVAX")
    assert avax_outcome.correct is None and avax_outcome.price_now is None
    print("5. Individual outcome records correctly carry accurate per-signal data (BTC: correct win +10%, AVAX: honestly unresolved): OK")

    # 6. Empty history — no crash, sensible defaults
    empty_db = "/tmp/test_track_record_empty.db"
    if os.path.exists(empty_db):
        os.remove(empty_db)
    conn2 = sqlite3.connect(empty_db)
    conn2.executescript(SCHEMA)
    conn2.commit()
    conn2.close()
    empty_summary = compute_track_record(empty_db, lookback_days=90, min_hours_since_signal=24.0)
    assert empty_summary.total_signals == 0
    assert empty_summary.win_rate_pct is None
    assert empty_summary.avg_return_pct is None
    print("6. Empty history correctly returns zero signals with None win rate/return, not a crash or a misleading 0%: OK")

    os.remove(db_path)
    os.remove(empty_db)
    print("\n✅ Track record test passed: signal age filtering, lookback window, resolved-vs-unresolved distinction, win rate, and average return all verified precisely.")


if __name__ == "__main__":
    main()

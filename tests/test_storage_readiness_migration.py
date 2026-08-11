"""
Storage readiness-column migration test — the critical case: a database
that already exists from BEFORE this migration was written, with real
scan history already in it (exactly the situation on the live
deployment). Same pattern as AppStorage's earlier migration test.
"""

from __future__ import annotations
import sqlite3
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.storage import ScanStorage
from opportunity_scanner.models import ScanResult, FactorResult


def main():
    db_path = "/tmp/test_storage_readiness_migration.db"
    if os.path.exists(db_path):
        os.remove(db_path)

    # 1. Simulate a database created by the OLD schema (pre-migration) —
    # raw SQL matching exactly what existed before, with a real scan
    # result already in it, same as the live deployment.
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS scan_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scanned_at TEXT NOT NULL,
            base TEXT NOT NULL,
            symbol TEXT NOT NULL,
            price REAL NOT NULL,
            composite_score REAL NOT NULL,
            confidence REAL NOT NULL,
            confidence_label TEXT NOT NULL,
            signal TEXT NOT NULL,
            risk_tier TEXT NOT NULL,
            strength_score REAL,
            oi_dynamics_score REAL,
            momentum_score REAL,
            social_score REAL,
            weights_used_json TEXT NOT NULL,
            reasons_summary_json TEXT NOT NULL
        );
    """)
    conn.execute(
        "INSERT INTO scan_results (scanned_at, base, symbol, price, composite_score, confidence, confidence_label, "
        "signal, risk_tier, weights_used_json, reasons_summary_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("2026-08-01T00:00:00", "BTC", "BTC/USDT", 65000.0, 75.0, 70.0, "High", "Buy", "core", "{}", "[]"),
    )
    conn.commit()
    conn.close()
    print("1. Simulated a pre-migration database with a real existing scan result, exactly like the live deployment: OK")

    # 2. Open it with the NEW ScanStorage — the migration must run without crashing or losing data
    storage = ScanStorage(db_path)
    conn2 = sqlite3.connect(db_path)
    conn2.row_factory = sqlite3.Row
    row = conn2.execute("SELECT * FROM scan_results WHERE base = 'BTC'").fetchone()
    assert row is not None, "The existing scan result must survive the migration"
    assert row["price"] == 65000.0, "Existing data must be preserved exactly"
    print("2. Migration ran without crashing, existing scan data survived intact: OK")

    # 3. New columns exist, correctly NULL for the pre-existing row (never
    # classified — honest, not fabricated backfill)
    assert row["readiness_label"] is None, "A pre-migration row should have NULL readiness (never classified), not a fabricated backfilled value"
    print("3. Pre-existing row correctly has NULL readiness (never classified) — no fabricated backfill: OK")

    # 4. A NEW scan saved after migration correctly gets a real readiness classification
    factors = {
        "momentum": FactorResult(name="momentum", score=85, reasons=["t"], available=True,
                                  raw={"alignment_score": 75.0, "dominant_direction": "bullish", "aligned_timeframes": ["4h", "1d"]}),
        "oi_dynamics": FactorResult(name="oi_dynamics", score=70, reasons=["t"], available=True, raw={"confirms_direction": True}),
        "strength": FactorResult(name="strength", score=70, reasons=["t"], available=True),
        "social": FactorResult(name="social", score=60, reasons=["t"], available=True),
    }
    new_result = ScanResult(
        symbol="ETH/USDT", base="ETH", price=3200.0, composite_score=80, confidence=75,
        confidence_label="High", signal="Strong Buy", factors=factors,
        weights_used={"strength": 0.26, "oi_dynamics": 0.34, "momentum": 0.30, "social": 0.10},
        reasons_summary=["t"], risk_tier="core", passed_filters=True,
    )
    storage.save_scan_result_sync(new_result)
    conn3 = sqlite3.connect(db_path)
    conn3.row_factory = sqlite3.Row
    eth_row = conn3.execute("SELECT * FROM scan_results WHERE base = 'ETH'").fetchone()
    assert eth_row["readiness_label"] == "Ready", f"Expected 'Ready' classification to be persisted, got {eth_row['readiness_label']}"
    assert eth_row["readiness_direction"] == "bullish"
    print(f"4. A new scan saved after migration correctly persists real readiness classification (label={eth_row['readiness_label']}, direction={eth_row['readiness_direction']}): OK")

    # 5. Re-running the migration (app restart) on an already-migrated database is a safe no-op
    storage2 = ScanStorage(db_path)
    conn4 = sqlite3.connect(db_path)
    conn4.row_factory = sqlite3.Row
    still_there = conn4.execute("SELECT * FROM scan_results WHERE base = 'ETH'").fetchone()
    assert still_there["readiness_label"] == "Ready"
    print("5. Re-running the migration on an already-migrated database is a safe no-op: OK")

    os.remove(db_path)
    print("\n✅ Storage readiness migration test passed: the critical pre-existing-database case works correctly, honest NULL for never-classified history, real classification for new scans going forward.")


if __name__ == "__main__":
    main()

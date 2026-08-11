"""
Persistent storage — SQLite to start (swap for Postgres later if this
scales past one instance; the interface here is deliberately just a thin
wrapper around a handful of SQL statements, so that swap means changing
the connection string and a few type mappings, not the calling code).

Two things this unlocks that a stateless "fresh pull every scan" design
can't:
  1. Backtesting the SCORING MODEL ITSELF — did a "Strong Buy" from two
     weeks ago actually outperform? You can't answer that without storing
     what the scanner said at the time.
  2. OI history that survives process restarts — right now OI change is
     computed from Bybit's own historical endpoint, but storing our own
     snapshots means the option to track OI evolution correlated with
     our own scoring even where the exchange's history window is short.
"""

from __future__ import annotations
import asyncio
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from .models import ScanResult, FactorResult
from .readiness import classify_readiness

DEFAULT_DB_PATH = "opportunity_scanner.db"

SCHEMA = """
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
    reasons_summary_json TEXT NOT NULL,
    readiness_label TEXT,
    readiness_direction TEXT
);
CREATE INDEX IF NOT EXISTS idx_scan_results_base_time ON scan_results(base, scanned_at);
CREATE INDEX IF NOT EXISTS idx_scan_results_signal ON scan_results(signal, scanned_at);

CREATE TABLE IF NOT EXISTS oi_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    base TEXT NOT NULL,
    oi_usd REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oi_snapshots_base_time ON oi_snapshots(base, recorded_at);
"""


class ScanStorage:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(SCHEMA)
            conn.commit()
            self._migrate_add_column(conn, "scan_results", "readiness_label", "TEXT")
            self._migrate_add_column(conn, "scan_results", "readiness_direction", "TEXT")
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _migrate_add_column(conn: sqlite3.Connection, table: str, column: str, column_def: str):
        """
        Safe, idempotent column addition — same pattern as
        AppStorage._migrate_add_column. This matters concretely here
        too: there's a live deployment with real scan history already in
        this exact database file, and CREATE TABLE IF NOT EXISTS doesn't
        retroactively add columns to a table that already exists.
        """
        existing_columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_def}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ---------------------------------------------------------- scan results

    def _save_scan_result_sync(self, result: ScanResult):
        conn = self._connect()
        try:
            factor_scores = {name: f.score for name, f in result.factors.items()}
            readiness = classify_readiness(result)
            conn.execute(
                """
                INSERT INTO scan_results (
                    scanned_at, base, symbol, price, composite_score, confidence,
                    confidence_label, signal, risk_tier, strength_score,
                    oi_dynamics_score, momentum_score, social_score,
                    weights_used_json, reasons_summary_json,
                    readiness_label, readiness_direction
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    result.base, result.symbol, result.price,
                    result.composite_score, result.confidence, result.confidence_label,
                    result.signal, result.risk_tier,
                    factor_scores.get("strength"), factor_scores.get("oi_dynamics"),
                    factor_scores.get("momentum"), factor_scores.get("social"),
                    json.dumps(result.weights_used),
                    json.dumps(result.reasons_summary),
                    readiness["label"], readiness["direction"],
                ),
            )
            conn.commit()
        finally:
            conn.close()

    async def save_scan_result(self, result: ScanResult):
        await asyncio.to_thread(self._save_scan_result_sync, result)

    def save_scan_result_sync(self, result: ScanResult):
        """Synchronous entry point — for callers that are already sync
        (e.g. Streamlit dashboards) and shouldn't need asyncio.run() just
        to write one row. The underlying operation was always synchronous
        SQLite; the async wrapper above exists for async callers like the
        scheduler, not because the work itself needs an event loop."""
        self._save_scan_result_sync(result)

    async def save_scan_results(self, results: List[ScanResult]):
        await asyncio.gather(*[self.save_scan_result(r) for r in results])

    def save_scan_results_sync(self, results: List[ScanResult]):
        for r in results:
            self._save_scan_result_sync(r)

    def _get_history_sync(self, base: str, limit: int) -> List[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM scan_results WHERE base = ? ORDER BY scanned_at DESC LIMIT ?",
                (base.upper(), limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def get_history(self, base: str, limit: int = 100) -> List[dict]:
        return await asyncio.to_thread(self._get_history_sync, base, limit)

    def _get_latest_scan_per_symbol_sync(self) -> List[dict]:
        """
        One row per symbol — its most recent scan, regardless of when that
        was. Deliberately per-symbol-latest rather than 'the last batch
        that ran together': if the universe changed between scan cycles
        (a symbol added/removed via blacklist/whitelist), this still
        reconstructs the best available current picture per symbol rather
        than requiring every symbol to share an identical timestamp.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT sr.* FROM scan_results sr
                INNER JOIN (
                    SELECT base, MAX(scanned_at) AS max_scanned_at
                    FROM scan_results
                    GROUP BY base
                ) latest ON sr.base = latest.base AND sr.scanned_at = latest.max_scanned_at
                ORDER BY sr.composite_score DESC
                """
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_latest_scan_per_symbol_sync(self) -> List[dict]:
        return self._get_latest_scan_per_symbol_sync()

    def _get_signal_changes_sync(self, since_iso: str) -> List[dict]:
        """Coins whose most recent signal differs from their prior signal — the raw input for Phase 5 alerts."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT base, signal, composite_score, scanned_at,
                       LAG(signal) OVER (PARTITION BY base ORDER BY scanned_at) AS prev_signal
                FROM scan_results
                WHERE scanned_at >= ?
                ORDER BY scanned_at DESC
                """,
                (since_iso,),
            ).fetchall()
            return [dict(r) for r in rows if r["prev_signal"] is not None and r["prev_signal"] != r["signal"]]
        finally:
            conn.close()

    async def get_signal_changes(self, since_iso: str) -> List[dict]:
        return await asyncio.to_thread(self._get_signal_changes_sync, since_iso)

    def get_signal_changes_sync(self, since_iso: str) -> List[dict]:
        return self._get_signal_changes_sync(since_iso)

    def _get_score_jumps_sync(self, since_iso: str, threshold: float) -> List[dict]:
        """
        Coins whose composite score moved by at least `threshold` points
        between consecutive scans, in EITHER direction — this catches
        real news even when it doesn't cross a signal-grade boundary
        (e.g. 55 -> 74 is both still technically "Neutral"-adjacent to
        "Buy" territory but represents a meaningful move worth flagging).
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT base, signal, composite_score, scanned_at,
                       LAG(composite_score) OVER (PARTITION BY base ORDER BY scanned_at) AS prev_score,
                       LAG(scanned_at) OVER (PARTITION BY base ORDER BY scanned_at) AS prev_scanned_at
                FROM scan_results
                WHERE scanned_at >= ?
                ORDER BY scanned_at DESC
                """,
                (since_iso,),
            ).fetchall()
            jumps = []
            for r in rows:
                if r["prev_score"] is None:
                    continue
                delta = r["composite_score"] - r["prev_score"]
                if abs(delta) >= threshold:
                    d = dict(r)
                    d["score_delta"] = round(delta, 1)
                    jumps.append(d)
            return jumps
        finally:
            conn.close()

    async def get_score_jumps(self, since_iso: str, threshold: float = 15.0) -> List[dict]:
        return await asyncio.to_thread(self._get_score_jumps_sync, since_iso, threshold)

    def get_score_jumps_sync(self, since_iso: str, threshold: float = 15.0) -> List[dict]:
        return self._get_score_jumps_sync(since_iso, threshold)

    # -------------------------------------------------------------- OI snapshots

    def _save_oi_snapshot_sync(self, base: str, oi_usd: float):
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO oi_snapshots (recorded_at, base, oi_usd) VALUES (?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), base.upper(), oi_usd),
            )
            conn.commit()
        finally:
            conn.close()

    async def save_oi_snapshot(self, base: str, oi_usd: float):
        await asyncio.to_thread(self._save_oi_snapshot_sync, base, oi_usd)

    def _get_oi_history_sync(self, base: str, limit: int) -> List[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM oi_snapshots WHERE base = ? ORDER BY recorded_at DESC LIMIT ?",
                (base.upper(), limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def get_oi_history(self, base: str, limit: int = 200) -> List[dict]:
        return await asyncio.to_thread(self._get_oi_history_sync, base, limit)

    # ---------------------------------------------------------------- backtesting

    def _backtest_signal_sync(self, signal: str, lookback_days: int, min_hours_since_signal: float = 24.0) -> dict:
        """
        Rough backtest: for every time a coin FIRST entered `signal` (e.g.
        "Strong Buy") within the lookback window, what was its price then
        vs. its most recent recorded price? This is intentionally simple
        (no position sizing, no fees, no slippage) -- it's a sanity check
        on whether the scoring model's calls have directionally been
        right, not a trading backtest.

        min_hours_since_signal is a real fix found while building the
        readiness track record (track_record.py): this previously had NO
        age filtering at all, meaning a signal that fired 5 minutes ago
        got backtested against essentially its own entry price (~0%
        return either way), diluting the win rate/avg return with noise
        from signals that hadn't had any real time to play out. Signals
        younger than this are correctly excluded, not counted as either
        a win or a loss.
        """
        conn = self._connect()
        try:
            since = (datetime.now(timezone.utc).timestamp() - lookback_days * 86400)
            since_iso = datetime.fromtimestamp(since, tz=timezone.utc).isoformat()
            age_cutoff_iso = (datetime.now(timezone.utc) - timedelta(hours=min_hours_since_signal)).isoformat()
            rows = conn.execute(
                """
                SELECT base, signal, price, scanned_at,
                       LAG(signal) OVER (PARTITION BY base ORDER BY scanned_at) AS prev_signal
                FROM scan_results
                WHERE scanned_at >= ?
                ORDER BY base, scanned_at
                """,
                (since_iso,),
            ).fetchall()

            entries = [
                dict(r) for r in rows
                if r["signal"] == signal and r["prev_signal"] != signal and r["scanned_at"] <= age_cutoff_iso
            ]

            results = []
            for entry in entries:
                latest = conn.execute(
                    "SELECT price FROM scan_results WHERE base = ? ORDER BY scanned_at DESC LIMIT 1",
                    (entry["base"],),
                ).fetchone()
                if latest is None or entry["price"] == 0:
                    continue
                forward_return_pct = (latest["price"] / entry["price"] - 1) * 100
                results.append({
                    "base": entry["base"], "entered_at": entry["scanned_at"],
                    "entry_price": entry["price"], "latest_price": latest["price"],
                    "forward_return_pct": forward_return_pct,
                })

            if not results:
                return {"signal": signal, "sample_size": 0, "avg_forward_return_pct": None, "win_rate_pct": None}

            avg_return = sum(r["forward_return_pct"] for r in results) / len(results)
            win_rate = sum(1 for r in results if r["forward_return_pct"] > 0) / len(results) * 100
            return {
                "signal": signal, "sample_size": len(results),
                "avg_forward_return_pct": round(avg_return, 2),
                "win_rate_pct": round(win_rate, 1),
                "entries": results,
            }
        finally:
            conn.close()

    async def backtest_signal(self, signal: str, lookback_days: int = 30, min_hours_since_signal: float = 24.0) -> dict:
        return await asyncio.to_thread(self._backtest_signal_sync, signal, lookback_days, min_hours_since_signal)

    def backtest_signal_sync(self, signal: str, lookback_days: int = 30, min_hours_since_signal: float = 24.0) -> dict:
        return self._backtest_signal_sync(signal, lookback_days, min_hours_since_signal)

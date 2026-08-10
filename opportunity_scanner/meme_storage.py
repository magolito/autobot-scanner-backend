"""
Meme scan storage — SQLite, same pattern as storage.py (the main
scanner's), but for FinalMemeResult history. Three things this powers
that nothing else can, all needing scan-over-time history:

  1. Hype event detection — "sudden spike" is meaningless without a prior
     value to compare against. This is where that prior value lives.
  2. Performance tracking — did a "Pass + high score" call actually go
     up afterward? Needs the price at scoring time and a later price.
  3. Deployer blacklist — persistent across restarts, and growable:
     a deployer whose token later gets flagged as a rug should stay
     flagged for every future token from that same wallet.
"""

from __future__ import annotations
import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from .meme_scoring_engine import FinalMemeResult

DEFAULT_MEME_DB_PATH = "meme_scanner.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meme_scan_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scanned_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    token_address TEXT NOT NULL,
    mode TEXT NOT NULL,
    safety_grade TEXT NOT NULL,
    opportunity_score REAL,
    hype_level TEXT,
    confidence REAL NOT NULL,
    price_usd REAL,
    hype_score REAL,
    onchain_score REAL,
    momentum_score REAL,
    mention_velocity_ratio REAL,
    dex_boosted INTEGER,
    kol_score REAL,
    deployer_address TEXT,
    risk_flags_json TEXT NOT NULL,
    thesis TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meme_results_token_time ON meme_scan_results(token_address, scanned_at);
CREATE INDEX IF NOT EXISTS idx_meme_results_deployer ON meme_scan_results(deployer_address);

CREATE TABLE IF NOT EXISTS deployer_blacklist (
    deployer_address TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    added_at TEXT NOT NULL,
    auto_detected INTEGER NOT NULL DEFAULT 0,
    source_token_address TEXT
);
"""


class MemeScanStorage:
    def __init__(self, db_path: str = DEFAULT_MEME_DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ---------------------------------------------------------- scan results

    def _save_result_sync(self, result: FinalMemeResult, price_usd: Optional[float], mention_velocity_ratio: Optional[float], dex_boosted: bool, kol_score: Optional[float], deployer_address: Optional[str]):
        conn = self._connect()
        try:
            pillars = result.pillar_scores
            conn.execute(
                """
                INSERT INTO meme_scan_results (
                    scanned_at, symbol, token_address, mode, safety_grade, opportunity_score,
                    hype_level, confidence, price_usd, hype_score, onchain_score, momentum_score,
                    mention_velocity_ratio, dex_boosted, kol_score, deployer_address,
                    risk_flags_json, thesis
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(), result.symbol, result.token_address, result.mode,
                    result.safety.grade, result.opportunity_score, result.hype_level, result.confidence,
                    price_usd, pillars.hype if pillars else None, pillars.onchain_health if pillars else None,
                    pillars.momentum if pillars else None, mention_velocity_ratio, int(dex_boosted), kol_score,
                    deployer_address, json.dumps([f.model_dump() for f in result.risk_flags]), result.thesis,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    async def save_result(self, result: FinalMemeResult, price_usd: Optional[float] = None, mention_velocity_ratio: Optional[float] = None, dex_boosted: bool = False, kol_score: Optional[float] = None, deployer_address: Optional[str] = None):
        await asyncio.to_thread(self._save_result_sync, result, price_usd, mention_velocity_ratio, dex_boosted, kol_score, deployer_address)

    def save_result_sync(self, result: FinalMemeResult, price_usd: Optional[float] = None, mention_velocity_ratio: Optional[float] = None, dex_boosted: bool = False, kol_score: Optional[float] = None, deployer_address: Optional[str] = None):
        self._save_result_sync(result, price_usd, mention_velocity_ratio, dex_boosted, kol_score, deployer_address)

    def _get_previous_scan_sync(self, token_address: str) -> Optional[dict]:
        """The most-recent PRIOR scan for this token — used for hype-delta
        comparison right before a new scan gets saved."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM meme_scan_results WHERE token_address = ? ORDER BY scanned_at DESC LIMIT 1",
                (token_address,),
            ).fetchall()
            return dict(rows[0]) if rows else None
        finally:
            conn.close()

    def get_previous_scan_sync(self, token_address: str) -> Optional[dict]:
        return self._get_previous_scan_sync(token_address)

    async def get_previous_scan(self, token_address: str) -> Optional[dict]:
        return await asyncio.to_thread(self._get_previous_scan_sync, token_address)

    def _get_history_sync(self, token_address: str, limit: int = 50) -> List[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM meme_scan_results WHERE token_address = ? ORDER BY scanned_at DESC LIMIT ?",
                (token_address, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_history_sync(self, token_address: str, limit: int = 50) -> List[dict]:
        return self._get_history_sync(token_address, limit)

    def _get_latest_scan_per_token_sync(self) -> List[dict]:
        """One row per distinct token — its most recent scan, regardless
        of when that was. Same pattern as the main scanner's
        get_latest_scan_per_symbol, used for dashboard hydration on startup
        so the table isn't empty just because the process restarted."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT r.* FROM meme_scan_results r
                INNER JOIN (
                    SELECT token_address, MAX(scanned_at) AS max_scanned_at
                    FROM meme_scan_results
                    GROUP BY token_address
                ) latest ON r.token_address = latest.token_address AND r.scanned_at = latest.max_scanned_at
                ORDER BY r.opportunity_score DESC
                """
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_latest_scan_per_token_sync(self) -> List[dict]:
        return self._get_latest_scan_per_token_sync()

    # ---------------------------------------------------------------- deployer blacklist

    def _is_deployer_blacklisted_sync(self, deployer_address: str) -> Optional[dict]:
        if not deployer_address:
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM deployer_blacklist WHERE deployer_address = ?", (deployer_address,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def is_deployer_blacklisted_sync(self, deployer_address: str) -> Optional[dict]:
        return self._is_deployer_blacklisted_sync(deployer_address)

    async def is_deployer_blacklisted(self, deployer_address: str) -> Optional[dict]:
        return await asyncio.to_thread(self._is_deployer_blacklisted_sync, deployer_address)

    def _add_to_deployer_blacklist_sync(self, deployer_address: str, reason: str, auto_detected: bool = False, source_token_address: Optional[str] = None):
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO deployer_blacklist (deployer_address, reason, added_at, auto_detected, source_token_address)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(deployer_address) DO UPDATE SET reason=excluded.reason
                """,
                (deployer_address, reason, datetime.now(timezone.utc).isoformat(), int(auto_detected), source_token_address),
            )
            conn.commit()
        finally:
            conn.close()

    def add_to_deployer_blacklist_sync(self, deployer_address: str, reason: str, auto_detected: bool = False, source_token_address: Optional[str] = None):
        self._add_to_deployer_blacklist_sync(deployer_address, reason, auto_detected, source_token_address)

    async def add_to_deployer_blacklist(self, deployer_address: str, reason: str, auto_detected: bool = False, source_token_address: Optional[str] = None):
        await asyncio.to_thread(self._add_to_deployer_blacklist_sync, deployer_address, reason, auto_detected, source_token_address)

    def _get_all_blacklisted_deployers_sync(self) -> List[str]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT deployer_address FROM deployer_blacklist").fetchall()
            return [r["deployer_address"] for r in rows]
        finally:
            conn.close()

    def get_all_blacklisted_deployers_sync(self) -> List[str]:
        return self._get_all_blacklisted_deployers_sync()

    # ---------------------------------------------------------------- performance tracking

    def _backtest_high_conviction_sync(self, min_score: float, lookback_days: int) -> dict:
        """
        Rough sanity check, same honest limitations as the main scanner's
        backtest_signal: no fees/slippage/position sizing modeled. For
        every FIRST time a token crossed the min_score threshold with
        Safety != Fail within the lookback window, compares its price
        then vs. its most recently recorded price.
        """
        conn = self._connect()
        try:
            since = (datetime.now(timezone.utc).timestamp() - lookback_days * 86400)
            since_iso = datetime.fromtimestamp(since, tz=timezone.utc).isoformat()
            rows = conn.execute(
                """
                SELECT token_address, symbol, opportunity_score, safety_grade, price_usd, scanned_at,
                       LAG(opportunity_score) OVER (PARTITION BY token_address ORDER BY scanned_at) AS prev_score
                FROM meme_scan_results
                WHERE scanned_at >= ?
                ORDER BY token_address, scanned_at
                """,
                (since_iso,),
            ).fetchall()

            entries = [
                dict(r) for r in rows
                if r["safety_grade"] != "Fail" and r["opportunity_score"] is not None and r["opportunity_score"] >= min_score
                and (r["prev_score"] is None or r["prev_score"] < min_score)
            ]

            results = []
            for entry in entries:
                if not entry["price_usd"]:
                    continue
                latest = conn.execute(
                    "SELECT price_usd FROM meme_scan_results WHERE token_address = ? ORDER BY scanned_at DESC LIMIT 1",
                    (entry["token_address"],),
                ).fetchone()
                if latest is None or not latest["price_usd"]:
                    continue
                forward_return_pct = (latest["price_usd"] / entry["price_usd"] - 1) * 100
                results.append({
                    "symbol": entry["symbol"], "entered_at": entry["scanned_at"],
                    "entry_price": entry["price_usd"], "latest_price": latest["price_usd"],
                    "forward_return_pct": forward_return_pct,
                })

            if not results:
                return {"min_score": min_score, "sample_size": 0, "avg_forward_return_pct": None, "win_rate_pct": None}

            avg_return = sum(r["forward_return_pct"] for r in results) / len(results)
            win_rate = sum(1 for r in results if r["forward_return_pct"] > 0) / len(results) * 100
            return {
                "min_score": min_score, "sample_size": len(results),
                "avg_forward_return_pct": round(avg_return, 2), "win_rate_pct": round(win_rate, 1),
                "entries": results,
            }
        finally:
            conn.close()

    def backtest_high_conviction_sync(self, min_score: float = 70.0, lookback_days: int = 30) -> dict:
        return self._backtest_high_conviction_sync(min_score, lookback_days)

    async def backtest_high_conviction(self, min_score: float = 70.0, lookback_days: int = 30) -> dict:
        return await asyncio.to_thread(self._backtest_high_conviction_sync, min_score, lookback_days)

"""
Our own open interest series.

The OI pillar answers "is real money backing this move" by comparing OI
now against OI earlier. Exchanges that expose a history (Bybit) are
geo-blocked from our host, and the ones that answer (Hyperliquid,
CoinGecko) return a single current point — so confirms_direction was
permanently None and nothing could ever reach Ready.

So we keep the series ourselves: every scan writes one row per coin, and
the pillar reads back a real 24h delta. Costs nothing, depends on no one,
and gets more useful the longer it runs.

Nothing here can break a scan — every call is wrapped. No history simply
means the pillar behaves exactly as it does today.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)


def _db() -> str:
    return os.getenv("SCANNER_DB_PATH", "opportunity_scanner.db")


def _connect():
    conn = sqlite3.connect(_db(), timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS oi_history (
            base   TEXT NOT NULL,
            ts     TEXT NOT NULL,
            oi_usd REAL NOT NULL,
            PRIMARY KEY (base, ts)
        )
    """)
    return conn


def record(base: str, oi_usd: Optional[float]) -> None:
    """One row per coin per scan."""
    if not base or oi_usd is None or oi_usd <= 0:
        return
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO oi_history (base, ts, oi_usd) VALUES (?, ?, ?)",
                (base.upper(), datetime.now(timezone.utc).isoformat(), float(oi_usd)),
            )
    except Exception as exc:
        log.debug("oi_history record failed for %s: %s", base, exc)


def frame(base: str, hours: int = 30):
    """
    The series in the shape the pillar expects (columns: ts, oi_usd), or
    None when there genuinely isn't enough history yet — which is the
    honest answer for the first day of running.
    """
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with _connect() as conn:
            rows = conn.execute(
                "SELECT ts, oi_usd FROM oi_history WHERE base = ? AND ts >= ? ORDER BY ts",
                (base.upper(), since),
            ).fetchall()
        if len(rows) < 2:
            return None
        return pd.DataFrame(rows, columns=["ts", "oi_usd"])
    except Exception as exc:
        log.debug("oi_history read failed for %s: %s", base, exc)
        return None


def prune(days: int = 14) -> None:
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with _connect() as conn:
            conn.execute("DELETE FROM oi_history WHERE ts < ?", (cutoff,))
    except Exception as exc:
        log.debug("oi_history prune failed: %s", exc)

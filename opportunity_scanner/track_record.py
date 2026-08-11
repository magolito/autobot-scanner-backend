"""
Track record — the actual "does this signal mean anything" answer.

Every scan since the readiness migration persists its classification
(Ready/Caution/Building + direction) alongside price. This computes
real, honest stats from that accumulated history: of the times "Ready"
fired, how often did price actually move the called direction, and by
how much.

Deliberately NOT a backtest against pre-existing history — readiness
classification didn't exist before this feature shipped, and
retroactively assigning it to old scan rows using rules that weren't in
effect when that data was captured would be fabricated evidence, not a
real track record. This tracks forward, honestly, starting from empty.
No real trader should deploy capital against an unproven signal — this
is the tool that lets "Ready" earn trust over time instead of asking
for it up front.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional
import sqlite3


@dataclass
class ReadinessOutcome:
    base: str
    signaled_at: str
    direction: str  # "bullish" | "bearish"
    price_at_signal: float
    price_now: Optional[float]
    return_pct: Optional[float]
    correct: Optional[bool]  # None if not yet resolved (no later price available)


@dataclass
class TrackRecordSummary:
    total_signals: int
    resolved_signals: int
    unresolved_signals: int
    win_rate_pct: Optional[float]
    avg_return_pct: Optional[float]
    outcomes: List[ReadinessOutcome] = field(default_factory=list)


def compute_track_record(
    db_path: str,
    lookback_days: int = 90,
    min_hours_since_signal: float = 24.0,
) -> TrackRecordSummary:
    """
    For every historical "Ready" signal within the lookback window,
    finds the LATEST known price for that symbol from a later scan and
    computes whether the direction call was right, and by how much.

    Only counts signals at least min_hours_since_signal old — a signal
    from 10 minutes ago hasn't had any real time to play out, and
    including it would just add noise to the stats, not information.
    A signal with no later scan yet is correctly reported as
    unresolved, not silently dropped or counted as a loss.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        age_cutoff = (datetime.now(timezone.utc) - timedelta(hours=min_hours_since_signal)).isoformat()

        signals = conn.execute(
            "SELECT * FROM scan_results WHERE readiness_label = 'Ready' AND scanned_at >= ? AND scanned_at <= ? "
            "ORDER BY scanned_at ASC",
            (cutoff, age_cutoff),
        ).fetchall()

        outcomes: List[ReadinessOutcome] = []
        for sig in signals:
            later = conn.execute(
                "SELECT price, scanned_at FROM scan_results WHERE base = ? AND scanned_at > ? "
                "ORDER BY scanned_at DESC LIMIT 1",
                (sig["base"], sig["scanned_at"]),
            ).fetchone()

            price_at_signal = sig["price"]
            direction = sig["readiness_direction"] or "bullish"

            if later is None:
                outcomes.append(ReadinessOutcome(
                    base=sig["base"], signaled_at=sig["scanned_at"], direction=direction,
                    price_at_signal=price_at_signal, price_now=None, return_pct=None, correct=None,
                ))
                continue

            price_now = later["price"]
            return_pct = ((price_now - price_at_signal) / price_at_signal) * 100.0 if price_at_signal else None
            correct = None
            if return_pct is not None:
                correct = (return_pct > 0) if direction == "bullish" else (return_pct < 0)

            outcomes.append(ReadinessOutcome(
                base=sig["base"], signaled_at=sig["scanned_at"], direction=direction,
                price_at_signal=price_at_signal, price_now=price_now, return_pct=return_pct, correct=correct,
            ))

        resolved = [o for o in outcomes if o.correct is not None]
        win_rate = (sum(1 for o in resolved if o.correct) / len(resolved) * 100.0) if resolved else None
        avg_return = (sum(o.return_pct for o in resolved) / len(resolved)) if resolved else None

        return TrackRecordSummary(
            total_signals=len(outcomes), resolved_signals=len(resolved),
            unresolved_signals=len(outcomes) - len(resolved),
            win_rate_pct=round(win_rate, 1) if win_rate is not None else None,
            avg_return_pct=round(avg_return, 2) if avg_return is not None else None,
            outcomes=outcomes,
        )
    finally:
        conn.close()

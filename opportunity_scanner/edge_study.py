"""
Does the score actually predict anything?

Every scan already writes price + composite + pillars + readiness into
scan_results. So the answer is already sitting in the database: take any
scan row, find that coin's price N hours later, measure the return. Do
it across thousands of rows and the question stops being a matter of
opinion.

Read-only. Changes no scoring, no signals, no behaviour — it reports.
"""

from __future__ import annotations

import os
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

DEFAULT_DB = os.getenv("SCANNER_DB_PATH", "opportunity_scanner.db")
HORIZONS = (4, 24, 72)          # hours forward
TOLERANCE_H = 1.5               # how near a later scan must be to count


def _rows(db_path: str):
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT scanned_at, base, price, composite_score, confidence, "
            "confidence_label, strength_score, oi_dynamics_score, momentum_score, "
            "social_score, readiness_label, readiness_direction "
            "FROM scan_results ORDER BY base, scanned_at"
        )]
    finally:
        conn.close()


def _ts(s: str) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def forward_returns(db_path: str = DEFAULT_DB):
    """Attach the realised return at each horizon to every scan row."""
    rows = _rows(db_path)
    by_base: dict = {}
    for r in rows:
        r["_t"] = _ts(r.get("scanned_at"))
        if r["_t"] and r.get("price"):
            by_base.setdefault(r["base"], []).append(r)

    out = []
    for _base, series in by_base.items():
        series.sort(key=lambda r: r["_t"])
        for i, r in enumerate(series):
            rec = dict(r)
            for h in HORIZONS:
                target = r["_t"] + timedelta(hours=h)
                best, best_gap = None, None
                for later in series[i + 1:]:
                    gap = abs((later["_t"] - target).total_seconds()) / 3600
                    if best_gap is None or gap < best_gap:
                        best, best_gap = later, gap
                    if later["_t"] > target + timedelta(hours=TOLERANCE_H):
                        break
                if best and best_gap is not None and best_gap <= TOLERANCE_H:
                    rec["ret_%dh" % h] = (best["price"] - r["price"]) / r["price"] * 100
            rec.pop("_t", None)
            out.append(rec)
    return out


def add_excess_returns(rows):
    """
    Raw return measures the market, not the signal. In a rising market
    every bucket looks predictive. So for each scan we also record the
    return RELATIVE to what every other coin did over the same window —
    that isolates whether the score picked better than average, which is
    the only thing worth knowing.
    """
    for h in HORIZONS:
        key, xkey = "ret_%dh" % h, "excess_%dh" % h
        by_time: dict = {}
        for r in rows:
            if key in r:
                by_time.setdefault(r["scanned_at"][:13], []).append(r[key])   # hour bucket
        means = {k: statistics.mean(v) for k, v in by_time.items() if len(v) >= 3}
        for r in rows:
            if key in r:
                m = means.get(r["scanned_at"][:13])
                if m is not None:
                    r[xkey] = r[key] - m
    return rows


def _summarise(bucket_rows, horizon):
    key, xkey = "ret_%dh" % horizon, "excess_%dh" % horizon
    vals = [r[key] for r in bucket_rows if key in r]
    if not vals:
        return None
    xs = [r[xkey] for r in bucket_rows if xkey in r]
    wins = [v for v in vals if v > 0]
    out = {
        "n": len(vals),
        "mean_return_pct": round(statistics.mean(vals), 3),
        "median_return_pct": round(statistics.median(vals), 3),
        "win_rate_pct": round(len(wins) / len(vals) * 100, 1),
    }
    if xs:
        beat = [x for x in xs if x > 0]
        out["mean_excess_pct"] = round(statistics.mean(xs), 3)
        out["beat_market_pct"] = round(len(beat) / len(xs) * 100, 1)
    return out


def report(db_path: str = DEFAULT_DB) -> dict:
    """
    The whole question in one payload: do higher scores, and the Ready
    label, precede better returns than lower ones?
    """
    try:
        rows = forward_returns(db_path)
    except sqlite3.OperationalError as exc:
        return {"ok": False, "error": "Cannot read scan history: %s" % exc}

    if not rows:
        return {"ok": False, "error": "No scan history yet."}

    rows = add_excess_returns(rows)

    scored = [r for r in rows if r.get("composite_score") is not None]
    buckets = {
        "80-100":   [r for r in scored if r["composite_score"] >= 80],
        "70-79":    [r for r in scored if 70 <= r["composite_score"] < 80],
        "60-69":    [r for r in scored if 60 <= r["composite_score"] < 70],
        "50-59":    [r for r in scored if 50 <= r["composite_score"] < 60],
        "below-50": [r for r in scored if r["composite_score"] < 50],
    }

    def group(field, labels):
        out = {}
        for label in labels:
            sel = [r for r in rows if (r.get(field) or "") == label]
            if sel:
                out[label] = {str(h): _summarise(sel, h) for h in HORIZONS}
        return out

    span = [r["scanned_at"] for r in rows]
    measured = sum(1 for r in rows if "ret_24h" in r)

    return {
        "ok": True,
        "rows": len(rows),
        "rows_with_24h_outcome": measured,
        "coins": len({r["base"] for r in rows}),
        "from": min(span),
        "to": max(span),
        "by_score_bucket": {
            k: {str(h): _summarise(v, h) for h in HORIZONS}
            for k, v in buckets.items() if v
        },
        "by_readiness": group("readiness_label", ("Ready", "Caution", "Building")),
        "by_confidence": group("confidence_label", ("High", "Medium", "Low")),
        "how_to_read": (
            "Read mean_excess_pct, not mean_return_pct. Raw return mostly "
            "measures the market; excess is the return relative to every "
            "other coin scanned at the same moment, which is what isolates "
            "the signal. If 80-100 has no higher excess than below-50, the "
            "composite score is not predictive and the weights need "
            "rethinking rather than tuning. Same test for Ready vs Building. "
            "Treat any bucket with n under ~100 as provisional."
        ),
    }

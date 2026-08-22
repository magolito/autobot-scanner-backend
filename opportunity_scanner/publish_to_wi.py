"""
Publish scan results to the Wealth Innovators Academy app.

Reads real ScanResult objects — no dict conversion needed. Call it after
a scan and members see the ranked list inside the Academy, in Wealth
Innovators' own design.

Railway environment variables:
    WI_PUBLISH_URL = https://wealthinnovators.club/api/scanner
    WI_ADMIN_TOKEN = (same token as in Netlify)

Never raises. If publishing fails, it logs and the scan carries on.
"""

from __future__ import annotations

import os
import logging
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

WI_PUBLISH_URL = os.getenv("WI_PUBLISH_URL", "https://wealthinnovators.club/api/scanner")
WI_ADMIN_TOKEN = os.getenv("WI_ADMIN_TOKEN", "")

# ScanResult.factors keys → what members see on the card
PILLAR_MAP = {
    "strength": "strength",
    "oi_dynamics": "oi",
    "momentum": "momentum",
    "social": "social",
}


def _pillar_scores(result) -> dict:
    """Pull the four pillar scores off ScanResult.factors."""
    out = {"strength": None, "oi": None, "momentum": None, "social": None}
    factors = getattr(result, "factors", None) or {}
    for key, label in PILLAR_MAP.items():
        f = factors.get(key)
        if f is None:
            continue
        score = getattr(f, "score", None)
        available = getattr(f, "available", True)
        if score is not None and available:
            try:
                out[label] = round(float(score), 1)
            except (TypeError, ValueError):
                pass
    return out


def _readiness(result) -> tuple[str, str | None, str]:
    """
    Use the scanner's own classifier so the app and the scanner never
    disagree about what's Ready.
    Returns (label, direction, explanation).
    """
    try:
        from opportunity_scanner.readiness import classify_readiness
        info = classify_readiness(result) or {}
        label = str(info.get("label", "Building")).lower()
        raw_dir = str(info.get("direction", "")).lower()
        direction = "long" if raw_dir == "bullish" else "short" if raw_dir == "bearish" else None
        return label, direction, str(info.get("explanation", ""))
    except Exception as exc:
        log.debug("readiness classify failed: %s", exc)
        return "building", None, ""


def build_payload(results, regime=None, universe=None, limit=12, only_passed=True):
    """Shape ScanResult objects into what the Academy expects."""
    rows = []

    ranked = [r for r in results if not only_passed or getattr(r, "passed_filters", True)]
    ranked = sorted(ranked, key=lambda r: getattr(r, "composite_score", 0) or 0, reverse=True)

    for r in ranked[:limit]:
        label, direction, explanation = _readiness(r)

        # prefer the readiness explanation; fall back to the reasons trail
        note = explanation
        if not note:
            reasons = getattr(r, "reasons_summary", None) or []
            note = reasons[0] if reasons else ""

        rows.append({
            "symbol": str(getattr(r, "base", "") or getattr(r, "symbol", "")).upper(),
            "name": "",
            "score": round(float(getattr(r, "composite_score", 0) or 0), 1),
            "readiness": label,
            "direction": direction,
            "price": getattr(r, "price", None),
            "change24h": getattr(r, "price_change_24h_pct", None),
            "pillars": _pillar_scores(r),
            "note": note[:160],
            "confidence": getattr(r, "confidence_label", None),
        })

    # regime comes off the results themselves if not passed in
    if regime is None and ranked:
        regime = getattr(ranked[0], "regime_label", None)
        if regime == "Unknown":
            regime = None

    return {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "regime": regime,
        "universe": universe if universe is not None else len(results),
        "results": [r for r in rows if r["symbol"]],
    }


def publish_to_wi(results, regime=None, universe=None, limit=12, timeout=10):
    """Push the latest scan to the Academy app. Never raises."""
    if not WI_ADMIN_TOKEN:
        log.warning("WI_ADMIN_TOKEN not set — skipping publish to Wealth Innovators")
        return False
    if not results:
        log.info("No results to publish")
        return False
    try:
        payload = build_payload(results, regime=regime, universe=universe, limit=limit)
        if not payload["results"]:
            log.warning("Nothing to publish — no rows survived filtering")
            return False
        res = requests.post(
            WI_PUBLISH_URL,
            json=payload,
            headers={"x-wi-token": WI_ADMIN_TOKEN, "content-type": "application/json"},
            timeout=timeout,
        )
        if res.ok:
            log.info("Published %d rows to Wealth Innovators", len(payload["results"]))
            return True
        log.warning("Publish failed: %s %s", res.status_code, res.text[:200])
    except Exception as exc:                      # never break a scan over this
        log.warning("Publish to Wealth Innovators failed: %s", exc)
    return False

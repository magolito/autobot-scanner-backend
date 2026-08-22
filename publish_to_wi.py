"""
Publish scan results to the Wealth Innovators Academy app.

Drop this file into the scanner project and call publish_to_wi(results)
at the end of a scan. Members then see the ranked list inside the app,
in Wealth Innovators' own design — no iframe, no Streamlit chrome.

Railway environment variables to add:
    WI_PUBLISH_URL = https://wealthinnovators.club/api/scanner
    WI_ADMIN_TOKEN = (the same token you set in Netlify)

Nothing here blocks or breaks a scan: if the publish fails, it logs and
moves on.
"""

import os
import logging
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

WI_PUBLISH_URL = os.getenv("WI_PUBLISH_URL", "https://wealthinnovators.club/api/scanner")
WI_ADMIN_TOKEN = os.getenv("WI_ADMIN_TOKEN", "")


def _pillar(row, *names):
    """Pull a pillar score from whichever key the scanner used."""
    for n in names:
        v = row.get(n)
        if v is None and isinstance(row.get("pillars"), dict):
            v = row["pillars"].get(n)
        if v is not None:
            try:
                v = float(v)
                # normalise 0–1 scores to 0–100
                return round(v * 100 if v <= 1 else v, 1)
            except (TypeError, ValueError):
                continue
    return None


def build_payload(results, regime=None, universe=None, limit=12):
    """Shape scanner rows into what the app expects."""
    rows = []
    for r in results[:limit]:
        rows.append({
            "symbol": str(r.get("symbol") or r.get("coin") or "").upper().replace("/USDT", "").replace("USDT", ""),
            "name": r.get("name") or "",
            "score": _pillar(r, "score", "total_score", "composite"),
            "readiness": str(r.get("readiness") or "building").lower(),
            "direction": (str(r.get("direction")).lower() if r.get("direction") else None),
            "price": r.get("price") or r.get("last_price"),
            "change24h": r.get("change24h") or r.get("change_24h") or r.get("pct_change_24h"),
            "pillars": {
                "trend":    _pillar(r, "trend", "trend_score"),
                "momentum": _pillar(r, "momentum", "momentum_score"),
                "strength": _pillar(r, "strength", "rs_score", "relative_strength"),
                "social":   _pillar(r, "social", "social_score", "galaxy_score"),
            },
            "note": (r.get("note") or r.get("summary") or "")[:160],
        })

    return {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "regime": regime,
        "universe": universe,
        "results": [r for r in rows if r["symbol"]],
    }


def publish_to_wi(results, regime=None, universe=None, limit=12, timeout=10):
    """Push the latest scan to the Academy app. Never raises."""
    if not WI_ADMIN_TOKEN:
        log.warning("WI_ADMIN_TOKEN not set — skipping publish to Wealth Innovators")
        return False
    try:
        payload = build_payload(results, regime=regime, universe=universe, limit=limit)
        if not payload["results"]:
            log.warning("Nothing to publish — no valid rows")
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


# ── how to use it ────────────────────────────────────────────────────
# At the end of your scan, once you have the ranked rows:
#
#     from publish_to_wi import publish_to_wi
#     publish_to_wi(ranked_results, regime=regime_label, universe=len(universe))
#
# In Streamlit you can also give yourself a manual button:
#
#     if st.button("Publish to Academy"):
#         ok = publish_to_wi(ranked_results, regime=regime_label)
#         st.success("Published") if ok else st.error("Publish failed — check logs")

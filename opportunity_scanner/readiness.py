"""
Trade readiness — the actual "is this worth watching or worth acting
on" answer, spelled out with the specific criteria that separate them,
not left implied by a bucket label. Built directly from a conversation
about what an "elite" scanner should communicate: not just a score, but
a clear reason, and a clear line between "still forming" and "an active
setup right now."

Two independent confirmations, both required for "Ready":
  - genuine multi-timeframe alignment (momentum agreeing with ITSELF
    across timeframes — see factors/momentum.py's _compute_alignment,
    not just one timeframe's noise)
  - OI confirming the SAME direction (real leveraged money actually
    backing the move — see factors/oi_dynamics.py's confirms_direction,
    not just price drifting on thin positioning)

Either alone is a real, partial signal — that's exactly "Building."
Alignment WITHOUT OI confirmation, or worse, with OI actively
disagreeing, is flagged as its own distinct "Caution" case rather than
folded silently into "Building," since a structure that looks ready but
has no real money behind it (or money actively leaving) is a
meaningfully different, more specific situation than one that simply
hasn't formed yet.
"""

from __future__ import annotations
from typing import Optional

from .models import ScanResult

# Matches Smart View's Super Strong min_alignment_score threshold
# deliberately — this is the same "is this genuine conviction" bar,
# not a second, independently-tuned number that could drift out of
# sync with what Super Strong actually requires.
READINESS_ALIGNMENT_THRESHOLD = 60.0


def _momentum_info(result: ScanResult) -> dict:
    momentum = result.factors.get("momentum")
    if not momentum or not momentum.available:
        return {"alignment_score": 0.0, "dominant_direction": "mixed", "aligned_timeframes": []}
    raw = momentum.raw or {}
    return {
        "alignment_score": raw.get("alignment_score", 0.0) or 0.0,
        "dominant_direction": raw.get("dominant_direction", "mixed"),
        "aligned_timeframes": raw.get("aligned_timeframes", []),
    }


def _oi_confirmation(result: ScanResult) -> Optional[bool]:
    """True = OI confirms the direction, False = OI contradicts it,
    None = no OI divergence data available to judge either way (a
    genuinely different case from "contradicts" — see factors/
    oi_dynamics.py's resilience design, which distinguishes 'no data'
    from a real negative signal rather than collapsing them together)."""
    oi = result.factors.get("oi_dynamics")
    if not oi or not oi.available:
        return None
    return (oi.raw or {}).get("confirms_direction")


def classify_readiness(result: ScanResult) -> dict:
    """
    Returns {"label", "direction", "explanation", "alignment_score",
    "oi_confirms"}. label is one of "Ready", "Caution", "Building".
    direction is "bullish", "bearish", or "mixed".
    """
    info = _momentum_info(result)
    alignment_score = info["alignment_score"]
    direction = info["dominant_direction"]
    aligned_tfs = info["aligned_timeframes"]
    oi_confirms = _oi_confirmation(result)

    is_aligned = alignment_score >= READINESS_ALIGNMENT_THRESHOLD and direction in ("bullish", "bearish")
    tf_list = ", ".join(aligned_tfs) if aligned_tfs else "multiple timeframes"

    if is_aligned and oi_confirms is True:
        side = "long" if direction == "bullish" else "short"
        label = "Ready"
        explanation = (
            f"Ready — full timeframe alignment ({alignment_score:.0f}% across {tf_list}), "
            f"OI confirming with real positioning behind the move. This is an active {side} setup."
        )
    elif is_aligned and oi_confirms is False:
        label = "Caution"
        explanation = (
            f"Caution — timeframes agree ({alignment_score:.0f}% aligned), but OI is moving AGAINST "
            f"the move (covering/liquidation, not fresh conviction) — structure without real backing yet."
        )
    elif is_aligned:
        label = "Building"
        explanation = (
            f"Building — timeframe alignment forming ({alignment_score:.0f}% across {tf_list}), "
            f"but no OI data available yet to confirm real positioning behind it."
        )
    else:
        label = "Building"
        explanation = f"Building — structure still forming, alignment partial ({alignment_score:.0f}%), not confirmed yet."

    return {
        "label": label, "direction": direction, "explanation": explanation,
        "alignment_score": alignment_score, "oi_confirms": oi_confirms,
    }

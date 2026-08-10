"""
Composite scoring — combines the four pillar FactorResults into one
Opportunity Score (0-100), handling missing pillars by redistributing
their weight rather than silently treating a missing pillar as zero
(which would unfairly tank coins without derivatives listings or social
coverage) or as neutral-50 in the final blend (which would understate
conviction from the pillars that ARE available).
"""

from __future__ import annotations
import statistics
from typing import Dict, List
from .config import Weights, SignalBands, ConfidenceBands
from .models import FactorResult


def _normalize(v: float, lo: float, hi: float) -> float:
    if hi == lo:
        return 50.0
    return max(0.0, min(100.0, (v - lo) / (hi - lo) * 100.0))


def compute_confidence(
    factors: Dict[str, FactorResult],
    weights_used: Dict[str, float],
    original_weights: Weights,
) -> float:
    """
    Confidence is reported ALONGSIDE the score, never folded into it —
    folding it in would conflate "this coin is weak" with "we don't have
    enough data to know." Two components, averaged:

      completeness: how much of the intended weight is backed by real
                    data vs redistributed away from a missing pillar
      agreement:    do the available pillars broadly agree with each
                    other? High variance across pillars (e.g. strength=85,
                    oi_dynamics=20) means a less trustworthy composite
                    even if each pillar individually has good data.
    """
    original = original_weights.as_dict()
    completeness = sum(original[k] for k, f in factors.items() if f.available)  # already 0-1

    available_scores = [f.score for f in factors.values() if f.available]
    if len(available_scores) >= 2:
        spread = statistics.stdev(available_scores)
        agreement = 1.0 - _normalize(spread, 0, 35) / 100.0
    else:
        agreement = 0.5  # only one pillar available — can't measure agreement, stay neutral

    confidence = (completeness * 0.5 + agreement * 0.5) * 100.0
    return round(max(0.0, min(100.0, confidence)), 1)


def combine_factors(
    factors: Dict[str, FactorResult],
    weights: Weights,
    signal_bands: SignalBands,
    confidence_bands: ConfidenceBands | None = None,
) -> tuple[float, float, str, str, Dict[str, float], List[str]]:
    missing = [name for name, f in factors.items() if not f.available]
    effective_weights = weights.redistribute_missing(missing) if missing else weights
    w = effective_weights.as_dict()

    composite = sum(factors[name].score * w[name] for name in factors)
    composite = round(max(0.0, min(100.0, composite)), 1)

    signal = signal_bands.grade(composite)

    confidence = compute_confidence(factors, w, weights)
    conf_bands = confidence_bands or ConfidenceBands()
    confidence_label = conf_bands.label(confidence)

    # Build a short, ranked explainability summary: pick the single most
    # informative reason from each pillar, ordered by that pillar's
    # contribution to the final score (weight * score).
    contributions = sorted(
        factors.items(),
        key=lambda kv: w[kv[0]] * kv[1].score,
        reverse=True,
    )
    reasons_summary = []
    for name, f in contributions:
        if f.reasons:
            reasons_summary.append(f"[{name}, weight {w[name]*100:.0f}%] {f.reasons[0]}")

    return composite, confidence, confidence_label, signal, w, reasons_summary[:5]

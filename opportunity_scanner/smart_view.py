"""
Smart View — buckets a flat list of ScanResults into four clear
priority groups instead of one undifferentiated table, so a person
looking at 20+ results sees "here's what actually matters" immediately
rather than having to scan every row themselves.

Deliberately a presentation-layer grouping on top of the existing
composite_score/confidence/risk_tier — not a second, competing scoring
system. Buckets are evaluated most-selective-first; a result lands in
the first bucket it qualifies for. Thresholds live in
SmartViewConfig (config.py), adjustable without touching this file.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List

from .config import SmartViewConfig, BucketThresholds
from .models import ScanResult


class Bucket(str, Enum):
    SUPER_STRONG = "super_strong"
    STRONG = "strong"
    BUILDING = "building"
    HIGH_RISK_LOW_CONVICTION = "high_risk_low_conviction"


BUCKET_LABELS = {
    Bucket.SUPER_STRONG: "🔥 Super Strong",
    Bucket.STRONG: "Strong",
    Bucket.BUILDING: "Building / Watchlist",
    Bucket.HIGH_RISK_LOW_CONVICTION: "High Risk / Low Conviction",
}


def data_completeness(result: ScanResult) -> float:
    """Fraction of pillars that actually had data available (FactorResult.
    available=True), independent of confidence — confidence blends
    completeness together with pillar agreement, so a result could have
    middling confidence for reasons OTHER than missing data (e.g.
    pillars disagreeing with each other). Checking completeness directly
    is a distinct, explicit signal, not a proxy for confidence."""
    if not result.factors:
        return 0.0
    available_count = sum(1 for f in result.factors.values() if f.available)
    return available_count / len(result.factors)


def _alignment_score(result: ScanResult) -> float:
    """Weighted multi-timeframe agreement from the momentum pillar (see
    factors/momentum.py's _compute_alignment) — 0.0 if momentum is
    unavailable or the field is missing (older cached results). Missing
    alignment data means "we can't claim genuine multi-timeframe
    conviction," so it correctly fails any positive min_alignment_score
    threshold rather than being treated as a free pass."""
    momentum = result.factors.get("momentum")
    if not momentum or not momentum.available:
        return 0.0
    return momentum.raw.get("alignment_score", 0.0) or 0.0


def _meets(result: ScanResult, thresholds: BucketThresholds, completeness: float) -> bool:
    return (
        result.composite_score >= thresholds.min_score
        and result.confidence >= thresholds.min_confidence
        and result.risk_tier in thresholds.allowed_risk_tiers
        and completeness >= thresholds.min_data_completeness
        and _alignment_score(result) >= thresholds.min_alignment_score
    )


def classify_bucket(result: ScanResult, config: SmartViewConfig) -> Bucket:
    """Most-selective-first evaluation — a result that would qualify for
    Super Strong AND Strong lands in Super Strong, not double-counted."""
    completeness = data_completeness(result)

    if _meets(result, config.super_strong, completeness):
        return Bucket.SUPER_STRONG
    if _meets(result, config.strong, completeness):
        return Bucket.STRONG
    if _meets(result, config.building, completeness):
        return Bucket.BUILDING
    return Bucket.HIGH_RISK_LOW_CONVICTION


def bucket_results(results: List[ScanResult], config: SmartViewConfig) -> Dict[Bucket, List[ScanResult]]:
    """Returns all four buckets, always, even if empty — callers can rely
    on every key existing rather than checking for KeyError, and each
    bucket's results stay sorted by score descending, matching the
    existing ranked-table convention."""
    buckets: Dict[Bucket, List[ScanResult]] = {b: [] for b in Bucket}
    for result in results:
        buckets[classify_bucket(result, config)].append(result)
    for bucket_results_list in buckets.values():
        bucket_results_list.sort(key=lambda r: r.composite_score, reverse=True)
    return buckets

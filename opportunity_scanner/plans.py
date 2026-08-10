"""
Plan tiers — the feature-gating scaffold Stage 4 (connect both scanners
with plan-based access) consumes. `PlanFeatures` was originally a single
global `max_scans_per_day`, which couldn't express "Pro gets unlimited
Opportunity Scanner but a capped Meme Scanner" — Meme Scanner queries
cost real money per call (RugCheck + GoPlus + DexScreener), so it
genuinely needs its own, stricter limit even on a plan where the main
scanner is unlimited. Redesigned to per-scanner limits for that reason,
not just for symmetry.

Concrete decisions made here (the "decide and document" ask), stated
plainly rather than left implicit:

  Free:  Opportunity — 5 scans/day, results capped to the top 5 by score
         (not the full ranked list — "limited" means less, not slower)
         Meme — blocked entirely (ScannerAccess.NONE)
  Pro:   Opportunity — unlimited scans, full results
         Meme — 10 scans/day, full results on the scans they do run
  Elite: Both — unlimited scans, full results

Prices are illustrative starting points, not locked — easy to adjust in
one place.
"""

from __future__ import annotations
from enum import Enum
from pydantic import BaseModel
from typing import Optional


class ScannerAccess(str, Enum):
    NONE = "none"
    LIMITED = "limited"
    FULL = "full"


class PlanTier(str, Enum):
    FREE = "free"
    PRO = "pro"
    ELITE = "elite"


class PlanFeatures(BaseModel):
    opportunity_scanner_access: ScannerAccess
    meme_scanner_access: ScannerAccess
    opportunity_max_scans_per_day: Optional[int] = None   # None = unlimited
    meme_max_scans_per_day: Optional[int] = None
    opportunity_max_results_shown: Optional[int] = None    # None = full list; caps the ranked table itself, not just what's visible
    priority_support: bool = False
    price_usd_per_month: float = 0.0


PLAN_CONFIG: dict[PlanTier, PlanFeatures] = {
    PlanTier.FREE: PlanFeatures(
        opportunity_scanner_access=ScannerAccess.LIMITED,
        meme_scanner_access=ScannerAccess.NONE,
        opportunity_max_scans_per_day=5,
        meme_max_scans_per_day=0,
        opportunity_max_results_shown=5,
        price_usd_per_month=0.0,
    ),
    PlanTier.PRO: PlanFeatures(
        opportunity_scanner_access=ScannerAccess.FULL,
        meme_scanner_access=ScannerAccess.LIMITED,
        opportunity_max_scans_per_day=None,
        meme_max_scans_per_day=10,
        opportunity_max_results_shown=None,
        price_usd_per_month=39.0,
    ),
    PlanTier.ELITE: PlanFeatures(
        opportunity_scanner_access=ScannerAccess.FULL,
        meme_scanner_access=ScannerAccess.FULL,
        opportunity_max_scans_per_day=None,
        meme_max_scans_per_day=None,
        opportunity_max_results_shown=None,
        priority_support=True,
        price_usd_per_month=89.0,
    ),
}


def get_plan_features(plan: PlanTier) -> PlanFeatures:
    return PLAN_CONFIG[plan]


def has_scanner_access(plan: PlanTier, scanner: str) -> ScannerAccess:
    """`scanner` is "opportunity" or "meme" — returns the access LEVEL
    (none/limited/full), not just a bool, since "limited" access is a
    real, distinct state the UI needs to handle differently from both
    "no access, show upsell" and "full access, no restrictions."""
    features = get_plan_features(plan)
    if scanner == "opportunity":
        return features.opportunity_scanner_access
    if scanner == "meme":
        return features.meme_scanner_access
    raise ValueError(f"Unknown scanner '{scanner}' — expected 'opportunity' or 'meme'")


def max_scans_per_day(plan: PlanTier, scanner: str) -> Optional[int]:
    features = get_plan_features(plan)
    if scanner == "opportunity":
        return features.opportunity_max_scans_per_day
    if scanner == "meme":
        return features.meme_max_scans_per_day
    raise ValueError(f"Unknown scanner '{scanner}' — expected 'opportunity' or 'meme'")

"""
Access control — the single place that decides whether a user can run a
scan right now, and why not if they can't. Both dashboards call this
same function rather than each reimplementing the plan/limit logic, so
there's one source of truth for "what does this plan actually allow"
matching plans.py's documented decisions.

This is the server-side enforcement point the requirement asks for: the
actual scan-triggering code in each dashboard calls check_scanner_access()
and refuses to proceed if `allowed` is False, rather than only hiding a
button in the UI. A hidden button doesn't stop a scan from running if
something else in the script path could still trigger it — checking at
the point of action, not just at the point of rendering, is what makes
this a real enforcement point rather than a UI suggestion.
"""

from __future__ import annotations
from typing import Optional
from pydantic import BaseModel

from .app_storage import AppStorage, User
from .plans import PlanTier, ScannerAccess, get_plan_features, has_scanner_access, max_scans_per_day

SCANNER_DISPLAY_NAMES = {"opportunity": "Opportunity Scanner", "meme": "Meme Coin Scanner"}


class AccessDecision(BaseModel):
    allowed: bool
    reason: Optional[str] = None          # human-readable, safe to show directly in the UI
    scans_used_today: int = 0
    scans_remaining_today: Optional[int] = None   # None = unlimited
    max_results_shown: Optional[int] = None         # None = full list; only meaningful for scanner="opportunity" today
    effective_plan: Optional[PlanTier] = None       # real fix for a confirmed bug: the dashboard's "Plan: X" label
                                                     # was reading user.plan directly, bypassing the admin_emails
                                                     # override entirely — this exposes what the UI should actually
                                                     # display, not just what functional limits apply


def get_effective_plan(user: User, admin_emails: Optional[list[str]] = None) -> PlanTier:
    """
    The single source of truth for the admin-override check — extracted
    directly after finding a THIRD place that needed this exact logic
    (billing_ui.py's "Account & Billing" section still showed "Free"
    after the dashboard's own top-level label was already fixed, since
    it read user.plan directly too). Three separate places needing the
    identical check is the actual signal this belongs in one function,
    not copy-pasted — the next place that needs it imports this instead
    of re-implementing the comparison a fourth time.
    """
    if admin_emails and user.email.lower() in {e.lower() for e in admin_emails}:
        return PlanTier.ELITE
    return user.plan


def check_scanner_access(user: User, scanner: str, storage: AppStorage, admin_emails: Optional[list[str]] = None) -> AccessDecision:
    """
    Call this BEFORE running a scan or showing results — not just before
    rendering the "Scan Now" button. Every branch that returns
    allowed=False also sets a plain-language `reason` ready to show
    directly, so the caller never has to construct its own error copy
    (and can't accidentally show a technical/internal message instead).

    admin_emails: an explicit, reversible testing/founder override —
    added directly from "how do I test the full version without going
    through billing." Listed emails get evaluated as PlanTier.ELITE for
    this decision only; the user's actual stored `plan` in the database
    is never touched, so nothing needs to be manually reset later, and
    removing the email from the env var immediately reverts to their
    real plan. Deliberately an env var (ADMIN_EMAILS), not a database
    flag, so it can't accidentally get included in a backup/restore or
    granted to the wrong account through a UI mistake.
    """
    effective_plan = get_effective_plan(user, admin_emails)
    display_name = SCANNER_DISPLAY_NAMES.get(scanner, scanner)
    access_level = has_scanner_access(effective_plan, scanner)
    limit = max_scans_per_day(effective_plan, scanner)
    used = storage.get_scan_count_today(user.id, scanner)
    remaining = None if limit is None else max(limit - used, 0)
    features = get_plan_features(effective_plan)
    max_results = features.opportunity_max_results_shown if scanner == "opportunity" else None

    if access_level == ScannerAccess.NONE:
        return AccessDecision(
            allowed=False,
            reason=f"The {display_name} isn't included in your {effective_plan.value.title()} plan. Upgrade to unlock it.",
            scans_used_today=used, scans_remaining_today=0, max_results_shown=max_results,
            effective_plan=effective_plan,
        )

    if limit is not None and used >= limit:
        return AccessDecision(
            allowed=False,
            reason=f"You've used all {limit} of today's {display_name} scans on the {effective_plan.value.title()} plan. Upgrade for more, or check back tomorrow.",
            scans_used_today=used, scans_remaining_today=0, max_results_shown=max_results,
            effective_plan=effective_plan,
        )

    return AccessDecision(
        allowed=True, scans_used_today=used, scans_remaining_today=remaining, max_results_shown=max_results,
        effective_plan=effective_plan,
    )


def record_scan(user: User, scanner: str, storage: AppStorage) -> int:
    """Call this AFTER a scan completes successfully — not on every
    attempt, so a network/API failure doesn't burn a Free user's limited
    daily allowance for a scan that never actually delivered results."""
    return storage.increment_scan_count(user.id, scanner)

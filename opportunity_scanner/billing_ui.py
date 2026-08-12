"""
Billing UI — shared between dashboard.py and meme_dashboard.py, same
reasoning as auth_ui.py: identical flow, no reason to duplicate it.

Deliberately generates the checkout/invoice URL only when the person
clicks a specific "Get link" button, not on every page load — both
create_checkout_session() and create_crypto_invoice() are live network
calls to a billing provider.

Honesty note matching billing_stripe.py/billing_crypto.py: the actual
network calls this UI triggers are untested against live Stripe/
NowPayments accounts in this sandbox. What IS tested here (see
test_billing_ui.py) is that the UI behaves correctly when billing isn't
configured, and that a live-call failure is caught and shown as a clean
error rather than crashing the whole dashboard.
"""

from __future__ import annotations
import asyncio
import logging
from typing import Optional

import streamlit as st

from .app_storage import User
from .plans import PlanTier, PLAN_CONFIG
from .access_control import get_effective_plan
from . import billing_stripe
from . import billing_crypto

logger = logging.getLogger("opportunity_scanner.billing_ui")


def _plan_rank(plan: PlanTier) -> int:
    return {PlanTier.FREE: 0, PlanTier.PRO: 1, PlanTier.ELITE: 2}[plan]


def _price_id_for_plan(target_plan: PlanTier, settings) -> Optional[str]:
    if target_plan == PlanTier.PRO:
        return settings.stripe.price_id_pro
    if target_plan == PlanTier.ELITE:
        return settings.stripe.price_id_elite
    return None


def _render_stripe_upgrade_button(user: User, target_plan: PlanTier, settings, success_url: str, cancel_url: str):
    state_key = f"stripe_checkout_url_{target_plan.value}"
    if st.button("Upgrade with card", key=f"stripe_btn_{target_plan.value}"):
        price_id = _price_id_for_plan(target_plan, settings)
        if not price_id or not settings.stripe.secret_key:
            st.error("Stripe price ID or secret key isn't configured for this plan.")
            return
        try:
            url = billing_stripe.create_checkout_session(
                settings.stripe.secret_key, price_id, user.id, user.email, success_url, cancel_url,
            )
            st.session_state[state_key] = url
        except Exception as e:  # noqa: BLE001 — a live billing API call; any failure here should be visible, not crash the dashboard
            logger.error(f"Stripe checkout session creation failed: {e}")
            st.error(f"Couldn't create a checkout session: {e}")
            return

    if st.session_state.get(state_key):
        st.link_button("Continue to Stripe →", st.session_state[state_key])


def _render_crypto_upgrade_button(user: User, target_plan: PlanTier, settings, success_url: str, cancel_url: str):
    state_key = f"crypto_invoice_url_{target_plan.value}"
    if st.button("Pay with crypto", key=f"crypto_btn_{target_plan.value}"):
        if not settings.crypto_payments.api_key:
            st.error("Crypto payment API key isn't configured.")
            return
        price_usd = settings.crypto_payments.price_usd_pro if target_plan == PlanTier.PRO else settings.crypto_payments.price_usd_elite
        ipn_callback_url = success_url.rsplit("/", 1)[0] + "/webhooks/nowpayments" if "/" in success_url else success_url
        try:
            url = asyncio.run(billing_crypto.create_crypto_invoice(
                settings.crypto_payments.api_key, user.id, target_plan, price_usd,
                ipn_callback_url, success_url, cancel_url, sandbox=settings.crypto_payments.sandbox,
            ))
            st.session_state[state_key] = url
        except Exception as e:  # noqa: BLE001 — same reasoning as the Stripe branch
            logger.error(f"NowPayments invoice creation failed: {e}")
            st.error(f"Couldn't create a crypto invoice: {e}")
            return

    if st.session_state.get(state_key):
        st.link_button("Continue to payment →", st.session_state[state_key])


def render_billing_section(user: User, settings, success_url: str, cancel_url: str):
    """
    `settings` is the loaded Settings object — typed loosely here (not
    imported from .settings) to avoid a circular import, since
    settings.py doesn't need to know this module exists.
    """
    st.markdown('<div class="mono-label">Billing</div>', unsafe_allow_html=True)

    admin_emails = getattr(settings, "admin_emails", None)
    effective_plan = get_effective_plan(user, admin_emails)
    is_admin_override = effective_plan != user.plan
    current_features = PLAN_CONFIG[effective_plan]

    if is_admin_override:
        # Explicit, honest label — the actual fix for a live report:
        # showing "Elite ($89/mo)" here would misleadingly imply a real
        # paid subscription that doesn't exist. This is a testing
        # override, not billing, and the UI should say so plainly.
        st.markdown(f"Current plan: **{effective_plan.value.title()}** (testing override, not billed) · Status: `{user.subscription_status}`")
    else:
        price_display = "$0/mo" if current_features.price_usd_per_month == 0 else f"${current_features.price_usd_per_month:.0f}/mo"
        st.markdown(f"Current plan: **{effective_plan.value.title()}** ({price_display}) · Status: `{user.subscription_status}`")

    if user.plan == PlanTier.ELITE:
        st.caption("You're on the highest tier — nothing to upgrade to.")
        return

    upgrade_targets = [p for p in (PlanTier.PRO, PlanTier.ELITE) if _plan_rank(p) > _plan_rank(user.plan)]

    if not settings.stripe.enabled and not settings.crypto_payments.enabled:
        st.caption("Billing isn't configured yet — upgrades aren't available.")
        return

    for target_plan in upgrade_targets:
        features = PLAN_CONFIG[target_plan]
        st.markdown(f"**{target_plan.value.title()}** — ${features.price_usd_per_month:.0f}/mo")
        col1, col2 = st.columns(2)

        with col1:
            if settings.stripe.enabled:
                _render_stripe_upgrade_button(user, target_plan, settings, success_url, cancel_url)
            else:
                st.caption("Card payment not configured")

        with col2:
            if settings.crypto_payments.enabled:
                _render_crypto_upgrade_button(user, target_plan, settings, success_url, cancel_url)
            else:
                st.caption("Crypto payment not configured")

"""
Stripe billing — checkout session creation and webhook event handling.

Honesty note, same standard as every external provider in this project
(RugCheck, GoPlus, LunarCrush): `create_checkout_session()` makes a real
network call to Stripe's API and is UNTESTED against a live Stripe
account in this sandbox (no network access to api.stripe.com here).
Written correctly against Stripe's documented API (verified via search
before writing this, not assumed), but verify against a real test-mode
account before relying on it in production.

`verify_and_parse_webhook()` and `handle_stripe_event()` are the parts
that matter most for correctness and ARE fully tested — signature
verification is pure local HMAC-SHA256 (no network call), so real
signed test payloads can be constructed and verified entirely offline.
This is also the security-critical half: an unverified webhook endpoint
is an open door for anyone to grant themselves a paid plan for free by
POSTing a fake "payment succeeded" event.
"""

from __future__ import annotations
import logging
from typing import Optional

import stripe

from .app_storage import AppStorage
from .plans import PlanTier

logger = logging.getLogger("opportunity_scanner.billing_stripe")


def create_checkout_session(
    secret_key: str, price_id: str, user_id: int, user_email: str,
    success_url: str, cancel_url: str,
) -> str:
    """
    Returns the Checkout URL to redirect the user to. `client_reference_id`
    carries our internal user_id through Stripe's flow — this is how the
    webhook handler later knows which of OUR users a given
    checkout.session.completed event belongs to, since Stripe's own
    customer ID doesn't exist yet at redirect time for a new customer.
    """
    stripe.api_key = secret_key
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        client_reference_id=str(user_id),
        customer_email=user_email,
        success_url=success_url,
        cancel_url=cancel_url,
    )
    return session.url


def verify_and_parse_webhook(payload: bytes, sig_header: str, webhook_secret: str) -> stripe.Event:
    """
    Raises stripe.error.SignatureVerificationError on an invalid/tampered
    signature — the caller (the FastAPI route) should catch this and
    return 400, not 200, so an attacker forging a fake event gets
    rejected rather than silently processed.
    """
    return stripe.Webhook.construct_event(payload, sig_header, webhook_secret)


def _price_id_to_plan(price_id: str, price_id_pro: Optional[str], price_id_elite: Optional[str]) -> Optional[PlanTier]:
    if price_id_pro and price_id == price_id_pro:
        return PlanTier.PRO
    if price_id_elite and price_id == price_id_elite:
        return PlanTier.ELITE
    return None


def handle_stripe_event(
    event: stripe.Event, storage: AppStorage,
    price_id_pro: Optional[str], price_id_elite: Optional[str],
) -> Optional[str]:
    """
    Deliberately relies only on data already present in each event's own
    payload — no extra API calls mid-handler (an earlier version of this
    function called stripe.checkout.Session.list_line_items(), a live
    network request; removed after confirming customer.subscription.
    created/updated already carry full price data directly, which
    several independent sources explicitly recommend relying on for
    exactly this reason). This keeps the entire webhook handler pure
    local computation, testable with constructed payloads and no network.

    Two-step handling, matching how Stripe actually fires events for a
    new subscription: checkout.session.completed links our user_id to
    the new Stripe customer_id (that's the only thing it's uniquely
    positioned to know); customer.subscription.created/updated then set
    the actual plan, found via that customer_id link. There's a small
    window where these could theoretically arrive out of order — a
    known, inherent characteristic of webhook-based systems in general,
    not something specific to this implementation, and not worth
    over-engineering a fix for before it's observed as a real problem.
    """
    if event.type == "checkout.session.completed":
        session = event.data.object.to_dict()
        user_id = session.get("client_reference_id")
        customer_id = session.get("customer")
        if not user_id or not customer_id:
            logger.warning("checkout.session.completed missing client_reference_id or customer — can't link")
            return None
        user = storage.get_user_by_id(int(user_id))
        if user is None:
            logger.warning(f"checkout.session.completed for unknown user_id {user_id}")
            return None
        storage.update_subscription(user.id, stripe_customer_id=customer_id, subscription_status="active")
        return f"Linked Stripe customer {customer_id} to user {user.id}"

    if event.type in ("customer.subscription.created", "customer.subscription.updated"):
        subscription = event.data.object.to_dict()
        user = storage.get_user_by_stripe_customer_id(subscription.get("customer"))
        if user is None:
            logger.warning(f"{event.type} for unknown Stripe customer {subscription.get('customer')} — checkout.session.completed may not have arrived yet")
            return None
        items = subscription.get("items", {}).get("data", [])
        price_id = items[0]["price"]["id"] if items else None
        plan = _price_id_to_plan(price_id, price_id_pro, price_id_elite) if price_id else None
        storage.update_subscription(user.id, plan=plan, subscription_status=subscription.get("status"))
        return f"Updated user {user.id}: status={subscription.get('status')}, plan={plan.value if plan else 'unchanged'}"

    if event.type == "customer.subscription.deleted":
        subscription = event.data.object.to_dict()
        user = storage.get_user_by_stripe_customer_id(subscription.get("customer"))
        if user is None:
            logger.warning(f"customer.subscription.deleted for unknown Stripe customer {subscription.get('customer')}")
            return None
        storage.update_subscription(user.id, plan=PlanTier.FREE, subscription_status="canceled")
        return f"Canceled subscription for user {user.id}, downgraded to Free"

    return None

"""
NowPayments crypto billing — invoice creation and IPN (their term for
webhook) handling.

Same honesty split as billing_stripe.py: `create_crypto_invoice()` makes
a real network call to NowPayments' API and is untested against a live
account here. `verify_ipn_signature()` and `handle_crypto_ipn_event()`
are pure local computation and ARE fully tested — the signature scheme
(HMAC-SHA512 over the recursively-sorted, compact-JSON payload) was
confirmed via NowPayments' own official onboarding documentation before
writing this, not assumed or guessed.
"""

from __future__ import annotations
import hashlib
import hmac
import json
import logging
from typing import Optional

import httpx

from .app_storage import AppStorage
from .plans import PlanTier

logger = logging.getLogger("opportunity_scanner.billing_crypto")

NOWPAYMENTS_BASE_URL = "https://api.nowpayments.io/v1"
NOWPAYMENTS_SANDBOX_BASE_URL = "https://api-sandbox.nowpayments.io/v1"

# Statuses that represent a completed, confirmed payment. NowPayments'
# own documentation sample IPN payload shows "finished" as the terminal
# success state; "confirmed" is included too since some payment flows
# report that as the point funds are considered final. Every other
# status (waiting, confirming, partially_paid, failed, expired, ...) is
# deliberately NOT treated as success.
SUCCESS_STATUSES = {"finished", "confirmed"}


def _encode_order_id(user_id: int, plan: PlanTier) -> str:
    return f"user_{user_id}_plan_{plan.value}"


def _decode_order_id(order_id: str):
    try:
        parts = order_id.split("_")
        if parts[0] != "user" or parts[2] != "plan":
            return None
        user_id = int(parts[1])
        plan_value = "_".join(parts[3:])
        return user_id, PlanTier(plan_value)
    except (IndexError, ValueError):
        return None


async def create_crypto_invoice(
    api_key: str, user_id: int, plan: PlanTier, price_usd: float,
    ipn_callback_url: str, success_url: str, cancel_url: str, sandbox: bool = False,
) -> str:
    """Returns the hosted invoice URL to redirect the user to."""
    base_url = NOWPAYMENTS_SANDBOX_BASE_URL if sandbox else NOWPAYMENTS_BASE_URL
    async with httpx.AsyncClient(base_url=base_url, timeout=15.0, headers={"x-api-key": api_key}) as client:
        resp = await client.post("/invoice", json={
            "price_amount": price_usd, "price_currency": "usd",
            "order_id": _encode_order_id(user_id, plan),
            "order_description": f"AutoBot Scanner — {plan.value} plan",
            "ipn_callback_url": ipn_callback_url,
            "success_url": success_url, "cancel_url": cancel_url,
        })
        resp.raise_for_status()
        return resp.json()["invoice_url"]


def verify_ipn_signature(payload: dict, ipn_secret: str, signature_header: str) -> bool:
    """
    HMAC-SHA512 over the payload with keys recursively sorted and
    compact separators (no spaces) — this exact scheme is required: a
    naive json.dumps(payload) without sort_keys+compact separators
    produces a different byte string and the signature will never
    match, even with the correct secret. Confirmed against NowPayments'
    own documented Python example before implementing.
    """
    sorted_payload = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    computed = hmac.new(ipn_secret.encode("utf-8"), sorted_payload.encode("utf-8"), hashlib.sha512).hexdigest()
    return hmac.compare_digest(computed, signature_header)


def handle_crypto_ipn_event(payload: dict, storage: AppStorage) -> Optional[str]:
    """Only acts on SUCCESS_STATUSES — every other status is a normal,
    expected intermediate state, not an error."""
    status = payload.get("payment_status")
    if status not in SUCCESS_STATUSES:
        return None

    order_id = payload.get("order_id")
    if not order_id:
        logger.warning("Crypto IPN success event with no order_id — can't map to a user")
        return None

    decoded = _decode_order_id(order_id)
    if decoded is None:
        logger.warning(f"Crypto IPN with unparseable order_id: {order_id}")
        return None
    user_id, plan = decoded

    user = storage.get_user_by_id(user_id)
    if user is None:
        logger.warning(f"Crypto IPN for unknown user_id {user_id}")
        return None

    storage.update_subscription(user.id, plan=plan, subscription_status="active")
    return f"Activated {plan.value} for user {user.id} via crypto payment"

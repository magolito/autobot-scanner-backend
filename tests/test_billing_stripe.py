"""
Stripe billing test — constructs REAL signed webhook payloads locally
using Stripe's documented signature scheme (t=<timestamp>,v1=HMAC-SHA256
over "{timestamp}.{payload}"), no network needed since signature
verification is pure local HMAC.

Checks:
  1. A validly signed webhook verifies successfully
  2. A tampered payload is rejected
  3. checkout.session.completed links the Stripe customer to our user
     (does NOT set a plan yet — see billing_stripe.py's two-step design)
  4. customer.subscription.created with a Pro price_id upgrades the user
  5. customer.subscription.deleted downgrades to Free and cancels status
  6. An event for an unrecognized Stripe customer is a safe no-op
  7. Full realistic sequence: checkout completes, then subscription.created
     arrives — user ends up on the correct plan
"""

from __future__ import annotations
import hashlib
import hmac
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stripe
from opportunity_scanner.app_storage import AppStorage
from opportunity_scanner.plans import PlanTier
from opportunity_scanner.billing_stripe import verify_and_parse_webhook, handle_stripe_event

WEBHOOK_SECRET = "whsec_test_fake_secret_for_local_signing"
PRICE_ID_PRO = "price_fake_pro_123"
PRICE_ID_ELITE = "price_fake_elite_456"


def sign_payload(payload_dict: dict, secret: str = WEBHOOK_SECRET):
    payload_str = json.dumps(payload_dict)
    payload_bytes = payload_str.encode("utf-8")
    timestamp = int(time.time())
    signed_payload = f"{timestamp}.{payload_str}"
    signature = hmac.new(secret.encode("utf-8"), signed_payload.encode("utf-8"), hashlib.sha256).hexdigest()
    sig_header = f"t={timestamp},v1={signature}"
    return payload_bytes, sig_header


def make_checkout_completed_event(user_id: int, customer_id: str) -> dict:
    return {
        "id": "evt_fake_1", "object": "event", "api_version": "2024-06-20",
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_fake_1", "object": "checkout.session",
            "client_reference_id": str(user_id), "customer": customer_id,
            "mode": "subscription",
        }},
    }


def make_subscription_event(event_type: str, customer_id: str, price_id: str, status: str = "active") -> dict:
    return {
        "id": "evt_fake_2", "object": "event", "api_version": "2024-06-20",
        "type": event_type,
        "data": {"object": {
            "id": "sub_fake_1", "object": "subscription", "customer": customer_id, "status": status,
            "items": {"object": "list", "data": [{"id": "si_fake_1", "price": {"id": price_id, "object": "price"}}]},
        }},
    }


def main():
    db_path = "/tmp/test_billing_stripe.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    storage = AppStorage(db_path)
    user = storage.create_user("stripeuser@example.com", "password123")

    payload_bytes, sig_header = sign_payload(make_checkout_completed_event(user.id, "cus_fake_1"))
    event = verify_and_parse_webhook(payload_bytes, sig_header, WEBHOOK_SECRET)
    assert event.type == "checkout.session.completed"
    print("1. Validly signed webhook verifies successfully: OK")

    tampered_bytes = payload_bytes + b"x"
    try:
        verify_and_parse_webhook(tampered_bytes, sig_header, WEBHOOK_SECRET)
        assert False, "Expected a SignatureVerificationError for a tampered payload"
    except stripe.SignatureVerificationError:
        print("2. Tampered payload correctly rejected by signature verification: OK")

    result = handle_stripe_event(event, storage, PRICE_ID_PRO, PRICE_ID_ELITE)
    print(f"3. checkout.session.completed result: {result}")
    linked_user = storage.get_user_by_id(user.id)
    assert linked_user.stripe_customer_id == "cus_fake_1"
    assert linked_user.subscription_status == "active"
    assert linked_user.plan == PlanTier.FREE, "Plan should NOT change from checkout.session.completed alone"
    print("   Customer linked, status active, plan correctly unchanged until subscription.created arrives: OK")

    payload2, sig2 = sign_payload(make_subscription_event("customer.subscription.created", "cus_fake_1", PRICE_ID_PRO))
    event2 = verify_and_parse_webhook(payload2, sig2, WEBHOOK_SECRET)
    result2 = handle_stripe_event(event2, storage, PRICE_ID_PRO, PRICE_ID_ELITE)
    print(f"4. customer.subscription.created result: {result2}")
    upgraded_user = storage.get_user_by_id(user.id)
    assert upgraded_user.plan == PlanTier.PRO
    print("   User correctly upgraded to Pro: OK")

    payload3, sig3 = sign_payload({
        "id": "evt_fake_3", "object": "event", "api_version": "2024-06-20",
        "type": "customer.subscription.deleted",
        "data": {"object": {"id": "sub_fake_1", "object": "subscription", "customer": "cus_fake_1", "status": "canceled"}},
    })
    event3 = verify_and_parse_webhook(payload3, sig3, WEBHOOK_SECRET)
    result3 = handle_stripe_event(event3, storage, PRICE_ID_PRO, PRICE_ID_ELITE)
    print(f"5. customer.subscription.deleted result: {result3}")
    canceled_user = storage.get_user_by_id(user.id)
    assert canceled_user.plan == PlanTier.FREE
    assert canceled_user.subscription_status == "canceled"
    print("   User correctly downgraded to Free with canceled status: OK")

    payload4, sig4 = sign_payload(make_subscription_event("customer.subscription.updated", "cus_totally_unknown", PRICE_ID_ELITE))
    event4 = verify_and_parse_webhook(payload4, sig4, WEBHOOK_SECRET)
    result4 = handle_stripe_event(event4, storage, PRICE_ID_PRO, PRICE_ID_ELITE)
    assert result4 is None
    print("6. Event for an unrecognized Stripe customer is a safe no-op, not a crash: OK")

    user2 = storage.create_user("secondstripeuser@example.com", "password123")
    p1, s1 = sign_payload(make_checkout_completed_event(user2.id, "cus_fake_2"))
    handle_stripe_event(verify_and_parse_webhook(p1, s1, WEBHOOK_SECRET), storage, PRICE_ID_PRO, PRICE_ID_ELITE)
    p2, s2 = sign_payload(make_subscription_event("customer.subscription.created", "cus_fake_2", PRICE_ID_ELITE))
    handle_stripe_event(verify_and_parse_webhook(p2, s2, WEBHOOK_SECRET), storage, PRICE_ID_PRO, PRICE_ID_ELITE)
    final_user = storage.get_user_by_id(user2.id)
    assert final_user.plan == PlanTier.ELITE
    assert final_user.stripe_customer_id == "cus_fake_2"
    print("7. Full realistic checkout -> subscription.created sequence ends with the correct plan: OK")

    os.remove(db_path)
    print("\n✅ Stripe billing test passed: signature verification (valid + tampered), the two-step link-then-upgrade design, downgrade on cancellation, and unknown-customer safety all verified with real locally-signed payloads.")


if __name__ == "__main__":
    main()

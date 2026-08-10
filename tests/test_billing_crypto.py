"""
NowPayments crypto billing test — constructs REAL HMAC-SHA512 signed IPN
payloads locally using NowPayments' documented signature scheme.

Checks:
  1. A validly signed IPN payload verifies successfully
  2. A tampered payload is rejected
  3. payment_status="finished" activates the correct plan for the correct user
  4. Intermediate statuses are safe no-ops, not treated as success
  5. An unknown user_id in the order_id is a safe no-op, not a crash
  6. A malformed order_id is a safe no-op, not a crash
"""

from __future__ import annotations
import hashlib
import hmac
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.app_storage import AppStorage
from opportunity_scanner.plans import PlanTier
from opportunity_scanner.billing_crypto import verify_ipn_signature, handle_crypto_ipn_event, _encode_order_id

IPN_SECRET = "fake_ipn_secret_for_local_signing"


def sign_ipn(payload: dict, secret: str = IPN_SECRET) -> str:
    sorted_payload = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hmac.new(secret.encode("utf-8"), sorted_payload.encode("utf-8"), hashlib.sha512).hexdigest()


def main():
    db_path = "/tmp/test_billing_crypto.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    storage = AppStorage(db_path)
    user = storage.create_user("cryptouser@example.com", "password123")

    payload = {
        "payment_id": 123456789, "payment_status": "finished",
        "order_id": _encode_order_id(user.id, PlanTier.ELITE),
        "price_amount": 89.0, "price_currency": "usd", "pay_amount": 0.002, "pay_currency": "btc",
        "actually_paid": 0.002, "outcome_amount": 88.5, "outcome_currency": "btc",
    }
    valid_sig = sign_ipn(payload)
    assert verify_ipn_signature(payload, IPN_SECRET, valid_sig) is True
    print("1. Validly signed IPN payload verifies successfully: OK")

    tampered = dict(payload)
    tampered["price_amount"] = 0.01
    assert verify_ipn_signature(tampered, IPN_SECRET, valid_sig) is False
    print("2. Tampered payload (price changed after signing) correctly rejected: OK")

    result = handle_crypto_ipn_event(payload, storage)
    print(f"3. Finished payment result: {result}")
    upgraded_user = storage.get_user_by_id(user.id)
    assert upgraded_user.plan == PlanTier.ELITE
    assert upgraded_user.subscription_status == "active"
    print("   User correctly upgraded to Elite via crypto payment: OK")

    for status in ("waiting", "confirming", "partially_paid"):
        pending_payload = dict(payload)
        pending_payload["payment_status"] = status
        pending_payload["order_id"] = _encode_order_id(user.id, PlanTier.PRO)
        result_pending = handle_crypto_ipn_event(pending_payload, storage)
        assert result_pending is None, f"Expected status '{status}' to be a no-op, got {result_pending}"
    still_elite = storage.get_user_by_id(user.id)
    assert still_elite.plan == PlanTier.ELITE, "Intermediate statuses should never change the plan"
    print("4. Intermediate statuses (waiting/confirming/partially_paid) are safe no-ops, plan unchanged: OK")

    unknown_user_payload = dict(payload)
    unknown_user_payload["order_id"] = _encode_order_id(999999, PlanTier.PRO)
    result_unknown = handle_crypto_ipn_event(unknown_user_payload, storage)
    assert result_unknown is None
    print("5. Unknown user_id in order_id is a safe no-op, not a crash: OK")

    malformed_payload = dict(payload)
    malformed_payload["order_id"] = "not_a_valid_order_id_format"
    result_malformed = handle_crypto_ipn_event(malformed_payload, storage)
    assert result_malformed is None
    print("6. Malformed order_id is a safe no-op, not a crash: OK")

    os.remove(db_path)
    print("\n✅ Crypto billing test passed: IPN signature verification (valid + tampered), successful-payment plan activation, intermediate-status safety, and unknown/malformed order_id safety all verified with real locally-signed payloads.")


if __name__ == "__main__":
    main()

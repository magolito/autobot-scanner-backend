"""
Webhook route test — hits the actual FastAPI routes via TestClient,
verifying the HTTP-level contract: correct signature -> 200, bad
signature -> 400 (not silently accepted), billing not configured -> 503.

Checks:
  1. Stripe webhook: unconfigured -> 503
  2. Stripe webhook: valid signature -> 200, event processed end-to-end
  3. Stripe webhook: invalid signature -> 400, NOT 200
  4. NowPayments webhook: unconfigured -> 503
  5. NowPayments webhook: valid signature -> 200, event processed end-to-end
  6. NowPayments webhook: invalid signature -> 400
"""

from __future__ import annotations
import hashlib
import hmac
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    os.environ["APP_DB_PATH"] = "/tmp/test_webhook_routes.db"
    os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test_route_secret"
    os.environ["NOWPAYMENTS_IPN_SECRET"] = "ipn_test_route_secret"

    if os.path.exists("/tmp/test_webhook_routes.db"):
        os.remove("/tmp/test_webhook_routes.db")

    try:
        from fastapi.testclient import TestClient
        from opportunity_scanner.api import app
        from opportunity_scanner.app_storage import AppStorage

        storage = AppStorage("/tmp/test_webhook_routes.db")
        user = storage.create_user("webhookrouteuser@example.com", "password123")

        client = TestClient(app)

        resp = client.post("/webhooks/stripe", content=b"{}", headers={"stripe-signature": "fake"})
        assert resp.status_code == 503, f"Expected 503 when stripe.enabled is False, got {resp.status_code}"
        print("1. Stripe webhook correctly returns 503 when billing isn't enabled: OK")

        os.environ["STRIPE__ENABLED"] = "true"

        payload_dict = {
            "id": "evt_route_1", "object": "event", "api_version": "2024-06-20",
            "type": "checkout.session.completed",
            "data": {"object": {"id": "cs_1", "object": "checkout.session", "client_reference_id": str(user.id), "customer": "cus_route_1"}},
        }
        payload_str = json.dumps(payload_dict)
        timestamp = int(time.time())
        signed = f"{timestamp}.{payload_str}"
        sig = hmac.new(b"whsec_test_route_secret", signed.encode(), hashlib.sha256).hexdigest()
        sig_header = f"t={timestamp},v1={sig}"

        resp2 = client.post("/webhooks/stripe", content=payload_str.encode(), headers={"stripe-signature": sig_header})
        print(f"2. Valid Stripe webhook response: {resp2.status_code} {resp2.json()}")
        assert resp2.status_code == 200
        assert resp2.json()["handled"] is True

        linked = storage.get_user_by_id(user.id)
        assert linked.stripe_customer_id == "cus_route_1"
        print("   Route correctly processed the event end-to-end (user linked in DB): OK")

        resp3 = client.post("/webhooks/stripe", content=payload_str.encode(), headers={"stripe-signature": "t=123,v1=deadbeef"})
        assert resp3.status_code == 400, f"Expected 400 for a forged signature, got {resp3.status_code}"
        print("3. Invalid Stripe signature correctly rejected with 400, never 200: OK")

        resp4 = client.post("/webhooks/nowpayments", json={}, headers={"x-nowpayments-sig": "fake"})
        assert resp4.status_code == 503
        print("4. NowPayments webhook correctly returns 503 when billing isn't enabled: OK")

        os.environ["CRYPTO_PAYMENTS__ENABLED"] = "true"

        from opportunity_scanner.billing_crypto import _encode_order_id
        from opportunity_scanner.plans import PlanTier
        ipn_payload = {"payment_id": 1, "payment_status": "finished", "order_id": _encode_order_id(user.id, PlanTier.PRO)}
        sorted_payload = json.dumps(ipn_payload, separators=(",", ":"), sort_keys=True)
        ipn_sig = hmac.new(b"ipn_test_route_secret", sorted_payload.encode(), hashlib.sha512).hexdigest()

        resp5 = client.post("/webhooks/nowpayments", json=ipn_payload, headers={"x-nowpayments-sig": ipn_sig})
        print(f"5. Valid NowPayments webhook response: {resp5.status_code} {resp5.json()}")
        assert resp5.status_code == 200
        assert resp5.json()["handled"] is True
        upgraded = storage.get_user_by_id(user.id)
        assert upgraded.plan == PlanTier.PRO
        print("   Route correctly processed the event end-to-end (user upgraded in DB): OK")

        resp6 = client.post("/webhooks/nowpayments", json=ipn_payload, headers={"x-nowpayments-sig": "wrongsignature"})
        assert resp6.status_code == 400
        print("6. Invalid NowPayments signature correctly rejected with 400: OK")

        print("\n✅ Webhook route test passed: both endpoints correctly gate on configuration, accept valid signatures end-to-end, and reject forged signatures with 400 rather than silently succeeding.")

    finally:
        for k in ["APP_DB_PATH", "STRIPE_WEBHOOK_SECRET", "NOWPAYMENTS_IPN_SECRET", "STRIPE__ENABLED", "CRYPTO_PAYMENTS__ENABLED"]:
            os.environ.pop(k, None)
        if os.path.exists("/tmp/test_webhook_routes.db"):
            os.remove("/tmp/test_webhook_routes.db")


if __name__ == "__main__":
    main()

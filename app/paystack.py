"""Thin Paystack API client — two free functions, not a class, so tests can
`unittest.mock.patch("app.paystack.initialize_transaction", ...)` directly
without touching requests internals. Unlike app/geo.py's fail-open lookup
(a flaky geo call must never block a login), a payment call's failure has
to reach the caller — silently swallowing it would either lose money or
leave a customer stuck mid-checkout.
"""
import hashlib
import hmac
import os

import requests

BASE_URL = "https://api.paystack.co"
_TIMEOUT = 15.0


class PaystackError(Exception):
    pass


def _headers():
    key = os.environ.get("PAYSTACK_SECRET_KEY", "").strip()
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def verify_webhook_signature(raw_body, signature_header):
    """raw_body must be the exact bytes Paystack sent (request.get_data(),
    not a re-serialized/re-parsed body) — the signature is an HMAC-SHA512
    of those exact bytes, keyed with the secret key. Paystack's dashboard
    only supports one webhook URL per account/mode, so this is shared by a
    single consolidated endpoint (app/webhooks/routes.py) rather than
    duplicated per product.
    """
    if not signature_header:
        return False
    secret = os.environ.get("PAYSTACK_SECRET_KEY", "").strip().encode()
    expected = hmac.new(secret, raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(signature_header, expected)


def initialize_transaction(email, amount_kobo, reference, callback_url, metadata=None):
    """amount_kobo must already be the integer subunit amount (naira * 100
    for NGN) — callers must never pass a client-submitted value here.
    Returns the response's `data` dict (has `authorization_url`, `reference`).
    """
    payload = {
        "email": email,
        "amount": amount_kobo,
        "reference": reference,
        "callback_url": callback_url,
    }
    if metadata:
        payload["metadata"] = metadata
    try:
        resp = requests.post(
            f"{BASE_URL}/transaction/initialize",
            json=payload,
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        body = resp.json()
    except (requests.RequestException, ValueError) as e:
        raise PaystackError(f"Initialize transaction request failed: {e}") from e
    if not resp.ok or not body.get("status"):
        raise PaystackError(f"Paystack rejected initialize: {body.get('message', body)}")
    return body["data"]


def verify_transaction(reference):
    """Returns the response's `data` dict — data['status'] is the actual
    transaction outcome ('success'/'failed'/'abandoned'), a different field
    than this call's own success/failure.
    """
    try:
        resp = requests.get(
            f"{BASE_URL}/transaction/verify/{reference}",
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        body = resp.json()
    except (requests.RequestException, ValueError) as e:
        raise PaystackError(f"Verify transaction request failed: {e}") from e
    if not resp.ok or not body.get("status"):
        raise PaystackError(f"Paystack rejected verify: {body.get('message', body)}")
    return body["data"]

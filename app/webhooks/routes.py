"""Single consolidated Paystack webhook endpoint. Paystack's dashboard only
supports ONE webhook URL per account/mode — it is not possible to register
a separate URL per product (Master Class, marketplace, ...) — so every
Paystack event lands here and gets dispatched by the payment reference's
prefix rather than each product owning its own webhook route.
"""
import logging

from flask import request

from . import bp
from .. import paystack
from ..extensions import csrf
from ..marketplace.services import mark_order_paid
from ..master_class.services import mark_enrollment_paid
from ..models import ChannelOrder, MasterClassEnrollment

logger = logging.getLogger(__name__)


def _confirm_and_mark(reference, record, mark_fn):
    expected_kobo = int(round(record.amount * 100))
    try:
        verified = paystack.verify_transaction(reference)
    except paystack.PaystackError as e:
        logger.warning("Webhook re-verify failed for %s: %s", reference, e)
        return
    # Belt-and-suspenders per Paystack's own guidance: don't grant value off
    # the webhook payload alone even after a valid signature — re-verify.
    if verified.get("status") == "success" and int(verified.get("amount", -1)) == expected_kobo:
        mark_fn(record)


@bp.route("/paystack", methods=["POST"])
@csrf.exempt
def paystack_webhook():
    raw = request.get_data()
    signature = request.headers.get("X-Paystack-Signature", "")

    if not paystack.verify_webhook_signature(raw, signature):
        logger.warning("Rejected Paystack webhook: signature mismatch")
        return {"status": "invalid signature"}, 400

    payload = request.get_json(silent=True) or {}
    if payload.get("event") != "charge.success":
        return {"status": "ignored"}, 200

    data = payload.get("data") or {}
    reference = data.get("reference") or ""

    # Reference prefixes are assigned at creation time — "mc-" in
    # master_class/routes.py:enroll(), "ch-" in marketplace/routes.py:buy() —
    # specifically so a single webhook endpoint can tell them apart.
    if reference.startswith("mc-"):
        enrollment = MasterClassEnrollment.query.filter_by(paystack_reference=reference).first()
        if enrollment is None:
            logger.warning("Paystack webhook for unknown Master Class reference %r", reference)
            return {"status": "ignored"}, 200
        if enrollment.status == "paid":
            return {"status": "already processed"}, 200
        _confirm_and_mark(reference, enrollment, mark_enrollment_paid)
        return {"status": "ok"}, 200

    if reference.startswith("ch-"):
        order = ChannelOrder.query.filter_by(paystack_reference=reference).first()
        if order is None:
            logger.warning("Paystack webhook for unknown marketplace reference %r", reference)
            return {"status": "ignored"}, 200
        if order.status == "paid":
            return {"status": "already processed"}, 200
        _confirm_and_mark(reference, order, mark_order_paid)
        return {"status": "ok"}, 200

    logger.warning("Paystack webhook for unrecognized reference format %r", reference)
    return {"status": "ignored"}, 200

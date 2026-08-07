"""Tests for Paystack-backed Master Class enrollment: server-side pricing,
webhook signature verification, idempotency (no double commission on
duplicate webhook delivery), the callback route not trusting the query
string alone, and commission-rate resolution matching the admin path.
"""
import hashlib
import hmac
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("CHANNEL_TOKEN_ENC_KEY", "test-channel-token-enc-key")
os.environ.setdefault("PAYSTACK_SECRET_KEY", "test-paystack-secret")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8000")

from app import create_app
from app.config import Config
from app.extensions import db
from app.models import (
    Commission,
    MasterClassEnrollment,
    MasterClassSettings,
    Prospect,
    User,
)
from app.paystack import PaystackError

PAYSTACK_SECRET = os.environ["PAYSTACK_SECRET_KEY"]


def _sign(body_bytes):
    return hmac.new(PAYSTACK_SECRET.encode(), body_bytes, hashlib.sha512).hexdigest()


class _TestConfig(Config):
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False


class _DbTestCase(unittest.TestCase):
    config_class = _TestConfig

    def setUp(self):
        self.app = create_app(self.config_class)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        settings = MasterClassSettings.get()
        settings.price_amount = 50000.00
        settings.currency = "NGN"
        settings.is_open_for_enrollment = True
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _make_affiliate(self, email, referral_code, rate_override=None):
        user = User(
            email=email, role="affiliate", referral_code=referral_code,
            commission_rate_percent=rate_override,
        )
        user.set_password("x")
        db.session.add(user)
        db.session.commit()
        return user

    def _enroll(self, name="Buyer One", email="buyer@example.com", ref_code=""):
        return self.client.post(
            "/master-class/enroll",
            data={"name": name, "email": email, "ref_code": ref_code},
        )

    def _charge_success_payload(self, reference, amount_kobo):
        return json.dumps(
            {"event": "charge.success", "data": {"reference": reference, "amount": amount_kobo}}
        ).encode()

    def _post_webhook(self, body_bytes, signature=None):
        headers = {"Content-Type": "application/json"}
        if signature is not None:
            headers["X-Paystack-Signature"] = signature
        return self.client.post("/webhooks/paystack", data=body_bytes, headers=headers)


class EnrollPricingTests(_DbTestCase):
    @patch("app.paystack.initialize_transaction")
    def test_enroll_uses_server_side_price(self, mock_init):
        mock_init.return_value = {"authorization_url": "https://paystack.test/pay/abc"}
        resp = self._enroll()
        self.assertEqual(resp.status_code, 302)

        enrollment = MasterClassEnrollment.query.filter_by(buyer_email="buyer@example.com").first()
        self.assertIsNotNone(enrollment)
        self.assertEqual(float(enrollment.amount), 50000.00)
        self.assertEqual(enrollment.status, "pending")

        # The amount Paystack was actually asked to charge, in kobo.
        _, kwargs = mock_init.call_args
        self.assertEqual(kwargs["amount_kobo"], 5000000)

    @patch("app.paystack.initialize_transaction")
    def test_enroll_closed_rejects(self, mock_init):
        MasterClassSettings.get().is_open_for_enrollment = False
        db.session.commit()
        resp = self._enroll()
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(MasterClassEnrollment.query.count(), 0)
        mock_init.assert_not_called()

    @patch("app.paystack.initialize_transaction")
    def test_enroll_attributes_to_valid_referral_code(self, mock_init):
        mock_init.return_value = {"authorization_url": "https://paystack.test/pay/abc"}
        affiliate = self._make_affiliate("aff@example.com", "REFCODE1")
        self._enroll(ref_code="refcode1")  # lowercase — route upper()s it
        enrollment = MasterClassEnrollment.query.filter_by(buyer_email="buyer@example.com").first()
        self.assertEqual(enrollment.affiliate_id, affiliate.id)
        self.assertEqual(enrollment.referral_code_used, "REFCODE1")

    @patch("app.paystack.initialize_transaction")
    def test_enroll_unknown_referral_code_is_unattributed(self, mock_init):
        mock_init.return_value = {"authorization_url": "https://paystack.test/pay/abc"}
        self._enroll(ref_code="NOTREAL1")
        enrollment = MasterClassEnrollment.query.filter_by(buyer_email="buyer@example.com").first()
        self.assertIsNone(enrollment.affiliate_id)

    @patch("app.paystack.initialize_transaction")
    def test_enroll_links_existing_prospect(self, mock_init):
        mock_init.return_value = {"authorization_url": "https://paystack.test/pay/abc"}
        prospect = Prospect(
            name="Buyer One", email="buyer@example.com", interest_type="master_class",
        )
        db.session.add(prospect)
        db.session.commit()
        self._enroll()
        enrollment = MasterClassEnrollment.query.filter_by(buyer_email="buyer@example.com").first()
        self.assertEqual(enrollment.prospect_id, prospect.id)

    @patch("app.paystack.initialize_transaction")
    def test_paystack_initialize_failure_marks_enrollment_failed(self, mock_init):
        mock_init.side_effect = PaystackError("boom")
        resp = self._enroll()
        self.assertEqual(resp.status_code, 302)
        enrollment = MasterClassEnrollment.query.filter_by(buyer_email="buyer@example.com").first()
        self.assertEqual(enrollment.status, "failed")


class WebhookTests(_DbTestCase):
    def _pending_enrollment(self, affiliate=None, amount=50000.00):
        enrollment = MasterClassEnrollment(
            buyer_name="Buyer One", buyer_email="buyer@example.com",
            affiliate_id=affiliate.id if affiliate else None,
            amount=amount, currency="NGN", paystack_reference="mc-test-ref",
        )
        db.session.add(enrollment)
        db.session.commit()
        return enrollment

    def test_valid_signature_marks_paid_and_creates_commission(self):
        affiliate = self._make_affiliate("aff@example.com", "AAAAAAAA", rate_override=20)
        enrollment = self._pending_enrollment(affiliate=affiliate)
        body = self._charge_success_payload(enrollment.paystack_reference, 5000000)

        with patch("app.paystack.verify_transaction") as mock_verify:
            mock_verify.return_value = {"status": "success", "amount": 5000000}
            resp = self._post_webhook(body, signature=_sign(body))

        self.assertEqual(resp.status_code, 200)
        db.session.refresh(enrollment)
        self.assertEqual(enrollment.status, "paid")
        self.assertIsNotNone(enrollment.paid_at)

        commissions = Commission.query.filter_by(source_enrollment_id=enrollment.id).all()
        self.assertEqual(len(commissions), 1)
        self.assertEqual(float(commissions[0].rate_percent_snapshot), 20.0)
        self.assertAlmostEqual(float(commissions[0].amount), 50000.00 * 0.20)
        self.assertIsNone(commissions[0].created_by_id)

    def test_default_rate_used_when_no_override(self):
        affiliate = self._make_affiliate("aff2@example.com", "BBBBBBBB")  # no override -> default 10
        enrollment = self._pending_enrollment(affiliate=affiliate)
        body = self._charge_success_payload(enrollment.paystack_reference, 5000000)

        with patch("app.paystack.verify_transaction") as mock_verify:
            mock_verify.return_value = {"status": "success", "amount": 5000000}
            self._post_webhook(body, signature=_sign(body))

        commission = Commission.query.filter_by(source_enrollment_id=enrollment.id).first()
        self.assertEqual(float(commission.rate_percent_snapshot), 10.0)

    def test_unattributed_enrollment_creates_no_commission(self):
        enrollment = self._pending_enrollment(affiliate=None)
        body = self._charge_success_payload(enrollment.paystack_reference, 5000000)

        with patch("app.paystack.verify_transaction") as mock_verify:
            mock_verify.return_value = {"status": "success", "amount": 5000000}
            self._post_webhook(body, signature=_sign(body))

        db.session.refresh(enrollment)
        self.assertEqual(enrollment.status, "paid")
        self.assertEqual(Commission.query.count(), 0)

    def test_invalid_signature_rejected_nothing_paid(self):
        enrollment = self._pending_enrollment()
        body = self._charge_success_payload(enrollment.paystack_reference, 5000000)

        with patch("app.paystack.verify_transaction") as mock_verify:
            resp = self._post_webhook(body, signature="deadbeef" * 8)
            mock_verify.assert_not_called()

        self.assertEqual(resp.status_code, 400)
        db.session.refresh(enrollment)
        self.assertEqual(enrollment.status, "pending")
        self.assertEqual(Commission.query.count(), 0)

    def test_missing_signature_rejected(self):
        enrollment = self._pending_enrollment()
        body = self._charge_success_payload(enrollment.paystack_reference, 5000000)
        resp = self._post_webhook(body, signature=None)
        self.assertEqual(resp.status_code, 400)
        db.session.refresh(enrollment)
        self.assertEqual(enrollment.status, "pending")

    def test_duplicate_delivery_creates_only_one_commission(self):
        affiliate = self._make_affiliate("aff3@example.com", "CCCCCCCC")
        enrollment = self._pending_enrollment(affiliate=affiliate)
        body = self._charge_success_payload(enrollment.paystack_reference, 5000000)
        sig = _sign(body)

        with patch("app.paystack.verify_transaction") as mock_verify:
            mock_verify.return_value = {"status": "success", "amount": 5000000}
            self._post_webhook(body, signature=sig)
            self._post_webhook(body, signature=sig)  # Paystack-style redelivery

        self.assertEqual(
            Commission.query.filter_by(source_enrollment_id=enrollment.id).count(), 1
        )

    def test_reverify_failure_does_not_mark_paid(self):
        enrollment = self._pending_enrollment()
        body = self._charge_success_payload(enrollment.paystack_reference, 5000000)

        with patch("app.paystack.verify_transaction") as mock_verify:
            mock_verify.return_value = {"status": "failed", "amount": 5000000}
            self._post_webhook(body, signature=_sign(body))

        db.session.refresh(enrollment)
        self.assertEqual(enrollment.status, "pending")

    def test_unknown_event_ignored(self):
        enrollment = self._pending_enrollment()
        body = json.dumps(
            {"event": "charge.failed", "data": {"reference": enrollment.paystack_reference}}
        ).encode()
        resp = self._post_webhook(body, signature=_sign(body))
        self.assertEqual(resp.status_code, 200)
        db.session.refresh(enrollment)
        self.assertEqual(enrollment.status, "pending")

    def test_unknown_reference_ignored_not_500(self):
        body = self._charge_success_payload("no-such-reference", 5000000)
        resp = self._post_webhook(body, signature=_sign(body))
        self.assertEqual(resp.status_code, 200)


class CallbackTests(_DbTestCase):
    def _pending_enrollment(self):
        enrollment = MasterClassEnrollment(
            buyer_name="Buyer One", buyer_email="buyer@example.com",
            amount=50000.00, currency="NGN", paystack_reference="mc-callback-ref",
        )
        db.session.add(enrollment)
        db.session.commit()
        return enrollment

    def test_callback_does_not_trust_query_string_alone(self):
        enrollment = self._pending_enrollment()
        with patch("app.paystack.verify_transaction") as mock_verify:
            mock_verify.return_value = {"status": "failed", "amount": 5000000}
            resp = self.client.get(
                f"/master-class/callback?reference={enrollment.paystack_reference}&status=success"
            )
        self.assertEqual(resp.status_code, 200)
        db.session.refresh(enrollment)
        self.assertEqual(enrollment.status, "pending")

    def test_callback_confirms_via_verify(self):
        enrollment = self._pending_enrollment()
        with patch("app.paystack.verify_transaction") as mock_verify:
            mock_verify.return_value = {"status": "success", "amount": 5000000}
            resp = self.client.get(f"/master-class/callback?reference={enrollment.paystack_reference}")
        self.assertEqual(resp.status_code, 200)
        db.session.refresh(enrollment)
        self.assertEqual(enrollment.status, "paid")

    def test_callback_already_paid_skips_reverify(self):
        enrollment = self._pending_enrollment()
        enrollment.status = "paid"
        db.session.commit()
        with patch("app.paystack.verify_transaction") as mock_verify:
            resp = self.client.get(f"/master-class/callback?reference={enrollment.paystack_reference}")
            mock_verify.assert_not_called()
        self.assertEqual(resp.status_code, 200)


class WebhookCsrfExemptionTests(_DbTestCase):
    """A dedicated subclass with CSRF actually enabled, so the webhook
    route's @csrf.exempt is genuinely exercised rather than masked by the
    shared test config's blanket WTF_CSRF_ENABLED=False.
    """

    class _CsrfOnConfig(_TestConfig):
        WTF_CSRF_ENABLED = True

    config_class = _CsrfOnConfig

    def test_webhook_post_succeeds_without_csrf_token(self):
        enrollment = MasterClassEnrollment(
            buyer_name="Buyer One", buyer_email="buyer@example.com",
            amount=50000.00, currency="NGN", paystack_reference="mc-csrf-ref",
        )
        db.session.add(enrollment)
        db.session.commit()

        body = self._charge_success_payload(enrollment.paystack_reference, 5000000)
        with patch("app.paystack.verify_transaction") as mock_verify:
            mock_verify.return_value = {"status": "success", "amount": 5000000}
            resp = self._post_webhook(body, signature=_sign(body))
        # If CSRF protection applied to this route, a token-free POST would
        # 400 with "CSRF token is missing" before ever reaching our handler.
        self.assertEqual(resp.status_code, 200)
        db.session.refresh(enrollment)
        self.assertEqual(enrollment.status, "paid")


if __name__ == "__main__":
    unittest.main()

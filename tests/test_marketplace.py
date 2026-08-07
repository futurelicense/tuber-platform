"""Tests for the channel marketplace: server-side pricing, the two-stage
locking mechanism (reserve at checkout, finalize at payment) that prevents
two buyers from both paying for the same unique listing, webhook signature
verification/idempotency (same machinery as Master Class), and the
payment_conflict fallback for the rare case both stages still race.
"""
import hashlib
import hmac
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
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
from app.marketplace import services
from app.models import ChannelListing, ChannelOrder, Commission, Prospect, User
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
        self.admin = User(email="admin@example.com", role="admin")
        self.admin.set_password("x")
        db.session.add(self.admin)
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

    def _make_listing(self, price=200000.00, status="published", availability="available"):
        listing = ChannelListing(
            title="Cooking Channel", monetization_status="monetized",
            price=price, currency="NGN", status=status, availability=availability,
            created_by_id=self.admin.id,
        )
        db.session.add(listing)
        db.session.commit()
        return listing

    def _buy(self, listing_id, name="Buyer One", email="buyer@example.com", ref_code=""):
        return self.client.post(
            f"/marketplace/{listing_id}/buy",
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


class BuyPricingTests(_DbTestCase):
    @patch("app.paystack.initialize_transaction")
    def test_buy_uses_listing_server_side_price(self, mock_init):
        mock_init.return_value = {"authorization_url": "https://paystack.test/pay/abc"}
        listing = self._make_listing(price=200000.00)
        resp = self._buy(listing.id)
        self.assertEqual(resp.status_code, 302)

        order = ChannelOrder.query.filter_by(listing_id=listing.id).first()
        self.assertIsNotNone(order)
        self.assertEqual(float(order.amount), 200000.00)

        _, kwargs = mock_init.call_args
        self.assertEqual(kwargs["amount_kobo"], 20000000)

    @patch("app.paystack.initialize_transaction")
    def test_buy_reserves_the_listing(self, mock_init):
        mock_init.return_value = {"authorization_url": "https://paystack.test/pay/abc"}
        listing = self._make_listing()
        self._buy(listing.id)
        db.session.refresh(listing)
        self.assertEqual(listing.availability, "reserved")
        order = ChannelOrder.query.filter_by(listing_id=listing.id).first()
        self.assertEqual(listing.holder_order_id, order.id)

    @patch("app.paystack.initialize_transaction")
    def test_buy_attributes_to_valid_referral_code(self, mock_init):
        mock_init.return_value = {"authorization_url": "https://paystack.test/pay/abc"}
        affiliate = self._make_affiliate("aff@example.com", "REFCODE1")
        listing = self._make_listing()
        self._buy(listing.id, ref_code="refcode1")
        order = ChannelOrder.query.filter_by(listing_id=listing.id).first()
        self.assertEqual(order.affiliate_id, affiliate.id)

    @patch("app.paystack.initialize_transaction")
    def test_buy_links_existing_prospect(self, mock_init):
        mock_init.return_value = {"authorization_url": "https://paystack.test/pay/abc"}
        prospect = Prospect(name="Buyer One", email="buyer@example.com", interest_type="buy_channel")
        db.session.add(prospect)
        db.session.commit()
        listing = self._make_listing()
        self._buy(listing.id)
        order = ChannelOrder.query.filter_by(listing_id=listing.id).first()
        self.assertEqual(order.prospect_id, prospect.id)

    @patch("app.paystack.initialize_transaction")
    def test_initialize_failure_marks_failed_and_releases_listing(self, mock_init):
        mock_init.side_effect = PaystackError("boom")
        listing = self._make_listing()
        self._buy(listing.id)
        order = ChannelOrder.query.filter_by(listing_id=listing.id).first()
        self.assertEqual(order.status, "failed")
        db.session.refresh(listing)
        self.assertEqual(listing.availability, "available")
        self.assertIsNone(listing.holder_order_id)

    def test_cannot_buy_a_draft_listing(self):
        listing = self._make_listing(status="draft")
        resp = self._buy(listing.id)
        self.assertEqual(resp.status_code, 404)


class ConcurrencyLockingTests(_DbTestCase):
    """Sequential double-invocation is the standard way this kind of
    UPDATE...WHERE guard gets proven in this test suite (SQLite's single
    test connection can't model true concurrent writers, and there's no
    existing precedent here for a real multi-threaded test — see
    suggestions/routes.py's _claim, which has the same shape and no test
    of its own today either).
    """

    def test_second_reservation_attempt_loses(self):
        listing = self._make_listing()
        order_a = ChannelOrder(
            listing_id=listing.id, buyer_name="A", buyer_email="a@example.com",
            amount=listing.price, currency="NGN", paystack_reference="ch-a",
        )
        order_b = ChannelOrder(
            listing_id=listing.id, buyer_name="B", buyer_email="b@example.com",
            amount=listing.price, currency="NGN", paystack_reference="ch-b",
        )
        db.session.add_all([order_a, order_b])
        db.session.commit()

        self.assertTrue(services.reserve_listing(listing.id, order_a.id))
        db.session.commit()
        self.assertFalse(services.reserve_listing(listing.id, order_b.id))
        db.session.rollback()

    @patch("app.paystack.initialize_transaction")
    def test_two_buy_requests_only_one_order_persists(self, mock_init):
        mock_init.return_value = {"authorization_url": "https://paystack.test/pay/abc"}
        listing = self._make_listing()

        first = self._buy(listing.id, email="first@example.com")
        second = self._buy(listing.id, email="second@example.com")

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)  # redirected back with a flash, not a crash
        self.assertEqual(ChannelOrder.query.count(), 1)
        self.assertEqual(mock_init.call_count, 1)

    def test_stale_reservation_can_be_reclaimed(self):
        listing = self._make_listing(availability="reserved")
        stale_order = ChannelOrder(
            listing_id=listing.id, buyer_name="Stale", buyer_email="stale@example.com",
            amount=listing.price, currency="NGN", paystack_reference="ch-stale",
        )
        db.session.add(stale_order)
        db.session.commit()
        listing.holder_order_id = stale_order.id
        listing.reserved_at = datetime.now(timezone.utc) - timedelta(
            minutes=services.RESERVATION_TTL_MINUTES + 5
        )
        db.session.commit()

        fresh_order = ChannelOrder(
            listing_id=listing.id, buyer_name="Fresh", buyer_email="fresh@example.com",
            amount=listing.price, currency="NGN", paystack_reference="ch-fresh",
        )
        db.session.add(fresh_order)
        db.session.commit()

        self.assertTrue(services.reserve_listing(listing.id, fresh_order.id))
        db.session.commit()
        db.session.refresh(listing)
        self.assertEqual(listing.holder_order_id, fresh_order.id)

    def test_browse_sweeps_stale_reservations(self):
        listing = self._make_listing(availability="reserved")
        listing.reserved_at = datetime.now(timezone.utc) - timedelta(
            minutes=services.RESERVATION_TTL_MINUTES + 5
        )
        db.session.commit()

        resp = self.client.get("/marketplace/")
        self.assertEqual(resp.status_code, 200)
        db.session.refresh(listing)
        self.assertEqual(listing.availability, "available")


class WebhookTests(_DbTestCase):
    def _pending_order(self, listing, affiliate=None):
        order = ChannelOrder(
            listing_id=listing.id, buyer_name="Buyer One", buyer_email="buyer@example.com",
            affiliate_id=affiliate.id if affiliate else None,
            amount=listing.price, currency="NGN", paystack_reference="ch-test-ref",
        )
        db.session.add(order)
        db.session.commit()
        listing.availability = "reserved"
        listing.holder_order_id = order.id
        listing.reserved_at = datetime.now(timezone.utc)
        db.session.commit()
        return order

    def test_valid_signature_marks_paid_sells_listing_creates_commission(self):
        affiliate = self._make_affiliate("aff@example.com", "AAAAAAAA", rate_override=15)
        listing = self._make_listing(price=200000.00)
        order = self._pending_order(listing, affiliate=affiliate)
        body = self._charge_success_payload(order.paystack_reference, 20000000)

        with patch("app.paystack.verify_transaction") as mock_verify:
            mock_verify.return_value = {"status": "success", "amount": 20000000}
            resp = self._post_webhook(body, signature=_sign(body))

        self.assertEqual(resp.status_code, 200)
        db.session.refresh(order)
        db.session.refresh(listing)
        self.assertEqual(order.status, "paid")
        self.assertEqual(listing.availability, "sold")

        commissions = Commission.query.filter_by(source_order_id=order.id).all()
        self.assertEqual(len(commissions), 1)
        self.assertEqual(float(commissions[0].rate_percent_snapshot), 15.0)
        self.assertAlmostEqual(float(commissions[0].amount), 200000.00 * 0.15)

    def test_unattributed_order_creates_no_commission(self):
        listing = self._make_listing()
        order = self._pending_order(listing, affiliate=None)
        body = self._charge_success_payload(order.paystack_reference, int(round(listing.price * 100)))

        with patch("app.paystack.verify_transaction") as mock_verify:
            mock_verify.return_value = {"status": "success", "amount": int(round(listing.price * 100))}
            self._post_webhook(body, signature=_sign(body))

        self.assertEqual(Commission.query.count(), 0)

    def test_invalid_signature_rejected(self):
        listing = self._make_listing()
        order = self._pending_order(listing)
        body = self._charge_success_payload(order.paystack_reference, 20000000)

        with patch("app.paystack.verify_transaction") as mock_verify:
            resp = self._post_webhook(body, signature="deadbeef" * 8)
            mock_verify.assert_not_called()

        self.assertEqual(resp.status_code, 400)
        db.session.refresh(order)
        self.assertEqual(order.status, "pending")

    def test_duplicate_delivery_creates_only_one_commission(self):
        affiliate = self._make_affiliate("aff2@example.com", "BBBBBBBB")
        listing = self._make_listing()
        order = self._pending_order(listing, affiliate=affiliate)
        body = self._charge_success_payload(order.paystack_reference, int(round(listing.price * 100)))
        sig = _sign(body)

        with patch("app.paystack.verify_transaction") as mock_verify:
            mock_verify.return_value = {"status": "success", "amount": int(round(listing.price * 100))}
            self._post_webhook(body, signature=sig)
            self._post_webhook(body, signature=sig)

        self.assertEqual(Commission.query.filter_by(source_order_id=order.id).count(), 1)

    def test_payment_conflict_when_listing_already_sold_to_another_order(self):
        listing = self._make_listing()
        order_a = self._pending_order(listing)  # holds the reservation
        # order_b never held the reservation (simulates it losing stage 1
        # earlier, or the reservation lapsing and someone else winning it) —
        # directly exercise mark_order_paid's stage-2 guard.
        order_b = ChannelOrder(
            listing_id=listing.id, buyer_name="B", buyer_email="b@example.com",
            amount=listing.price, currency="NGN", paystack_reference="ch-conflict",
        )
        db.session.add(order_b)
        db.session.commit()

        # order_a wins stage 2 first.
        services.mark_order_paid(order_a)
        db.session.refresh(listing)
        self.assertEqual(listing.availability, "sold")

        # order_b's payment is confirmed after the fact — must not steal the sale.
        services.mark_order_paid(order_b)
        db.session.refresh(order_b)
        self.assertEqual(order_b.status, "payment_conflict")
        self.assertEqual(Commission.query.filter_by(source_order_id=order_b.id).count(), 0)
        db.session.refresh(listing)
        self.assertEqual(listing.holder_order_id, order_a.id)


class CallbackTests(_DbTestCase):
    def _pending_order(self, listing):
        order = ChannelOrder(
            listing_id=listing.id, buyer_name="Buyer One", buyer_email="buyer@example.com",
            amount=listing.price, currency="NGN", paystack_reference="ch-callback-ref",
        )
        db.session.add(order)
        db.session.commit()
        listing.availability = "reserved"
        listing.holder_order_id = order.id
        db.session.commit()
        return order

    def test_callback_does_not_trust_query_string_alone(self):
        listing = self._make_listing()
        order = self._pending_order(listing)
        with patch("app.paystack.verify_transaction") as mock_verify:
            mock_verify.return_value = {"status": "failed", "amount": int(round(listing.price * 100))}
            resp = self.client.get(
                f"/marketplace/callback?reference={order.paystack_reference}&status=success"
            )
        self.assertEqual(resp.status_code, 200)
        db.session.refresh(order)
        self.assertEqual(order.status, "pending")

    def test_callback_confirms_via_verify(self):
        listing = self._make_listing()
        order = self._pending_order(listing)
        with patch("app.paystack.verify_transaction") as mock_verify:
            mock_verify.return_value = {"status": "success", "amount": int(round(listing.price * 100))}
            resp = self.client.get(f"/marketplace/callback?reference={order.paystack_reference}")
        self.assertEqual(resp.status_code, 200)
        db.session.refresh(order)
        self.assertEqual(order.status, "paid")


class WebhookCsrfExemptionTests(_DbTestCase):
    class _CsrfOnConfig(_TestConfig):
        WTF_CSRF_ENABLED = True

    config_class = _CsrfOnConfig

    def test_webhook_post_succeeds_without_csrf_token(self):
        listing = self._make_listing()
        order = ChannelOrder(
            listing_id=listing.id, buyer_name="Buyer One", buyer_email="buyer@example.com",
            amount=listing.price, currency="NGN", paystack_reference="ch-csrf-ref",
        )
        db.session.add(order)
        db.session.commit()
        listing.availability = "reserved"
        listing.holder_order_id = order.id
        db.session.commit()

        body = self._charge_success_payload(order.paystack_reference, int(round(listing.price * 100)))
        with patch("app.paystack.verify_transaction") as mock_verify:
            mock_verify.return_value = {"status": "success", "amount": int(round(listing.price * 100))}
            resp = self._post_webhook(body, signature=_sign(body))
        self.assertEqual(resp.status_code, 200)
        db.session.refresh(order)
        self.assertEqual(order.status, "paid")


if __name__ == "__main__":
    unittest.main()

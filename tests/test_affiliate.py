"""Tests for the Phase 1 affiliate program: signup, referral-link intake,
homepage-direct interest capture, admin commission control, affiliate
dashboard authorization, RoleGateMiddleware still excluding the new role
from /clip and /produce, and the public-homepage routing change.
"""
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("CHANNEL_TOKEN_ENC_KEY", "test-channel-token-enc-key")
os.environ.setdefault("PAYSTACK_SECRET_KEY", "test-paystack-secret")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8000")

from flask import Response
from flask_login import login_user
from PIL import Image
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.test import EnvironBuilder

from app import create_app
from app.affiliate.codes import generate_referral_code
from app.config import Config
from app.extensions import db
from app.models import AffiliateProgramSettings, Commission, LinkClick, Prospect, User
from app.mounting.role_gate import RoleGateMiddleware


class _TestConfig(Config):
    # See test_suggest_agent.py for why this must be set on the Config
    # subclass rather than app.config after create_app().
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False


class _DbTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_TestConfig)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _make_user(self, email, role, **kwargs):
        user = User(email=email, role=role, **kwargs)
        user.set_password("x")
        db.session.add(user)
        db.session.commit()
        return user

    def _login(self, email, password="x"):
        return self.client.post("/login", data={"email": email, "password": password})


class SignupTests(_DbTestCase):
    def test_signup_creates_affiliate_with_referral_code(self):
        resp = self.client.post(
            "/affiliate/signup",
            data={"display_name": "Ada", "email": "ada@example.com", "password": "longenough1"},
        )
        self.assertEqual(resp.status_code, 302)
        user = User.query.filter_by(email="ada@example.com").first()
        self.assertIsNotNone(user)
        self.assertEqual(user.role, "affiliate")
        self.assertIsNotNone(user.referral_code)
        self.assertEqual(len(user.referral_code), 8)

    def test_signup_duplicate_email_rejected(self):
        self._make_user("dupe@example.com", "affiliate", referral_code="AAAAAAAA")
        resp = self.client.post(
            "/affiliate/signup",
            data={"display_name": "X", "email": "dupe@example.com", "password": "longenough1"},
        )
        self.assertEqual(resp.status_code, 200)  # re-renders the form, no redirect
        self.assertEqual(User.query.filter_by(email="dupe@example.com").count(), 1)

    def test_generate_referral_code_retries_on_collision(self):
        self._make_user("taken@example.com", "affiliate", referral_code="AAAAAAAA")
        calls = iter(["AAAAAAAA", "BBBBBBBB"])  # first collides, second is free
        import app.affiliate.codes as codes_module

        original_choice = codes_module.secrets.choice
        seq = iter("AAAAAAAABBBBBBBB")

        def fake_choice(alphabet):
            return next(seq)

        codes_module.secrets.choice = fake_choice
        try:
            code = generate_referral_code()
        finally:
            codes_module.secrets.choice = original_choice
        self.assertEqual(code, "BBBBBBBB")


class ReferralCaptureTests(_DbTestCase):
    """/r/<code> is now a pure redirect to a real buy page (default: Academy
    signup) rather than rendering the lead form directly. The lead form
    moved to /r/<code>/contact as an explicit fallback.
    """

    def setUp(self):
        super().setUp()
        self.affiliate = self._make_user(
            "affiliate@example.com", "affiliate", referral_code="REF12345"
        )

    def test_valid_code_redirects_to_default_landing(self):
        resp = self.client.get("/r/REF12345")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/academy/signup", resp.headers["Location"])
        self.assertIn("ref=REF12345", resp.headers["Location"])

    def test_valid_code_redirect_records_one_click(self):
        self.client.get("/r/REF12345")
        click = LinkClick.query.filter_by(affiliate_id=self.affiliate.id).first()
        self.assertIsNotNone(click)
        self.assertEqual(click.destination, "academy_signup")

    def test_unknown_code_redirects_without_creating_prospect_or_click(self):
        resp = self.client.get("/r/NOTREAL1")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Prospect.query.count(), 0)
        self.assertEqual(LinkClick.query.count(), 0)

    def test_contact_fallback_renders_intake_form(self):
        resp = self.client.get("/r/REF12345/contact")
        self.assertEqual(resp.status_code, 200)

    def test_contact_fallback_post_creates_attributed_prospect(self):
        resp = self.client.post(
            "/r/ref12345/contact",  # lowercase in the URL — contact() upper()s it
            data={
                "name": "Prospect One",
                "email": "p1@example.com",
                "interest_type": "grow_from_scratch",
            },
        )
        self.assertEqual(resp.status_code, 200)
        prospect = Prospect.query.filter_by(email="p1@example.com").first()
        self.assertIsNotNone(prospect)
        self.assertEqual(prospect.affiliate_id, self.affiliate.id)
        self.assertEqual(prospect.referral_code_used, "REF12345")

    def test_contact_fallback_invalid_interest_type_rejected(self):
        resp = self.client.post(
            "/r/REF12345/contact",
            data={"name": "Bad Type", "email": "bad@example.com", "interest_type": "not_real"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Prospect.query.count(), 0)

    def test_admin_configured_landing_changes_redirect(self):
        settings = AffiliateProgramSettings.get()
        settings.default_landing = "marketplace"
        db.session.commit()
        resp = self.client.get("/r/REF12345")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/marketplace", resp.headers["Location"])

    def test_contact_landing_choice_redirects_to_contact_form(self):
        settings = AffiliateProgramSettings.get()
        settings.default_landing = "contact"
        db.session.commit()
        resp = self.client.get("/r/REF12345")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/r/REF12345/contact", resp.headers["Location"])


class InterestCaptureTests(_DbTestCase):
    def test_creates_unattributed_prospect(self):
        resp = self.client.post(
            "/interest",
            data={
                "name": "Direct Lead",
                "email": "direct@example.com",
                "interest_type": "master_class",
            },
        )
        self.assertEqual(resp.status_code, 302)
        prospect = Prospect.query.filter_by(email="direct@example.com").first()
        self.assertIsNotNone(prospect)
        self.assertIsNone(prospect.affiliate_id)


class AdminCommissionTests(_DbTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self._make_user("admin@example.com", "admin")
        self.affiliate = self._make_user(
            "aff@example.com", "affiliate", referral_code="ABCDEFGH"
        )

    def test_default_rate_applied_when_no_override(self):
        self._login("admin@example.com")
        resp = self.client.post(
            f"/admin/affiliates/{self.affiliate.id}/commissions/new",
            data={"amount": "50.00", "note": "test"},
        )
        self.assertEqual(resp.status_code, 302)
        commission = Commission.query.filter_by(affiliate_id=self.affiliate.id).first()
        self.assertIsNotNone(commission)
        self.assertEqual(
            float(commission.rate_percent_snapshot),
            float(AffiliateProgramSettings.get().default_commission_rate_percent),
        )

    def test_per_affiliate_override_rate_applied(self):
        self._login("admin@example.com")
        self.client.post(
            f"/admin/affiliates/{self.affiliate.id}/rate",
            data={"commission_rate_percent": "25"},
        )
        resp = self.client.post(
            f"/admin/affiliates/{self.affiliate.id}/commissions/new",
            data={"amount": "50.00"},
        )
        self.assertEqual(resp.status_code, 302)
        commission = Commission.query.filter_by(affiliate_id=self.affiliate.id).first()
        self.assertEqual(float(commission.rate_percent_snapshot), 25.0)

    def test_status_transition_stamps_timestamps(self):
        self._login("admin@example.com")
        self.client.post(
            f"/admin/affiliates/{self.affiliate.id}/commissions/new", data={"amount": "10"}
        )
        commission = Commission.query.filter_by(affiliate_id=self.affiliate.id).first()
        self.assertIsNone(commission.approved_at)

        self.client.post(
            f"/admin/commissions/{commission.id}/status", data={"status": "approved"}
        )
        db.session.refresh(commission)
        self.assertIsNotNone(commission.approved_at)
        self.assertIsNone(commission.paid_at)

        self.client.post(f"/admin/commissions/{commission.id}/status", data={"status": "paid"})
        db.session.refresh(commission)
        self.assertIsNotNone(commission.paid_at)

    def test_non_admin_forbidden_on_admin_commission_routes(self):
        self._login("aff@example.com")
        resp = self.client.post(
            f"/admin/affiliates/{self.affiliate.id}/commissions/new", data={"amount": "10"}
        )
        self.assertEqual(resp.status_code, 403)

    def test_admin_can_set_default_landing(self):
        self._login("admin@example.com")
        resp = self.client.post(
            "/admin/affiliates/settings",
            data={"default_commission_rate_percent": "10", "default_landing": "academy_home"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(AffiliateProgramSettings.get().default_landing, "academy_home")

    def test_invalid_default_landing_rejected(self):
        self._login("admin@example.com")
        before = AffiliateProgramSettings.get().default_landing
        resp = self.client.post(
            "/admin/affiliates/settings",
            data={"default_commission_rate_percent": "10", "default_landing": "not_a_real_page"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(AffiliateProgramSettings.get().default_landing, before)

    def test_admin_can_toggle_pilot_cohort(self):
        self._login("admin@example.com")
        self.assertFalse(self.affiliate.is_pilot)
        self.client.post(f"/admin/affiliates/{self.affiliate.id}/pilot")
        db.session.refresh(self.affiliate)
        self.assertTrue(self.affiliate.is_pilot)
        self.client.post(f"/admin/affiliates/{self.affiliate.id}/pilot")
        db.session.refresh(self.affiliate)
        self.assertFalse(self.affiliate.is_pilot)


class ClickTrackingTests(_DbTestCase):
    def setUp(self):
        super().setUp()
        self.affiliate = self._make_user(
            "click@example.com", "affiliate", referral_code="CLICK123"
        )

    def test_academy_ref_visit_records_click(self):
        self.client.get("/academy/", query_string={"ref": "CLICK123"})
        clicks = LinkClick.query.filter_by(affiliate_id=self.affiliate.id).all()
        self.assertEqual(len(clicks), 1)
        self.assertEqual(clicks[0].destination, "academy")

    def test_repeat_visit_same_session_not_double_counted(self):
        self.client.get("/academy/", query_string={"ref": "CLICK123"})
        self.client.get("/academy/", query_string={"ref": "CLICK123"})
        self.assertEqual(LinkClick.query.filter_by(affiliate_id=self.affiliate.id).count(), 1)

    def test_marketplace_ref_visit_records_click(self):
        self.client.get("/marketplace/", query_string={"ref": "CLICK123"})
        clicks = LinkClick.query.filter_by(affiliate_id=self.affiliate.id).all()
        self.assertEqual(len(clicks), 1)
        self.assertEqual(clicks[0].destination, "marketplace")

    def test_unknown_ref_records_no_click(self):
        self.client.get("/academy/", query_string={"ref": "NOTREAL9"})
        self.assertEqual(LinkClick.query.count(), 0)


class AffiliateDashboardAuthTests(_DbTestCase):
    def setUp(self):
        super().setUp()
        self.affiliate_a = self._make_user(
            "a@example.com", "affiliate", referral_code="AAAAAAAA"
        )
        self.affiliate_b = self._make_user(
            "b@example.com", "affiliate", referral_code="BBBBBBBB"
        )
        db.session.add(Prospect(
            affiliate_id=self.affiliate_a.id, name="Prospect Alpha",
            email="onlya@example.com", interest_type="buy_channel",
        ))
        db.session.commit()

    def test_dashboard_requires_login(self):
        resp = self.client.get("/affiliate/dashboard")
        self.assertEqual(resp.status_code, 302)

    def test_dashboard_forbidden_for_clipper(self):
        self._make_user("clip@example.com", "clipper")
        self._login("clip@example.com")
        resp = self.client.get("/affiliate/dashboard")
        self.assertEqual(resp.status_code, 403)

    def test_dashboard_shows_only_own_prospects(self):
        self._login("a@example.com")
        resp = self.client.get("/affiliate/dashboard")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Prospect Alpha", resp.data)

        self.client.get("/logout")
        self._login("b@example.com")
        resp = self.client.get("/affiliate/dashboard")
        self.assertNotIn(b"Prospect Alpha", resp.data)


class RoleGateExclusionTests(_DbTestCase):
    """Confirms RoleGateMiddleware's existing allow-lists (unchanged) still
    correctly exclude the new affiliate role — constructed directly against
    a fake wrapped app, the same way test_origin_check.py drives
    OriginCheckMiddleware, to avoid needing the vendored sub-apps loaded.
    """

    def _signed_cookie_header(self, user):
        with self.app.test_request_context():
            login_user(user)
            from flask import session

            resp = Response()
            self.app.session_interface.save_session(self.app, session, resp)
            set_cookie = resp.headers.get("Set-Cookie")
        # "session=<value>; Path=/; ..." -> "session=<value>"
        return set_cookie.split(";", 1)[0]

    def _hit(self, middleware, cookie_header):
        builder = EnvironBuilder(path="/", method="GET", headers={"Cookie": cookie_header})
        environ = builder.get_environ()
        captured = {}

        def start_response(status, headers, exc_info=None):
            captured["status"] = status

        middleware(environ, start_response)
        return captured["status"]

    def test_affiliate_rejected_from_clip_and_produce(self):
        affiliate = self._make_user("aff@example.com", "affiliate", referral_code="ZZZZZZZZ")
        cookie = self._signed_cookie_header(affiliate)

        class FakeApp:
            def __call__(self, environ, start_response):
                resp = Response(b"ok")
                return resp(environ, start_response)

        clip_mw = RoleGateMiddleware(
            FakeApp(), self.app, section="clip", allowed_roles=("clipper",)
        )
        produce_mw = RoleGateMiddleware(
            FakeApp(), self.app, section="produce", allowed_roles=("producer",)
        )
        self.assertTrue(self._hit(clip_mw, cookie).startswith("403"))
        self.assertTrue(self._hit(produce_mw, cookie).startswith("403"))

    def test_clipper_still_allowed_into_clip(self):
        clipper = self._make_user("clip@example.com", "clipper")
        cookie = self._signed_cookie_header(clipper)

        class FakeApp:
            def __call__(self, environ, start_response):
                resp = Response(b"ok")
                return resp(environ, start_response)

        clip_mw = RoleGateMiddleware(
            FakeApp(), self.app, section="clip", allowed_roles=("clipper",)
        )
        self.assertTrue(self._hit(clip_mw, cookie).startswith("200"))


class HomepageRoutingTests(_DbTestCase):
    def test_anonymous_get_home_renders_marketing_page(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"MoneyTuber", resp.data)

    def test_anonymous_get_home_survives_missing_marketplace_table(self):
        # Proves the defensive wrap added after the 2026-08-08 production
        # incident: a broken ChannelListing/MasterClassSettings query must
        # degrade the homepage, never 500 it.
        with patch("app.auth.routes.ChannelListing") as mock_listing:
            mock_listing.query.filter_by.side_effect = SQLAlchemyError("relation does not exist")
            resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"MoneyTuber", resp.data)

    def test_authenticated_redirects_per_role(self):
        cases = {
            "admin": ("adm@example.com", "/admin/"),
            "clipper": ("clp@example.com", "/suggestions/"),
            "producer": ("prd@example.com", "/producer-scout/"),
            "affiliate": ("afl@example.com", "/affiliate/dashboard"),
        }
        for role, (email, expected_location_fragment) in cases.items():
            kwargs = {"referral_code": "R" + role[:7].upper()} if role == "affiliate" else {}
            self._make_user(email, role, **kwargs)
            self._login(email)
            resp = self.client.get("/")
            self.assertEqual(resp.status_code, 302, f"role={role}")
            self.assertIn(expected_location_fragment, resp.headers["Location"], f"role={role}")
            self.client.get("/logout")


def _tiny_png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color=(10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


class AffiliateProfileTests(_DbTestCase):
    """Phase 2: the /a/<code> public profile page — self-serve edit
    (including photo upload/clear, reusing the same save_image/
    LISTING_UPLOAD_DIR pattern as Academy course covers), public rendering,
    click recording, and admin moderation.
    """

    def setUp(self):
        super().setUp()
        self.upload_dir = tempfile.mkdtemp()
        self.app.config["LISTING_UPLOAD_DIR"] = self.upload_dir
        self.affiliate = self._make_user(
            "profile@example.com", "affiliate", referral_code="PROF1234"
        )

    def tearDown(self):
        shutil.rmtree(self.upload_dir, ignore_errors=True)
        super().tearDown()

    def test_edit_requires_login(self):
        resp = self.client.get("/affiliate/profile")
        self.assertEqual(resp.status_code, 302)

    def test_saves_headline_and_bio(self):
        self._login("profile@example.com")
        resp = self.client.post(
            "/affiliate/profile",
            data={"profile_headline": "0 to 1k in 30 days", "profile_bio": "Ask me anything."},
        )
        self.assertEqual(resp.status_code, 302)
        db.session.refresh(self.affiliate)
        self.assertEqual(self.affiliate.profile_headline, "0 to 1k in 30 days")
        self.assertEqual(self.affiliate.profile_bio, "Ask me anything.")

    def test_headline_too_long_rejected(self):
        self._login("profile@example.com")
        resp = self.client.post(
            "/affiliate/profile",
            data={"profile_headline": "x" * 161, "profile_bio": ""},
        )
        self.assertEqual(resp.status_code, 200)
        db.session.refresh(self.affiliate)
        self.assertIsNone(self.affiliate.profile_headline)

    def test_photo_upload_saved_and_served(self):
        self._login("profile@example.com")
        resp = self.client.post(
            "/affiliate/profile",
            data={
                "profile_headline": "", "profile_bio": "",
                "profile_photo": (io.BytesIO(_tiny_png_bytes()), "me.png"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 302)
        db.session.refresh(self.affiliate)
        self.assertIsNotNone(self.affiliate.profile_photo_url)
        stored_name = self.affiliate.profile_photo_url.rstrip("/").split("/")[-1]
        self.assertTrue(stored_name.startswith("affiliate-"))
        self.assertTrue(os.path.isfile(os.path.join(self.upload_dir, stored_name)))

    def test_disguised_non_image_rejected(self):
        self._login("profile@example.com")
        resp = self.client.post(
            "/affiliate/profile",
            data={
                "profile_headline": "", "profile_bio": "",
                "profile_photo": (io.BytesIO(b"not an image"), "fake.png"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 200)
        db.session.refresh(self.affiliate)
        self.assertIsNone(self.affiliate.profile_photo_url)

    def test_clear_photo_deletes_stored_file(self):
        self._login("profile@example.com")
        self.client.post(
            "/affiliate/profile",
            data={
                "profile_headline": "", "profile_bio": "",
                "profile_photo": (io.BytesIO(_tiny_png_bytes()), "me.png"),
            },
            content_type="multipart/form-data",
        )
        db.session.refresh(self.affiliate)
        stored_name = self.affiliate.profile_photo_url.rstrip("/").split("/")[-1]
        stored_path = os.path.join(self.upload_dir, stored_name)
        self.assertTrue(os.path.isfile(stored_path))

        self.client.post(
            "/affiliate/profile",
            data={"profile_headline": "", "profile_bio": "", "clear_photo": "on"},
        )
        db.session.refresh(self.affiliate)
        self.assertIsNone(self.affiliate.profile_photo_url)
        self.assertFalse(os.path.isfile(stored_path))

    def test_public_profile_renders_content(self):
        self.affiliate.profile_headline = "0 to 1k in 30 days"
        self.affiliate.profile_bio = "Real talk about YouTube."
        self.affiliate.display_name = "Ada"
        db.session.commit()
        resp = self.client.get("/a/PROF1234")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"0 to 1k in 30 days", resp.data)
        self.assertIn(b"Real talk about YouTube.", resp.data)

    def test_public_profile_unknown_code_redirects(self):
        resp = self.client.get("/a/NOTREAL9")
        self.assertEqual(resp.status_code, 302)

    def test_public_profile_visit_records_click_every_time(self):
        self.client.get("/a/PROF1234")
        self.client.get("/a/PROF1234")
        clicks = LinkClick.query.filter_by(affiliate_id=self.affiliate.id).all()
        self.assertEqual(len(clicks), 2)
        self.assertTrue(all(c.destination == "profile" for c in clicks))

    def test_admin_can_clear_affiliate_profile(self):
        self.affiliate.profile_headline = "Old headline"
        self.affiliate.profile_bio = "Old bio"
        db.session.commit()
        self._make_user("admin@example.com", "admin")
        self._login("admin@example.com")
        resp = self.client.post(f"/admin/affiliates/{self.affiliate.id}/profile/clear")
        self.assertEqual(resp.status_code, 302)
        db.session.refresh(self.affiliate)
        self.assertIsNone(self.affiliate.profile_headline)
        self.assertIsNone(self.affiliate.profile_bio)


if __name__ == "__main__":
    unittest.main()

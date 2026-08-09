"""Smoke tests for Academy admin + learner entry paths."""

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
    User,
    AcademyCourse,
    AcademyUnit,
    AcademyLesson,
    AcademySettings,
    AcademySubscription,
    AcademyMessage,
)


class _TestConfig(Config):
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False


class AcademyTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_TestConfig)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        admin = User(email="admin@example.com", role="admin", display_name="Admin")
        admin.set_password("password123")
        db.session.add(admin)
        aff = User(email="aff@example.com", role="affiliate", referral_code="AFFCODE1")
        aff.set_password("password123")
        db.session.add(aff)
        db.session.commit()
        AcademySettings.get()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self, email, password="password123"):
        return self.client.post("/login", data={"email": email, "password": password})

    def _seed_course(self):
        admin = User.query.filter_by(email="admin@example.com").first()
        course = AcademyCourse(
            title="Gemini",
            slug="gemini",
            catalog="tool",
            status="published",
            created_by_id=admin.id,
        )
        db.session.add(course)
        db.session.flush()
        unit = AcademyUnit(course_id=course.id, title="UNIT 1 Gemini", sort_order=0)
        db.session.add(unit)
        db.session.flush()
        lesson = AcademyLesson(
            unit_id=unit.id,
            title="Meet Gemini",
            lesson_type="read",
            content="Hello Gemini",
            sort_order=0,
            is_published=True,
        )
        db.session.add(lesson)
        db.session.commit()
        return course, lesson

    def test_relate_admin_signup_messages_only(self):
        course, lesson = self._seed_course()
        resp = self.client.post(
            "/academy/signup",
            data={
                "email": "lead@example.com",
                "password": "password123",
                "display_name": "Lead",
                "whatsapp": "+2348012345678",
                "ai_knowledge": "3",
                "entry_path": "relate_admin",
                "ref_code": "AFFCODE1",
            },
        )
        self.assertEqual(resp.status_code, 302)
        user = User.query.filter_by(email="lead@example.com").first()
        sub = AcademySubscription.query.filter_by(user_id=user.id).first()
        self.assertEqual(sub.status, "relate_only")
        self.assertEqual(sub.affiliate_id, User.query.filter_by(email="aff@example.com").first().id)
        self.assertTrue(AcademyMessage.query.filter_by(learner_id=user.id).count() >= 1)

        resp = self.client.get(f"/academy/lessons/{lesson.id}")
        self.assertEqual(resp.status_code, 302)

        resp = self.client.get("/academy/messages")
        self.assertEqual(resp.status_code, 200)

    @patch("app.paystack.initialize_transaction")
    def test_direct_pay_signup_starts_checkout(self, mock_init):
        mock_init.return_value = {"authorization_url": "https://paystack.test/pay/abc"}
        resp = self.client.post(
            "/academy/signup",
            data={
                "email": "buyer@example.com",
                "password": "password123",
                "display_name": "Buyer",
                "whatsapp": "+2348011111111",
                "ai_knowledge": "2",
                "entry_path": "direct_pay",
                "ref_code": "AFFCODE1",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("paystack.test", resp.headers["Location"])
        buyer = User.query.filter_by(email="buyer@example.com").first()
        sub = AcademySubscription.query.filter_by(user_id=buyer.id).first()
        self.assertEqual(sub.status, "pending")
        self.assertEqual(sub.affiliate_id, User.query.filter_by(email="aff@example.com").first().id)
        self.assertTrue(sub.paystack_reference.startswith("ac-"))

    def test_admin_can_create_course(self):
        self._login("admin@example.com")
        resp = self.client.post(
            "/admin/academy/courses/new",
            data={
                "title": "ChatGPT Deep Dive",
                "catalog": "tool",
                "category": "Writing",
                "summary": "Master ChatGPT",
                "sort_order": 0,
            },
        )
        self.assertEqual(resp.status_code, 302)
        course = AcademyCourse.query.filter_by(slug="chatgpt-deep-dive").first()
        self.assertIsNotNone(course)

    def test_homepage_showcases_academy_when_open(self):
        course, _lesson = self._seed_course()
        course.is_featured = True
        course.summary = "Learn Gemini fast"
        db.session.commit()
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("MoneyTuber Academy", body)
        self.assertIn("id=\"academy\"", body)
        self.assertIn(course.title, body)
        self.assertIn("/academy/signup", body)

    def test_admin_rejects_invalid_category(self):
        self._login("admin@example.com")
        resp = self.client.post(
            "/admin/academy/courses/new",
            data={
                "title": "Bad Cat",
                "catalog": "tool",
                "category": "Not A Real Category",
                "sort_order": 0,
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(AcademyCourse.query.filter_by(title="Bad Cat").first())

    def test_admin_lesson_media_fields(self):
        course, lesson = self._seed_course()
        self._login("admin@example.com")
        resp = self.client.post(
            f"/admin/academy/lessons/{lesson.id}/edit",
            data={
                "title": "Prompt builder",
                "lesson_type": "interactive",
                "content": "<p>Try this <script>alert(1)</script><b>prompt</b></p>",
                "interactive_instruction": "Fill the blanks",
                "interactive_template": "A [subject] in [setting]",
                "interactive_choices": "sunset\nocean",
                "interactive_check_tip": "Clear subject + setting",
                "video_embed_url": "",
                "sort_order": 0,
                "is_published": "on",
            },
        )
        self.assertEqual(resp.status_code, 302)
        lesson = AcademyLesson.query.get(lesson.id)
        self.assertEqual(lesson.lesson_type, "interactive")
        self.assertEqual(lesson.interactive_template, "A [subject] in [setting]")
        self.assertIn("<b>prompt</b>", lesson.content)
        self.assertNotIn("<script>", lesson.content)


if __name__ == "__main__":
    unittest.main()

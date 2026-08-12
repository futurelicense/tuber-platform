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
        from app.academy.taxonomy import seed_default_taxonomy

        seed_default_taxonomy()
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
        self.client.get("/logout")
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

    def test_seed_sample_course(self):
        from app.academy.sample_course import seed_sample_course, SAMPLE_SLUG

        course, created = seed_sample_course()
        self.assertTrue(created)
        self.assertEqual(course.slug, SAMPLE_SLUG)
        self.assertEqual(course.status, "published")
        self.assertEqual(len(course.units), 3)
        lessons = [l for u in course.units for l in u.lessons]
        self.assertEqual(len(lessons), 9)
        types = {l.lesson_type for l in lessons}
        self.assertTrue({"read", "listen", "interactive", "video"}.issubset(types))
        again, created_again = seed_sample_course()
        self.assertFalse(created_again)
        self.assertEqual(again.id, course.id)

    def test_cover_falls_back_when_upload_missing(self):
        """Missing uploads still resolve to the marketplace URL (no silent SVG swap)."""
        from app.academy.covers import resolve_course_cover_url, repair_missing_upload_covers

        course, _lesson = self._seed_course()
        course.cover_image_url = "/marketplace/uploads/academy-missingdeadbeef.webp"
        db.session.commit()
        with self.app.test_request_context("/"):
            url = resolve_course_cover_url(course)
        self.assertIn("marketplace/uploads/academy-missingdeadbeef.webp", url)

        n = repair_missing_upload_covers()
        self.assertEqual(n, 1)
        course = AcademyCourse.query.get(course.id)
        with self.app.test_request_context("/"):
            repaired = resolve_course_cover_url(course)
        self.assertIn("/static/academy/covers/", repaired)

    def test_uploaded_cover_shows_on_homepage_and_courses(self):
        import io
        from PIL import Image

        from app.academy.covers import resolve_course_cover_url

        self._login("admin@example.com")
        img = Image.new("RGB", (40, 24), color=(200, 20, 30))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        resp = self.client.post(
            "/admin/academy/courses/new",
            data={
                "title": "Cover Course",
                "catalog": "tool",
                "category": "Writing",
                "sort_order": 0,
                "is_featured": "on",
                "cover_image": (buf, "cover.png"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 302)
        course = AcademyCourse.query.filter_by(slug="cover-course").first()
        self.assertIsNotNone(course)
        self.assertTrue(course.cover_image_url.startswith("/marketplace/uploads/academy-"))
        course.status = "published"
        db.session.commit()

        with self.app.test_request_context("/"):
            cover = resolve_course_cover_url(course)
        self.assertIn("/marketplace/uploads/", cover)

        self.client.get("/logout")
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn(cover, home.get_data(as_text=True))

        courses_page = self.client.get("/academy/courses")
        self.assertEqual(courses_page.status_code, 200)
        self.assertIn(cover, courses_page.get_data(as_text=True))

    def test_admin_taxonomy_crud(self):
        self._login("admin@example.com")
        resp = self.client.post(
            "/admin/academy/catalogs/new",
            data={"label": "Workshops", "slug": "workshops", "sort_order": 30},
        )
        self.assertEqual(resp.status_code, 302)
        from app.models import AcademyCatalog, AcademyCategory

        catalog = AcademyCatalog.query.filter_by(slug="workshops").first()
        self.assertIsNotNone(catalog)
        resp = self.client.post(
            f"/admin/academy/catalogs/{catalog.id}/categories/new",
            data={"name": "Live cohort", "sort_order": 0},
        )
        self.assertEqual(resp.status_code, 302)
        cat = AcademyCategory.query.filter_by(catalog_id=catalog.id, name="Live cohort").first()
        self.assertIsNotNone(cat)

        resp = self.client.post(
            "/admin/academy/courses/new",
            data={
                "title": "Workshop One",
                "catalog": "workshops",
                "category": "Live cohort",
                "sort_order": 0,
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIsNotNone(AcademyCourse.query.filter_by(slug="workshop-one").first())

    def test_homepage_showcases_academy_when_open(self):
        course, _lesson = self._seed_course()
        course.is_featured = True
        course.summary = "Learn Gemini fast"
        db.session.commit()
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("AI Skills for Content Creation", body)
        self.assertIn('id="skills"', body)
        self.assertIn('id="affiliate"', body)
        self.assertIn("MoneyTuber Academy", body)
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


    def test_live_messaging_poll_and_send(self):
        # Relate path creates a learner with message access
        self.client.post(
            "/academy/signup",
            data={
                "email": "chatter@example.com",
                "password": "password123",
                "display_name": "Chatter",
                "whatsapp": "+2348099999999",
                "ai_knowledge": "4",
                "entry_path": "relate_admin",
            },
        )
        learner = User.query.filter_by(email="chatter@example.com").first()
        self.assertIsNotNone(learner)

        # Learner sends via JSON
        resp = self.client.post(
            "/academy/messages/send",
            json={"body": "Hi admin, need access"},
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertIn("message", payload)
        msg_id = payload["message"]["id"]

        # Admin polls and sees it
        self._login("admin@example.com")
        resp = self.client.get(
            f"/admin/academy/learners/{learner.id}/messages/updates?after_id=0"
        )
        self.assertEqual(resp.status_code, 200)
        msgs = resp.get_json()["messages"]
        self.assertTrue(any(m["id"] == msg_id for m in msgs))

        # Admin replies
        resp = self.client.post(
            f"/admin/academy/learners/{learner.id}/messages/send",
            json={"body": "Granted — welcome!"},
        )
        self.assertEqual(resp.status_code, 200)
        admin_msg_id = resp.get_json()["message"]["id"]

        # Learner polls for admin reply
        self._login("chatter@example.com", "password123")
        resp = self.client.get(f"/academy/messages/updates?after_id={msg_id}")
        self.assertEqual(resp.status_code, 200)
        ids = [m["id"] for m in resp.get_json()["messages"]]
        self.assertIn(admin_msg_id, ids)

    def test_tools_chat_requires_access(self):
        self.client.post(
            "/academy/signup",
            data={
                "email": "lead2@example.com",
                "password": "password123",
                "display_name": "Lead2",
                "whatsapp": "+2348012345679",
                "ai_knowledge": "3",
                "entry_path": "relate_admin",
            },
        )
        resp = self.client.post(
            "/academy/tools/chat",
            json={"message": "hello", "mode": "chat"},
            headers={"X-CSRFToken": "not-needed-when-disabled"},
        )
        # CSRF disabled in tests; relate_only has no course access
        self.assertEqual(resp.status_code, 403)

    def test_tools_chat_uses_groq_helper(self):
        from unittest.mock import patch

        admin = User.query.filter_by(email="admin@example.com").first()
        # Admin has course access
        self._login("admin@example.com")
        with patch.dict("os.environ", {"AI_KEY": "test-key"}, clear=False):
            with patch("app.academy.ai.chat_completion", return_value="Hook idea: …") as mock_chat:
                resp = self.client.post(
                    "/academy/tools/chat",
                    json={"message": "Give me a Shorts hook", "mode": "ideas"},
                )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["reply"], "Hook idea: …")
        mock_chat.assert_called_once()
        # unused but keeps import lint quiet in some setups
        self.assertIsNotNone(admin)


if __name__ == "__main__":
    unittest.main()

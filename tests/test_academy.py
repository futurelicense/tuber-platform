"""Smoke tests for Academy admin + learner flows."""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("CHANNEL_TOKEN_ENC_KEY", "test-channel-token-enc-key")
os.environ.setdefault("PAYSTACK_SECRET_KEY", "test-paystack-secret")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8000")

from app import create_app
from app.config import Config
from app.extensions import db
from app.models import User, AcademyCourse, AcademyUnit, AcademyLesson, AcademySettings


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

    def test_learner_signup_and_complete_lesson(self):
        course, lesson = self._seed_course()
        resp = self.client.post(
            "/academy/signup",
            data={
                "email": "learner@example.com",
                "password": "password123",
                "display_name": "Lea",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(User.query.filter_by(email="learner@example.com").first().role, "learner")

        resp = self.client.get(f"/academy/courses/{course.slug}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Meet Gemini", resp.data)

        resp = self.client.post(
            f"/academy/lessons/{lesson.id}",
            data={"action": "complete"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"completed", resp.data.lower())

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
        self.assertEqual(course.status, "draft")


if __name__ == "__main__":
    unittest.main()

"""Tests for the suggest-agent's data model (dedup, status transitions,
controlled vocab) and the suggestions review-queue's approve/reject race
guard — the two things flagged as needing real test coverage in the plan,
beyond the manual end-to-end verification already done against a real
channel and a real Groq rate limit.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8000")

from sqlalchemy.exc import IntegrityError

from app import create_app
from app.config import Config
from app.extensions import db
from app.models import User, WatchedChannel, DiscoveredVideo, SuggestedClip


class _TestConfig(Config):
    # Config.SQLALCHEMY_DATABASE_URI is computed once at class-body-eval time
    # (module import), not per create_app() call — if some other test module
    # imports app.config first (e.g. test discovery pulling in files
    # alphabetically before this one), it locks in Config's fallback (a real
    # file, sqlite:///<cwd>/dev.db) before our os.environ.setdefault above
    # ever runs, and setting app.config[...] *after* create_app() is too late
    # since db.init_app() already bound the engine to that fallback file —
    # tests would then share and pollute a real on-disk db between runs.
    # Passing this subclass into create_app() up front avoids all of that.
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"


class _DbTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_TestConfig)
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()


class WatchedChannelAndDiscoveredVideoTests(_DbTestCase):
    def setUp(self):
        super().setUp()
        self.admin = User(email="admin", role="admin")
        self.admin.set_password("x")
        db.session.add(self.admin)
        db.session.commit()
        self.channel = WatchedChannel(
            channel_url="https://www.youtube.com/@test", added_by_user_id=self.admin.id
        )
        db.session.add(self.channel)
        db.session.commit()

    def test_duplicate_video_per_channel_rejected(self):
        db.session.add(DiscoveredVideo(
            channel_watch_id=self.channel.id, video_url="https://youtube.com/watch?v=abc"
        ))
        db.session.commit()
        db.session.add(DiscoveredVideo(
            channel_watch_id=self.channel.id, video_url="https://youtube.com/watch?v=abc"
        ))
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_same_video_url_allowed_across_different_channels(self):
        other = WatchedChannel(
            channel_url="https://www.youtube.com/@other", added_by_user_id=self.admin.id
        )
        db.session.add(other)
        db.session.commit()
        db.session.add(DiscoveredVideo(
            channel_watch_id=self.channel.id, video_url="https://youtube.com/watch?v=abc"
        ))
        db.session.add(DiscoveredVideo(
            channel_watch_id=other.id, video_url="https://youtube.com/watch?v=abc"
        ))
        db.session.commit()  # must not raise — uniqueness is per-channel, not global
        self.assertEqual(DiscoveredVideo.query.count(), 2)

    def test_process_status_defaults_to_pending(self):
        dv = DiscoveredVideo(channel_watch_id=self.channel.id, video_url="https://youtube.com/watch?v=x")
        db.session.add(dv)
        db.session.commit()
        self.assertEqual(dv.process_status, "pending")

    def test_invalid_process_status_rejected(self):
        dv = DiscoveredVideo(
            channel_watch_id=self.channel.id,
            video_url="https://youtube.com/watch?v=y",
            process_status="bogus",
        )
        db.session.add(dv)
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_invalid_platform_target_rejected(self):
        wc = WatchedChannel(
            channel_url="https://x.com", added_by_user_id=self.admin.id, platform_target="NotReal"
        )
        db.session.add(wc)
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_suggested_clip_defaults_to_pending(self):
        dv = DiscoveredVideo(channel_watch_id=self.channel.id, video_url="https://youtube.com/watch?v=z")
        db.session.add(dv)
        db.session.commit()
        sc = SuggestedClip(discovered_video_id=dv.id, start=1, end=10)
        db.session.add(sc)
        db.session.commit()
        self.assertEqual(sc.status, "pending")


class SuggestionsApproveRaceGuardTests(_DbTestCase):
    def setUp(self):
        super().setUp()
        self.clipper = User(email="clipper1", role="clipper")
        self.clipper.set_password("x")
        db.session.add(self.clipper)
        admin = User(email="admin", role="admin")
        admin.set_password("x")
        db.session.add(admin)
        db.session.commit()

        wc = WatchedChannel(channel_url="https://www.youtube.com/@test", added_by_user_id=admin.id)
        db.session.add(wc)
        db.session.commit()
        dv = DiscoveredVideo(
            channel_watch_id=wc.id, video_url="https://youtube.com/watch?v=abc", video_title="T"
        )
        db.session.add(dv)
        db.session.commit()
        self.clip = SuggestedClip(discovered_video_id=dv.id, start=1, end=10, platform="TikTok")
        db.session.add(self.clip)
        db.session.commit()

        self.client = self.app.test_client()
        self.client.post("/login", data={"email": "clipper1", "password": "x"})

    def test_approve_redirects_into_clipper_with_video_url(self):
        resp = self.client.post(f"/suggestions/{self.clip.id}/approve")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/clip/?url=", resp.headers["Location"])
        self.assertIn("watch%3Fv%3Dabc", resp.headers["Location"])

    def test_double_approve_does_not_change_the_first_reviewer(self):
        self.client.post(f"/suggestions/{self.clip.id}/approve")
        first_reviewer = SuggestedClip.query.get(self.clip.id).reviewed_by_id
        self.assertIsNotNone(first_reviewer)

        resp = self.client.post(f"/suggestions/{self.clip.id}/approve")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/suggestions/")

        clip = SuggestedClip.query.get(self.clip.id)
        self.assertEqual(clip.status, "approved")
        self.assertEqual(clip.reviewed_by_id, first_reviewer)

    def test_reject_marks_rejected_and_is_not_reapprovable(self):
        self.client.post(f"/suggestions/{self.clip.id}/reject")
        self.assertEqual(SuggestedClip.query.get(self.clip.id).status, "rejected")

        resp = self.client.post(f"/suggestions/{self.clip.id}/approve")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/suggestions/")
        self.assertEqual(SuggestedClip.query.get(self.clip.id).status, "rejected")


if __name__ == "__main__":
    unittest.main()

"""Tests for OriginCheckMiddleware — the CSRF defense for the mounted
sub-apps (/clip, /produce), which have no token concept of their own (see
app/mounting/origin_check.py for why). Exercises the Origin/Referer match
logic directly against a fake wrapped app, the same way
test_prefix_rewrite.py exercises PrefixRewriteMiddleware.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Importing app.mounting.* still imports the app package first, which
# evaluates Config's class body — same DATABASE_URL-implies-real-deployment
# guards as test_suggest_agent.py, so these need setting here too.
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("CHANNEL_TOKEN_ENC_KEY", "test-channel-token-enc-key")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8000")

from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Response

from app.mounting.origin_check import OriginCheckMiddleware


class FakeApp:
    def __call__(self, environ, start_response):
        resp = Response(b"ok", mimetype="text/plain")
        return resp(environ, start_response)


def _run(middleware, method="POST", headers=None, path="/download_video"):
    builder = EnvironBuilder(
        path=path, method=method, headers=headers or {}, base_url="https://tuber.example"
    )
    environ = builder.get_environ()
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = dict(headers)

    body = b"".join(middleware(environ, start_response))
    return captured["status"], captured["headers"], body


class TestOriginCheckMiddleware(unittest.TestCase):
    def test_get_passes_through_regardless_of_origin(self):
        mw = OriginCheckMiddleware(FakeApp())
        status, _, body = _run(mw, method="GET", headers={"Origin": "https://evil.example"})
        self.assertTrue(status.startswith("200"))
        self.assertEqual(body, b"ok")

    def test_same_origin_post_passes(self):
        mw = OriginCheckMiddleware(FakeApp())
        status, _, body = _run(mw, method="POST", headers={"Origin": "https://tuber.example"})
        self.assertTrue(status.startswith("200"))
        self.assertEqual(body, b"ok")

    def test_cross_origin_post_rejected(self):
        mw = OriginCheckMiddleware(FakeApp())
        status, _, body = _run(mw, method="POST", headers={"Origin": "https://evil.example"})
        self.assertTrue(status.startswith("403"))
        self.assertNotEqual(body, b"ok")

    def test_missing_origin_falls_back_to_matching_referer(self):
        mw = OriginCheckMiddleware(FakeApp())
        status, _, body = _run(
            mw, method="POST", headers={"Referer": "https://tuber.example/clip/"}
        )
        self.assertTrue(status.startswith("200"))
        self.assertEqual(body, b"ok")

    def test_missing_origin_and_mismatched_referer_rejected(self):
        mw = OriginCheckMiddleware(FakeApp())
        status, _, _ = _run(
            mw, method="POST", headers={"Referer": "https://evil.example/phish"}
        )
        self.assertTrue(status.startswith("403"))

    def test_missing_origin_and_referer_rejected(self):
        mw = OriginCheckMiddleware(FakeApp())
        status, _, _ = _run(mw, method="POST", headers={})
        self.assertTrue(status.startswith("403"))

    def test_put_and_delete_are_also_checked(self):
        mw = OriginCheckMiddleware(FakeApp())
        for method in ("PUT", "PATCH", "DELETE"):
            status, _, _ = _run(mw, method=method, headers={"Origin": "https://evil.example"})
            self.assertTrue(status.startswith("403"), f"{method} should be checked")


if __name__ == "__main__":
    unittest.main()

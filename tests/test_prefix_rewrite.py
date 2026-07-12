"""Fixture-based tests for PrefixRewriteMiddleware — the core risk flagged in
the platform plan: both vendored apps hardcode root-relative URLs in their
HTML/JSON responses, and a missed literal in the rewrite whitelist fails
silently as a broken button rather than a build error. These tests exercise
canned Response bodies representative of what Youtube-Clipper/ytproduction
actually emit.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Response

from app.mounting.prefix_rewrite import PrefixRewriteMiddleware

WHITELIST = ["/info", "/clip", "/progress/", "/download/", "/download_video", "/script/"]


def _run(middleware, path="/", method="GET"):
    builder = EnvironBuilder(path=path, method=method)
    environ = builder.get_environ()
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = headers

    body = b"".join(middleware(environ, start_response))
    return captured["status"], dict(captured["headers"]), body


class FakeHtmlApp:
    """Mimics Youtube-Clipper's inline HTML page: root-relative fetch()/
    EventSource() literals inside a <script> block."""

    BODY = (
        b'<html><body><script>\n'
        b'fetch("/info?url="+u);\n'
        b'fetch("/download_video", {method:"POST"});\n'
        b'const es = new EventSource("/progress/"+jobId);\n'
        b'a.href = "/download/"+jobId;\n'
        b'</script>Some prose that mentions /info in passing should not be touched.</body></html>'
    )

    def __call__(self, environ, start_response):
        resp = Response(self.BODY, mimetype="text/html")
        return resp(environ, start_response)


class FakeJsonApp:
    """Mimics ytproduction's server-emitted relative-path JSON field."""

    def __call__(self, environ, start_response):
        resp = Response(
            b'{"preview": "/download/abc123/section_000.jpg", "status": "ok"}',
            mimetype="application/json",
        )
        return resp(environ, start_response)


class FakeSseApp:
    """SSE progress stream — must pass through completely untouched."""

    def __call__(self, environ, start_response):
        def gen():
            yield b'data: {"phase": "downloading", "pct": 50}\n\n'
            yield b'data: {"phase": "done"}\n\n'

        resp = Response(gen(), mimetype="text/event-stream")
        return resp(environ, start_response)


class FakeBinaryApp:
    def __call__(self, environ, start_response):
        resp = Response(b"\x00\x01\x02binarydata", mimetype="video/mp4")
        return resp(environ, start_response)


class FakeRedirectApp:
    """Redirects to one of its own routes (should be rewritten) — distinct
    from a redirect RoleGateMiddleware would issue to /login (must NOT be
    rewritten, tested separately at the role_gate level, not here)."""

    def __call__(self, environ, start_response):
        resp = Response(status=302)
        resp.headers["Location"] = "/download/abc123"
        return resp(environ, start_response)


class TestPrefixRewriteMiddleware(unittest.TestCase):
    def test_rewrites_html_fetch_and_eventsource_literals(self):
        mw = PrefixRewriteMiddleware(FakeHtmlApp(), "/clip", WHITELIST)
        status, headers, body = _run(mw)
        text = body.decode()

        self.assertIn('fetch("/clip/info?url="', text)
        self.assertIn('fetch("/clip/download_video"', text)
        self.assertIn('new EventSource("/clip/progress/"', text)
        self.assertIn('a.href = "/clip/download/"', text)
        # Content-Length must be recomputed after rewriting.
        self.assertEqual(int(headers["Content-Length"]), len(body))

    def test_does_not_rewrite_prose_mentioning_a_route_name(self):
        mw = PrefixRewriteMiddleware(FakeHtmlApp(), "/clip", WHITELIST)
        _, _, body = _run(mw)
        text = body.decode()
        self.assertIn("prose that mentions /info in passing", text)

    def test_rewrites_json_body_field(self):
        mw = PrefixRewriteMiddleware(FakeJsonApp(), "/produce", ["/download/"])
        _, headers, body = _run(mw)
        self.assertIn(b'"preview": "/produce/download/abc123', body)
        self.assertEqual(int(headers["Content-Length"]), len(body))

    def test_sse_passes_through_untouched(self):
        mw = PrefixRewriteMiddleware(FakeSseApp(), "/clip", WHITELIST)
        _, headers, body = _run(mw)
        self.assertEqual(
            body, b'data: {"phase": "downloading", "pct": 50}\n\ndata: {"phase": "done"}\n\n'
        )
        self.assertTrue(headers["Content-Type"].startswith("text/event-stream"))

    def test_binary_passes_through_untouched(self):
        mw = PrefixRewriteMiddleware(FakeBinaryApp(), "/clip", WHITELIST)
        _, _, body = _run(mw)
        self.assertEqual(body, b"\x00\x01\x02binarydata")

    def test_rewrites_own_redirect_location(self):
        mw = PrefixRewriteMiddleware(FakeRedirectApp(), "/produce", ["/download/"])
        status, headers, _ = _run(mw)
        self.assertTrue(status.startswith("302"))
        self.assertEqual(headers["Location"], "/produce/download/abc123")

    def test_does_not_double_prefix_an_already_prefixed_location(self):
        class AlreadyPrefixed:
            def __call__(self, environ, start_response):
                resp = Response(status=302)
                resp.headers["Location"] = "/clip/download/xyz"
                return resp(environ, start_response)

        mw = PrefixRewriteMiddleware(AlreadyPrefixed(), "/clip", ["/download/"])
        _, headers, _ = _run(mw)
        self.assertEqual(headers["Location"], "/clip/download/xyz")

    def test_does_not_rewrite_unrelated_location_like_login_redirect(self):
        """Guards the bug caught during manual testing: RoleGateMiddleware's
        redirect to /login must never be prefixed by the wrapping
        PrefixRewriteMiddleware, since /login isn't in either sub-app's
        whitelist."""

        class LoginRedirect:
            def __call__(self, environ, start_response):
                resp = Response(status=302)
                resp.headers["Location"] = "/login?next=/clip/foo"
                return resp(environ, start_response)

        mw = PrefixRewriteMiddleware(LoginRedirect(), "/clip", WHITELIST)
        _, headers, _ = _run(mw)
        self.assertEqual(headers["Location"], "/login?next=/clip/foo")


if __name__ == "__main__":
    unittest.main()

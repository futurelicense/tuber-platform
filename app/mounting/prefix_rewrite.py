"""Rewrites root-relative URLs baked into a mounted sub-app's responses.

Both vendored apps (Youtube-Clipper, ytproduction) embed their entire
frontend as an inline HTML string and use root-relative fetch()/
EventSource()/href literals throughout (e.g. fetch("/info?...")), and even
some server-emitted JSON fields (e.g. ytproduction's "preview": "/result/...").
Mounting them under a URL prefix via DispatcherMiddleware correctly routes
*incoming* requests (it strips the prefix before handing off), but does
nothing about these *outgoing* hardcoded paths in response bodies — without
this middleware, every fetch/EventSource/link in the mounted app resolves
against the platform root instead of the sub-app and 404s.

Deliberately narrow in scope:
- Only text/html and application/json bodies are buffered and rewritten
  (these are small — UI pages and small API replies, never video files).
- Only literals from the given whitelist (routes actually owned by that
  sub-app) are rewritten, matched only when they open a JS/JSON string
  literal (preceded by a quote/backtick) — avoids mangling prose text.
- Everything else (video/image downloads, text/event-stream SSE progress)
  passes through as an unmodified streaming generator. SSE payloads in both
  apps are plain status dicts with no embedded URLs, so there's nothing to
  rewrite there, and buffering would defeat live progress streaming.
- Redirect `Location` headers are rewritten using the same whitelist check
  (NOT unconditionally) — a redirect issued by RoleGateMiddleware (e.g. to
  the platform's own /login) must NOT be prefixed, only redirects the
  sub-app itself issues to one of its own routes.
"""
import re


class PrefixRewriteMiddleware:
    def __init__(self, wsgi_app, prefix, route_whitelist):
        self.wsgi_app = wsgi_app
        self.prefix = prefix.rstrip("/")
        # Longest first so e.g. "/download_video" isn't shadowed by "/download".
        self._literals = sorted(route_whitelist, key=len, reverse=True)
        pattern = "|".join(re.escape(lit) for lit in self._literals)
        self._body_re = re.compile(r'([\'"`])(' + pattern + r")")

    def _rewrite_bytes(self, data: bytes) -> bytes:
        text = data.decode("utf-8", errors="replace")
        text = self._body_re.sub(lambda m: m.group(1) + self.prefix + m.group(2), text)
        return text.encode("utf-8")

    def _rewrite_location(self, location: str) -> str:
        for lit in self._literals:
            if location.startswith(lit):
                return self.prefix + location
        return location

    def __call__(self, environ, start_response):
        captured = {}

        def capture_start_response(status, headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = headers
            captured["exc_info"] = exc_info
            return lambda data: None

        app_iter = self.wsgi_app(environ, capture_start_response)
        headers = captured["headers"]
        header_dict = {k.lower(): v for k, v in headers}
        content_type = header_dict.get("content-type", "")
        should_rewrite = content_type.startswith("text/html") or content_type.startswith(
            "application/json"
        )

        if not should_rewrite:
            new_headers = [
                (k, self._rewrite_location(v) if k.lower() == "location" else v)
                for k, v in headers
            ]
            start_response(captured["status"], new_headers, captured["exc_info"])
            return app_iter

        body = b"".join(app_iter)
        if hasattr(app_iter, "close"):
            app_iter.close()
        body = self._rewrite_bytes(body)

        new_headers = []
        seen_length = False
        for k, v in headers:
            lk = k.lower()
            if lk == "content-length":
                v = str(len(body))
                seen_length = True
            elif lk == "location":
                v = self._rewrite_location(v)
            new_headers.append((k, v))
        if not seen_length:
            new_headers.append(("Content-Length", str(len(body))))

        start_response(captured["status"], new_headers, captured["exc_info"])
        return [body]

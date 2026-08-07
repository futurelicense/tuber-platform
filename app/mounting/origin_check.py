"""CSRF defense for the mounted sub-apps via Origin/Referer checking.

Youtube-Clipper and ytproduction are separate Flask apps reached through
DispatcherMiddleware — Flask-WTF's CSRFProtect (app/extensions.py) is bound
to the platform Flask instance and never sees their requests, and neither
vendored app's fetch()-based frontend sends a CSRF token. `SESSION_COOKIE_
SAMESITE = "Lax"` already stops the classic cross-site <form> POST (Lax
cookies aren't sent on cross-site POST navigations), but that's a cookie
attribute, not an application-level check — this middleware adds the
actual CSRF defense: for any state-changing request, the browser-supplied
Origin (falling back to Referer) must match this app's own origin.

Deliberately does NOT require a token from the vendored apps' HTML/JS,
which would mean patching their baked-in frontend strings (against the
"snapshot, not fork" philosophy — see README). Same-origin fetch()/form
POSTs already carry a same-origin Origin header in every browser this app
needs to support; only a cross-origin forgery attempt fails the check.
"""
from urllib.parse import urlsplit

from werkzeug.wrappers import Response

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class OriginCheckMiddleware:
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def _expected_origin(self, environ):
        # ProxyFix (outermost wrapper, dispatcher.build_wsgi_app) has already
        # corrected wsgi.url_scheme/HTTP_HOST from Render's proxy headers by
        # the time a request reaches here, so this reflects what the
        # browser's address bar actually shows, not a raw hop-by-hop value.
        scheme = environ.get("wsgi.url_scheme", "http")
        host = environ.get("HTTP_HOST", "")
        return f"{scheme}://{host}"

    def _origin_of(self, url):
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            return None
        return f"{parts.scheme}://{parts.netloc}"

    def _is_same_origin(self, environ):
        expected = self._expected_origin(environ)

        origin = environ.get("HTTP_ORIGIN")
        if origin is not None:
            return origin == expected

        # Origin can be legitimately absent on some older browsers/proxies —
        # Referer is the fallback CSRF check (same approach Django's
        # CSRF middleware uses for HTTPS requests).
        referer = environ.get("HTTP_REFERER")
        if referer:
            return self._origin_of(referer) == expected

        # Neither header present: can't confirm same-origin, so refuse.
        # A same-origin fetch()/form POST always carries at least one.
        return False

    def __call__(self, environ, start_response):
        if environ.get("REQUEST_METHOD", "GET").upper() in UNSAFE_METHODS:
            if not self._is_same_origin(environ):
                resp = Response(
                    "Forbidden — request origin didn't match this site.",
                    status=403,
                    mimetype="text/plain",
                )
                return resp(environ, start_response)

        return self.wsgi_app(environ, start_response)

"""WSGI middleware enforcing role-based access to a mounted sub-app.

Youtube-Clipper and ytproduction have zero session/auth concept of their
own, so this is the only place access control for /clip/* and /produce/*
can be enforced. It has to be true WSGI middleware — not a Flask
before_request hook on the platform app — because a request routed by
DispatcherMiddleware to a mounted sub-app never reaches the platform Flask
app instance at all.

Session identity is decoded via the platform Flask app's own
session_interface (reusing whatever cookie-signing Flask/Flask-Login already
do with the shared SECRET_KEY), rather than hand-rolling itsdangerous
unsigning — keeps this in sync automatically if Flask's session format ever
changes.
"""
from werkzeug.wrappers import Request, Response


class RoleGateMiddleware:
    def __init__(self, wsgi_app, platform_app, section, allowed_roles):
        self.wsgi_app = wsgi_app
        self.platform_app = platform_app
        self.section = section  # "clip" or "produce" — used in ActivityLog.action
        self.allowed_roles = set(allowed_roles) | {"admin"}

    def _current_user(self, environ):
        from ..models import User

        req = Request(environ)
        with self.platform_app.app_context():
            try:
                data = self.platform_app.session_interface.open_session(self.platform_app, req)
            except Exception:
                return None
            if not data or "_user_id" not in data:
                return None
            return User.query.get(int(data["_user_id"]))

    def __call__(self, environ, start_response):
        from ..extensions import db
        from ..models import ActivityLog

        user = self._current_user(environ)

        if user is None or not user.is_active:
            # DispatcherMiddleware strips the mount prefix into SCRIPT_NAME and
            # leaves PATH_INFO relative to it — reassemble the real external
            # path so the post-login redirect lands back on /clip/... or
            # /produce/..., not the platform root.
            script_name = environ.get("SCRIPT_NAME", "")
            path = script_name + environ.get("PATH_INFO", "/")
            qs = environ.get("QUERY_STRING", "")
            next_url = path + (("?" + qs) if qs else "")
            resp = Response(status=302)
            resp.headers["Location"] = f"/login?next={next_url}"
            return resp(environ, start_response)

        if user.role not in self.allowed_roles:
            resp = Response(
                "Forbidden — your account doesn't have access to this section.",
                status=403,
                mimetype="text/plain",
            )
            return resp(environ, start_response)

        ip = environ.get("HTTP_X_FORWARDED_FOR", environ.get("REMOTE_ADDR", ""))
        ip = ip.split(",")[0].strip()

        log_id = None
        with self.platform_app.app_context():
            log = ActivityLog(
                user_id=user.id,
                action=f"{self.section}.route_access",
                route=environ.get("PATH_INFO", ""),
                method=environ.get("REQUEST_METHOD", ""),
                ip_address=ip,
            )
            db.session.add(log)
            db.session.commit()
            log_id = log.id

        status_holder = {}

        def logging_start_response(status, headers, exc_info=None):
            status_holder["code"] = int(status.split(" ", 1)[0])
            return start_response(status, headers, exc_info)

        result = self.wsgi_app(environ, logging_start_response)

        if log_id is not None and "code" in status_holder:
            with self.platform_app.app_context():
                from ..models import ActivityLog as _ActivityLog

                row = _ActivityLog.query.get(log_id)
                if row:
                    row.status_code = status_holder["code"]
                    db.session.commit()

        return result

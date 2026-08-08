import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from flask import render_template, request, redirect, url_for, flash, make_response, current_app
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy.exc import SQLAlchemyError

from . import bp
from ..extensions import db
from ..models import User, LoginEvent, ChannelListing, MasterClassSettings
from ..geo import lookup_ip
import logging

logger = logging.getLogger(__name__)

# In-process failed-login throttle, keyed per client IP and per attempted
# email. Process-local state is sufficient here: production runs a single
# gunicorn worker (Dockerfile pins --workers 1 for job-state reasons), so
# every request sees the same dicts.
_FAILURE_WINDOW = 900  # seconds
_MAX_FAILURES = 10  # per IP / per email within the window


_failed_logins = defaultdict(deque)  # throttle key -> recent failure monotonic timestamps


def _throttled(keys):
    now = time.monotonic()
    for key in keys:
        attempts = _failed_logins[key]
        while attempts and now - attempts[0] > _FAILURE_WINDOW:
            attempts.popleft()
        if len(attempts) >= _MAX_FAILURES:
            return True
    return False


def _note_failure(keys):
    now = time.monotonic()
    for key in keys:
        _failed_logins[key].append(now)


def _client_ip():
    # ProxyFix (see dispatcher.build_wsgi_app) has already replaced
    # remote_addr with the client IP taken from Render's own X-Forwarded-For
    # entry. Parsing the header here instead would trust its *first* element,
    # which the client controls by sending its own X-Forwarded-For — letting
    # a tuber forge the IP/location the admin dashboard shows for them.
    return request.remote_addr or ""


def _record_login_event(email, user, success):
    ip = _client_ip()
    geo = lookup_ip(ip) if success else {}
    event = LoginEvent(
        user_id=user.id if user else None,
        attempted_email=email or None,
        ip_address=ip or "unknown",
        geo_country=geo.get("country"),
        geo_region=geo.get("regionName"),
        geo_city=geo.get("city"),
        geo_lat=geo.get("lat"),
        geo_lon=geo.get("lon"),
        geo_raw=geo or None,
        user_agent=request.headers.get("User-Agent", "")[:500],
        success=success,
    )
    db.session.add(event)
    if success and user:
        user.last_login_at = datetime.now(timezone.utc)
    db.session.commit()


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("admin.dashboard" if current_user.role == "admin" else "auth.home"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        throttle_keys = (f"ip:{_client_ip()}", f"email:{email}")

        if _throttled(throttle_keys):
            flash("Too many failed attempts — try again in a few minutes.", "error")
            return render_template("login.html")

        user = User.query.filter_by(email=email).first()

        if user and user.is_active and user.check_password(password):
            login_user(user)
            _record_login_event(email, user, success=True)
            next_url = request.args.get("next")
            # Same-site paths only. "//evil.com" (and "/\evil.com", which
            # browsers normalize to it) starts with "/" but is treated as a
            # protocol-relative URL — an open redirect on the login page.
            if next_url and next_url.startswith("/") and not next_url.startswith(("//", "/\\")):
                return redirect(next_url)
            return redirect(url_for("admin.dashboard" if user.role == "admin" else "auth.home"))

        # Recorded whether or not the email matched a real account — a
        # credential-stuffing run against guessed emails should be visible in
        # the admin's logins dashboard, not silently dropped.
        _note_failure(throttle_keys)
        _record_login_event(email, user, success=False)
        flash("Invalid email or password.", "error")

    # no-store: a cached copy of this page carries a CSRF token from a stale
    # session — submitting it fails even though the user did nothing wrong.
    resp = make_response(render_template("login.html"))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


@bp.route("/")
def home():
    """Public marketing homepage for anonymous visitors; sends everyone
    already signed in straight to their tool."""
    if not current_user.is_authenticated:
        # Defensive: a missing/behind-on-migrations marketplace or
        # master_class table must never 500 the whole homepage (this
        # happened in production on 2026-08-08 — a migration lagged behind
        # a deploy). Degrade to "feature not yet shown" instead of crashing.
        try:
            marketplace_open = (
                ChannelListing.query.filter_by(
                    status="published", availability="available"
                ).count()
                > 0
            )
        except SQLAlchemyError:
            logger.exception("Homepage: ChannelListing query failed, defaulting closed")
            db.session.rollback()
            marketplace_open = False

        try:
            master_class_open = MasterClassSettings.get().is_open_for_enrollment
        except SQLAlchemyError:
            logger.exception("Homepage: MasterClassSettings query failed, defaulting closed")
            db.session.rollback()
            master_class_open = False

        return render_template(
            "home.html",
            master_class_open=master_class_open,
            marketplace_open=marketplace_open,
            whatsapp_number=current_app.config.get("WHATSAPP_NUMBER", ""),
        )
    if current_user.role == "admin":
        return redirect(url_for("admin.dashboard"))
    if current_user.role == "clipper":
        # Suggestions queue acts as their inbox — it links into /clip/ itself
        # when there's something to act on, and offers a plain link there too.
        return redirect(url_for("suggestions.queue"))
    if current_user.role == "producer":
        # Video-to-sections is their entry — it links into /produce/ after
        # approval, and the nav offers a plain Produce link too.
        return redirect(url_for("producer_scout.new"))
    if current_user.role == "affiliate":
        return redirect(url_for("affiliate.dashboard"))
    return redirect(url_for("auth.login"))

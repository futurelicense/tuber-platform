from datetime import datetime, timezone

from flask import render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user

from . import bp
from ..extensions import db
from ..models import User, LoginEvent
from ..geo import lookup_ip


def _record_login_event(user, success):
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    geo = lookup_ip(ip) if success else {}
    event = LoginEvent(
        user_id=user.id,
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
    if success:
        user.last_login_at = datetime.now(timezone.utc)
    db.session.commit()


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("admin.dashboard" if current_user.role == "admin" else "auth.home"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        user = User.query.filter_by(email=email).first()

        if user and user.is_active and user.check_password(password):
            login_user(user)
            _record_login_event(user, success=True)
            next_url = request.args.get("next")
            if next_url and next_url.startswith("/"):
                return redirect(next_url)
            return redirect(url_for("admin.dashboard" if user.role == "admin" else "auth.home"))

        if user:
            _record_login_event(user, success=False)
        flash("Invalid email or password.", "error")

    return render_template("login.html")


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


@bp.route("/")
@login_required
def home():
    """Landing page for non-admin tubers: sends them straight to their tool."""
    if current_user.role == "admin":
        return redirect(url_for("admin.dashboard"))
    if current_user.role == "clipper":
        return redirect("/clip/")
    if current_user.role == "producer":
        return redirect("/produce/")
    return redirect(url_for("auth.login"))

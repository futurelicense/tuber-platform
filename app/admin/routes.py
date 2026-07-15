import secrets

from flask import render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from . import bp
from ..extensions import db
from ..auth.decorators import roles_required
from ..models import (
    User,
    LoginEvent,
    ActivityLog,
    ConnectedChannel,
    MetricDefinition,
    RewardRule,
    WatchedChannel,
)


@bp.before_request
@login_required
@roles_required("admin")
def _require_admin():
    pass


@bp.route("/")
def dashboard():
    user_count = User.query.count()
    clipper_count = User.query.filter_by(role="clipper").count()
    producer_count = User.query.filter_by(role="producer").count()
    recent_activity = ActivityLog.query.order_by(ActivityLog.created_at.desc()).limit(15).all()
    recent_logins = LoginEvent.query.order_by(LoginEvent.created_at.desc()).limit(15).all()
    return render_template(
        "admin/dashboard.html",
        user_count=user_count,
        clipper_count=clipper_count,
        producer_count=producer_count,
        recent_activity=recent_activity,
        recent_logins=recent_logins,
    )


@bp.route("/users")
def users():
    all_users = User.query.order_by(User.created_at.desc()).all()
    return render_template("admin/users.html", users=all_users)


@bp.route("/users/new", methods=["GET", "POST"])
def new_user():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        role = request.form.get("role")
        display_name = (request.form.get("display_name") or "").strip()

        if role not in ("clipper", "producer", "admin"):
            flash("Invalid role.", "error")
            return render_template("admin/new_user.html")
        if not email:
            flash("Email is required.", "error")
            return render_template("admin/new_user.html")
        if User.query.filter_by(email=email).first():
            flash("A user with that email already exists.", "error")
            return render_template("admin/new_user.html")

        temp_password = secrets.token_urlsafe(9)
        user = User(
            email=email,
            role=role,
            display_name=display_name or None,
            created_by_id=current_user.id,
        )
        user.set_password(temp_password)
        db.session.add(user)
        db.session.commit()

        flash(
            f"Created {email} ({role}). Temporary password: {temp_password} "
            "— share this out of band, it won't be shown again.",
            "success",
        )
        return redirect(url_for("admin.users"))

    return render_template("admin/new_user.html")


@bp.route("/users/<int:user_id>/toggle-active", methods=["POST"])
def toggle_active(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("You can't deactivate your own account.", "error")
        return redirect(url_for("admin.users"))
    user.is_active_flag = not user.is_active_flag
    db.session.commit()
    flash(f"{user.email} is now {'active' if user.is_active_flag else 'inactive'}.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:user_id>")
def user_detail(user_id):
    user = User.query.get_or_404(user_id)
    logins = (
        LoginEvent.query.filter_by(user_id=user.id)
        .order_by(LoginEvent.created_at.desc())
        .limit(50)
        .all()
    )
    activity = (
        ActivityLog.query.filter_by(user_id=user.id)
        .order_by(ActivityLog.created_at.desc())
        .limit(50)
        .all()
    )
    channels = ConnectedChannel.query.filter_by(user_id=user.id).all()
    return render_template(
        "admin/user_detail.html", user=user, logins=logins, activity=activity, channels=channels
    )


@bp.route("/activity")
def activity():
    q = ActivityLog.query
    user_id = request.args.get("user_id", type=int)
    action = request.args.get("action", "").strip()
    if user_id:
        q = q.filter_by(user_id=user_id)
    if action:
        q = q.filter(ActivityLog.action.ilike(f"%{action}%"))
    logs = q.order_by(ActivityLog.created_at.desc()).limit(200).all()
    all_users = User.query.order_by(User.email).all()
    return render_template(
        "admin/activity.html", logs=logs, users=all_users, user_id=user_id, action=action
    )


@bp.route("/logins")
def logins():
    events = LoginEvent.query.order_by(LoginEvent.created_at.desc()).limit(200).all()
    return render_template("admin/logins.html", events=events)


@bp.route("/rewards")
def rewards():
    metrics = MetricDefinition.query.order_by(MetricDefinition.created_at.desc()).all()
    rules = RewardRule.query.order_by(RewardRule.created_at.desc()).all()
    return render_template("admin/rewards.html", metrics=metrics, rules=rules)


@bp.route("/rewards/metrics/new", methods=["POST"])
def new_metric():
    key = (request.form.get("key") or "").strip()
    label = (request.form.get("label") or "").strip()
    data_type = request.form.get("data_type")
    applies_to_role = request.form.get("applies_to_role") or None

    if not key or not label or data_type not in ("integer", "float", "boolean", "duration_seconds"):
        flash("Metric key, label, and a valid data type are required.", "error")
        return redirect(url_for("admin.rewards"))
    if MetricDefinition.query.filter_by(key=key).first():
        flash("A metric with that key already exists.", "error")
        return redirect(url_for("admin.rewards"))

    db.session.add(
        MetricDefinition(key=key, label=label, data_type=data_type, applies_to_role=applies_to_role)
    )
    db.session.commit()
    flash(f"Metric '{label}' created.", "success")
    return redirect(url_for("admin.rewards"))


@bp.route("/rewards/rules/new", methods=["POST"])
def new_rule():
    name = (request.form.get("name") or "").strip()
    metric_id = request.form.get("metric_id", type=int)
    operator = request.form.get("operator")
    threshold_low = request.form.get("threshold_low") or None
    threshold_high = request.form.get("threshold_high") or None
    reward_description = (request.form.get("reward_description") or "").strip()
    applies_to_role = request.form.get("applies_to_role") or None

    if not name or not metric_id or operator not in ("gte", "lte", "eq", "between"):
        flash("Rule name, metric, and a valid operator are required.", "error")
        return redirect(url_for("admin.rewards"))

    db.session.add(
        RewardRule(
            name=name,
            metric_id=metric_id,
            operator=operator,
            threshold_low=threshold_low,
            threshold_high=threshold_high,
            reward_description=reward_description or None,
            applies_to_role=applies_to_role,
        )
    )
    db.session.commit()
    flash(f"Reward rule '{name}' created.", "success")
    return redirect(url_for("admin.rewards"))


@bp.route("/watched-channels")
def watched_channels():
    channels = WatchedChannel.query.order_by(WatchedChannel.created_at.desc()).all()
    return render_template("admin/watched_channels.html", channels=channels)


@bp.route("/watched-channels/new", methods=["POST"])
def new_watched_channel():
    channel_url = (request.form.get("channel_url") or "").strip()
    label = (request.form.get("label") or "").strip()
    platform_target = request.form.get("platform_target") or None

    if not channel_url:
        flash("Channel URL is required.", "error")
        return redirect(url_for("admin.watched_channels"))

    db.session.add(
        WatchedChannel(
            channel_url=channel_url,
            label=label or None,
            platform_target=platform_target,
            added_by_user_id=current_user.id,
        )
    )
    db.session.commit()
    flash(f"Now watching {label or channel_url}.", "success")
    return redirect(url_for("admin.watched_channels"))


@bp.route("/watched-channels/<int:channel_id>/toggle-active", methods=["POST"])
def toggle_watched_channel(channel_id):
    channel = WatchedChannel.query.get_or_404(channel_id)
    channel.is_active = not channel.is_active
    db.session.commit()
    flash(
        f"{channel.label or channel.channel_url} is now "
        f"{'active' if channel.is_active else 'inactive'}.",
        "success",
    )
    return redirect(url_for("admin.watched_channels"))

import secrets
from datetime import datetime, timezone

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
    MetricEvent,
    RewardRule,
    WatchedChannel,
    DiscoveredVideo,
    SuggestedClip,
    Prospect,
    Commission,
    AffiliateProgramSettings,
    effective_commission_rate,
    MasterClassEnrollment,
    MasterClassSettings,
    ChannelListing,
    ChannelOrder,
    ListingAttachment,
)
from ..models.affiliate import PROSPECT_STATUSES, COMMISSION_STATUSES
from ..models.marketplace import LISTING_STATUSES, MONETIZATION_STATUSES, ORDER_STATUSES
from ..uploads import save_image, delete_image, UploadRejected
from ..marketplace.services import release_listing

MAX_ATTACHMENTS_PER_LISTING = 6


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

        if role not in ("clipper", "producer", "admin", "affiliate"):
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
        if role == "affiliate":
            from ..affiliate.codes import generate_referral_code

            user.referral_code = generate_referral_code()
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


@bp.route("/users/<int:user_id>/delete", methods=["POST"])
def delete_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("You can't delete your own account.", "error")
        return redirect(url_for("admin.users"))

    # WatchedChannel.added_by_user_id is NOT NULL — unlike the other FKs
    # below, there's no safe value to fall back to (nulling it or
    # reassigning it to someone else would misattribute who configured the
    # watch). Block instead, same spirit as delete_watched_channel requiring
    # an explicit separate action for its own dependents.
    if WatchedChannel.query.filter_by(added_by_user_id=user.id).first():
        flash(
            f"Can't delete {user.email}: they added one or more watched channels. "
            "Delete or reassign those first.",
            "error",
        )
        return redirect(url_for("admin.users"))

    email = user.email

    # This user's own history/records — deleting the account means deleting
    # what it did, same as delete_watched_channel taking its discovered
    # videos/suggestions down with it.
    LoginEvent.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    ActivityLog.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    ConnectedChannel.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    MetricEvent.query.filter_by(user_id=user.id).delete(synchronize_session=False)

    # Nullable FKs where this user was just the *actor* on someone else's
    # row — clear the attribution rather than deleting rows that aren't
    # this user's own data.
    User.query.filter_by(created_by_id=user.id).update({"created_by_id": None})
    SuggestedClip.query.filter_by(reviewed_by_id=user.id).update({"reviewed_by_id": None})

    db.session.delete(user)
    db.session.commit()
    flash(f"Deleted {email}.", "success")
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


@bp.route("/watched-channels/<int:channel_id>/delete", methods=["POST"])
def delete_watched_channel(channel_id):
    channel = WatchedChannel.query.get_or_404(channel_id)
    label = channel.label or channel.channel_url

    # Cascade manually — no ondelete on the FKs, and Postgres enforces them —
    # a plain delete of the channel would fail if it has any discovered
    # videos. Deactivate is the reversible option; delete is meant to be a
    # real, permanent removal (e.g. a mistaken/typo'd entry), so this takes
    # the suggestion history down with it rather than orphaning rows.
    video_ids = [v.id for v in DiscoveredVideo.query.filter_by(channel_watch_id=channel.id)]
    if video_ids:
        SuggestedClip.query.filter(SuggestedClip.discovered_video_id.in_(video_ids)).delete(
            synchronize_session=False
        )
        DiscoveredVideo.query.filter_by(channel_watch_id=channel.id).delete(
            synchronize_session=False
        )
    db.session.delete(channel)
    db.session.commit()
    flash(f"Deleted {label} and its discovered videos/suggestions.", "success")
    return redirect(url_for("admin.watched_channels"))


def _commission_totals(affiliate_id):
    commissions = Commission.query.filter_by(affiliate_id=affiliate_id).all()
    return {
        status: sum((c.amount for c in commissions if c.status == status), start=0)
        for status in ("pending", "approved", "paid")
    }


@bp.route("/affiliates")
def affiliates():
    affiliate_users = User.query.filter_by(role="affiliate").order_by(User.created_at.desc()).all()
    rows = [
        {"user": u, "prospect_count": Prospect.query.filter_by(affiliate_id=u.id).count(),
         "totals": _commission_totals(u.id)}
        for u in affiliate_users
    ]
    settings = AffiliateProgramSettings.get()
    direct_prospects = (
        Prospect.query.filter_by(affiliate_id=None).order_by(Prospect.created_at.desc()).all()
    )
    return render_template(
        "admin/affiliates.html", rows=rows, settings=settings, direct_prospects=direct_prospects
    )


@bp.route("/affiliates/settings", methods=["POST"])
def update_affiliate_settings():
    rate = request.form.get("default_commission_rate_percent", type=float)
    if rate is None or rate < 0:
        flash("Enter a valid default commission rate.", "error")
        return redirect(url_for("admin.affiliates"))
    settings = AffiliateProgramSettings.get()
    settings.default_commission_rate_percent = rate
    db.session.commit()
    flash(f"Default commission rate set to {rate}%.", "success")
    return redirect(url_for("admin.affiliates"))


@bp.route("/affiliates/<int:user_id>")
def affiliate_detail(user_id):
    affiliate = User.query.filter_by(id=user_id, role="affiliate").first_or_404()
    prospects = (
        Prospect.query.filter_by(affiliate_id=affiliate.id)
        .order_by(Prospect.created_at.desc())
        .all()
    )
    commissions = (
        Commission.query.filter_by(affiliate_id=affiliate.id)
        .order_by(Commission.created_at.desc())
        .all()
    )
    effective_rate = effective_commission_rate(affiliate)
    return render_template(
        "admin/affiliate_detail.html",
        affiliate=affiliate,
        prospects=prospects,
        commissions=commissions,
        effective_rate=effective_rate,
        prospect_statuses=PROSPECT_STATUSES,
        commission_statuses=COMMISSION_STATUSES,
    )


@bp.route("/affiliates/<int:user_id>/rate", methods=["POST"])
def set_affiliate_rate(user_id):
    affiliate = User.query.filter_by(id=user_id, role="affiliate").first_or_404()
    raw = request.form.get("commission_rate_percent")
    affiliate.commission_rate_percent = float(raw) if raw not in (None, "") else None
    db.session.commit()
    flash(f"Commission rate override updated for {affiliate.email}.", "success")
    return redirect(url_for("admin.affiliate_detail", user_id=affiliate.id))


@bp.route("/affiliates/<int:user_id>/commissions/new", methods=["POST"])
def new_commission(user_id):
    affiliate = User.query.filter_by(id=user_id, role="affiliate").first_or_404()
    amount = request.form.get("amount", type=float)
    prospect_id = request.form.get("prospect_id", type=int) or None
    note = (request.form.get("note") or "").strip()

    if not amount or amount <= 0:
        flash("A positive commission amount is required.", "error")
        return redirect(url_for("admin.affiliate_detail", user_id=affiliate.id))

    rate = effective_commission_rate(affiliate)
    db.session.add(
        Commission(
            affiliate_id=affiliate.id,
            prospect_id=prospect_id,
            amount=amount,
            rate_percent_snapshot=rate,
            note=note or None,
            created_by_id=current_user.id,
        )
    )
    db.session.commit()
    flash(f"Commission of ${amount:.2f} created for {affiliate.email}.", "success")
    return redirect(url_for("admin.affiliate_detail", user_id=affiliate.id))


@bp.route("/commissions/<int:commission_id>/status", methods=["POST"])
def update_commission_status(commission_id):
    commission = Commission.query.get_or_404(commission_id)
    new_status = request.form.get("status")
    if new_status not in COMMISSION_STATUSES:
        flash("Invalid commission status.", "error")
        return redirect(url_for("admin.affiliate_detail", user_id=commission.affiliate_id))

    commission.status = new_status
    if new_status == "approved" and commission.approved_at is None:
        commission.approved_at = datetime.now(timezone.utc)
    if new_status == "paid" and commission.paid_at is None:
        commission.paid_at = datetime.now(timezone.utc)
    db.session.commit()
    flash(f"Commission #{commission.id} marked {new_status}.", "success")
    return redirect(url_for("admin.affiliate_detail", user_id=commission.affiliate_id))


def _prospect_redirect(prospect):
    # Unattributed prospects (submitted via the homepage's /interest, no
    # referral code) have no affiliate detail page to return to.
    if prospect.affiliate_id is None:
        return redirect(url_for("admin.affiliates"))
    return redirect(url_for("admin.affiliate_detail", user_id=prospect.affiliate_id))


@bp.route("/prospects/<int:prospect_id>/status", methods=["POST"])
def update_prospect_status(prospect_id):
    prospect = Prospect.query.get_or_404(prospect_id)
    new_status = request.form.get("status")
    if new_status not in PROSPECT_STATUSES:
        flash("Invalid prospect status.", "error")
        return _prospect_redirect(prospect)
    prospect.status = new_status
    db.session.commit()
    flash(f"Prospect '{prospect.name}' marked {new_status}.", "success")
    return _prospect_redirect(prospect)


@bp.route("/master-class")
def master_class():
    settings = MasterClassSettings.get()
    enrollments = MasterClassEnrollment.query.order_by(
        MasterClassEnrollment.created_at.desc()
    ).all()
    return render_template(
        "admin/master_class.html", settings=settings, enrollments=enrollments
    )


@bp.route("/master-class/settings", methods=["POST"])
def update_master_class_settings():
    price = request.form.get("price_amount", type=float)
    currency = (request.form.get("currency") or "").strip().upper()
    is_open = request.form.get("is_open_for_enrollment") == "on"

    if not price or price <= 0 or len(currency) != 3:
        flash("Enter a valid price and 3-letter currency code.", "error")
        return redirect(url_for("admin.master_class"))

    settings = MasterClassSettings.get()
    settings.price_amount = price
    settings.currency = currency
    settings.is_open_for_enrollment = is_open
    db.session.commit()
    flash("Master Class settings updated.", "success")
    return redirect(url_for("admin.master_class"))


@bp.route("/master-class/enrollments/<int:enrollment_id>/refund", methods=["POST"])
def refund_enrollment(enrollment_id):
    enrollment = MasterClassEnrollment.query.get_or_404(enrollment_id)
    if enrollment.status != "paid":
        flash("Only a paid enrollment can be refunded.", "error")
        return redirect(url_for("admin.master_class"))
    enrollment.status = "refunded"
    db.session.commit()
    flash(f"Enrollment #{enrollment.id} marked refunded.", "success")
    return redirect(url_for("admin.master_class"))


@bp.route("/marketplace/listings")
def marketplace_listings():
    listings = ChannelListing.query.order_by(ChannelListing.created_at.desc()).all()
    return render_template("admin/marketplace_listings.html", listings=listings)


def _listing_form_fields():
    return {
        "title": (request.form.get("title") or "").strip(),
        "description": (request.form.get("description") or "").strip(),
        "niche": (request.form.get("niche") or "").strip(),
        "monetization_status": request.form.get("monetization_status"),
        "subscriber_count": request.form.get("subscriber_count", type=int),
        "total_views": request.form.get("total_views", type=int),
        "stats_note": (request.form.get("stats_note") or "").strip(),
        "price": request.form.get("price", type=float),
        "currency": (request.form.get("currency") or "").strip().upper(),
    }


def _save_uploaded_attachments(listing):
    """Shared by new_listing/edit_listing. Silently no-ops if no files were
    posted (the field is optional). Invalid files are skipped with a flash
    explaining why, rather than failing the whole form submit.
    """
    files = [f for f in request.files.getlist("attachments") if f and f.filename]
    if not files:
        return

    existing_count = ListingAttachment.query.filter_by(listing_id=listing.id).count()
    room = MAX_ATTACHMENTS_PER_LISTING - existing_count
    if room <= 0:
        flash(
            f"'{listing.title}' already has the maximum of {MAX_ATTACHMENTS_PER_LISTING} "
            "attachments — remove one before adding more.",
            "error",
        )
        return

    saved = 0
    for f in files[:room]:
        try:
            filename, original_filename, content_type, size_bytes = save_image(
                f, prefix=f"listing{listing.id}"
            )
        except UploadRejected as e:
            flash(str(e), "error")
            continue
        db.session.add(
            ListingAttachment(
                listing_id=listing.id,
                filename=filename,
                original_filename=original_filename,
                content_type=content_type,
                size_bytes=size_bytes,
            )
        )
        saved += 1

    if len(files) > room:
        flash(
            f"Only {room} more attachment(s) could be added "
            f"(max {MAX_ATTACHMENTS_PER_LISTING} per listing).",
            "error",
        )

    if saved:
        db.session.commit()
        flash(f"{saved} attachment(s) uploaded.", "success")


@bp.route("/marketplace/listings/new", methods=["GET", "POST"])
def new_listing():
    if request.method == "POST":
        fields = _listing_form_fields()
        if (
            not fields["title"]
            or not fields["price"]
            or fields["price"] <= 0
            or fields["monetization_status"] not in MONETIZATION_STATUSES
            or len(fields["currency"]) != 3
        ):
            flash("Title, a valid price, currency, and monetization status are required.", "error")
            return render_template("admin/marketplace_listing_form.html", listing=None, max_attachments=MAX_ATTACHMENTS_PER_LISTING)

        listing = ChannelListing(
            title=fields["title"],
            description=fields["description"] or None,
            niche=fields["niche"] or None,
            monetization_status=fields["monetization_status"],
            subscriber_count=fields["subscriber_count"],
            total_views=fields["total_views"],
            stats_note=fields["stats_note"] or None,
            price=fields["price"],
            currency=fields["currency"],
            created_by_id=current_user.id,
        )
        db.session.add(listing)
        db.session.commit()
        _save_uploaded_attachments(listing)
        flash(f"Listing '{listing.title}' created as a draft.", "success")
        return redirect(url_for("admin.edit_listing", listing_id=listing.id))

    return render_template("admin/marketplace_listing_form.html", listing=None, max_attachments=MAX_ATTACHMENTS_PER_LISTING)


@bp.route("/marketplace/listings/<int:listing_id>/edit", methods=["GET", "POST"])
def edit_listing(listing_id):
    listing = ChannelListing.query.get_or_404(listing_id)

    if request.method == "POST":
        fields = _listing_form_fields()
        if (
            not fields["title"]
            or not fields["price"]
            or fields["price"] <= 0
            or fields["monetization_status"] not in MONETIZATION_STATUSES
            or len(fields["currency"]) != 3
        ):
            flash("Title, a valid price, currency, and monetization status are required.", "error")
            return render_template("admin/marketplace_listing_form.html", listing=listing, max_attachments=MAX_ATTACHMENTS_PER_LISTING)

        listing.title = fields["title"]
        listing.description = fields["description"] or None
        listing.niche = fields["niche"] or None
        listing.monetization_status = fields["monetization_status"]
        listing.subscriber_count = fields["subscriber_count"]
        listing.total_views = fields["total_views"]
        listing.stats_note = fields["stats_note"] or None
        listing.price = fields["price"]
        listing.currency = fields["currency"]
        db.session.commit()
        _save_uploaded_attachments(listing)
        flash(f"Listing '{listing.title}' updated.", "success")
        return redirect(url_for("admin.edit_listing", listing_id=listing.id))

    return render_template("admin/marketplace_listing_form.html", listing=listing, max_attachments=MAX_ATTACHMENTS_PER_LISTING)


@bp.route("/marketplace/listings/<int:listing_id>/attachments/<int:attachment_id>/delete", methods=["POST"])
def delete_attachment(listing_id, attachment_id):
    attachment = ListingAttachment.query.filter_by(
        id=attachment_id, listing_id=listing_id
    ).first_or_404()
    delete_image(attachment.filename)
    db.session.delete(attachment)
    db.session.commit()
    flash("Attachment removed.", "success")
    return redirect(url_for("admin.edit_listing", listing_id=listing_id))


@bp.route("/marketplace/listings/<int:listing_id>/publish", methods=["POST"])
def publish_listing(listing_id):
    listing = ChannelListing.query.get_or_404(listing_id)
    listing.status = "published"
    db.session.commit()
    flash(f"'{listing.title}' is now published.", "success")
    return redirect(url_for("admin.marketplace_listings"))


@bp.route("/marketplace/listings/<int:listing_id>/unpublish", methods=["POST"])
def unpublish_listing(listing_id):
    listing = ChannelListing.query.get_or_404(listing_id)
    listing.status = "draft"
    db.session.commit()
    flash(f"'{listing.title}' moved back to draft.", "success")
    return redirect(url_for("admin.marketplace_listings"))


@bp.route("/marketplace/listings/<int:listing_id>/withdraw", methods=["POST"])
def withdraw_listing(listing_id):
    listing = ChannelListing.query.get_or_404(listing_id)

    # A live reservation on a listing being pulled from sale would otherwise
    # leave its buyer's checkout dangling — cancel that order outright
    # rather than letting it silently expire via the reservation TTL.
    if listing.availability == "reserved" and listing.holder_order_id:
        held_order = ChannelOrder.query.get(listing.holder_order_id)
        if held_order and held_order.status == "pending":
            held_order.status = "cancelled"
            note = (held_order.admin_note + "\n") if held_order.admin_note else ""
            held_order.admin_note = note + "AUTO: listing was withdrawn by admin."
        release_listing(listing.id, listing.holder_order_id)

    listing.status = "withdrawn"
    db.session.commit()
    flash(f"'{listing.title}' withdrawn.", "success")
    return redirect(url_for("admin.marketplace_listings"))


@bp.route("/marketplace/orders")
def marketplace_orders():
    orders = ChannelOrder.query.order_by(ChannelOrder.created_at.desc()).all()
    return render_template(
        "admin/marketplace_orders.html", orders=orders, order_statuses=ORDER_STATUSES
    )


@bp.route("/marketplace/orders/<int:order_id>/status", methods=["POST"])
def update_order_status(order_id):
    order = ChannelOrder.query.get_or_404(order_id)
    new_status = request.form.get("status")
    admin_note = (request.form.get("admin_note") or "").strip()

    if new_status not in ORDER_STATUSES:
        flash("Invalid order status.", "error")
        return redirect(url_for("admin.marketplace_orders"))

    order.status = new_status
    if admin_note:
        order.admin_note = admin_note
    if new_status == "completed" and order.completed_at is None:
        order.completed_at = datetime.now(timezone.utc)
    db.session.commit()
    flash(f"Order #{order.id} marked {new_status}.", "success")
    return redirect(url_for("admin.marketplace_orders"))


@bp.route("/marketplace/orders/<int:order_id>/refund", methods=["POST"])
def refund_order(order_id):
    order = ChannelOrder.query.get_or_404(order_id)
    if order.status not in ("paid", "handoff_in_progress", "payment_conflict"):
        flash("Only a paid, in-progress, or conflicted order can be refunded.", "error")
        return redirect(url_for("admin.marketplace_orders"))

    order.status = "refunded"
    db.session.commit()

    listing = ChannelListing.query.get(order.listing_id)
    if listing and listing.holder_order_id == order.id and listing.availability == "sold":
        listing.availability = "available"
        listing.holder_order_id = None
        listing.sold_at = None
        db.session.commit()

    flash(f"Order #{order.id} marked refunded.", "success")
    return redirect(url_for("admin.marketplace_orders"))

from flask import render_template, request, redirect, url_for, flash, session
from flask_login import login_required, login_user, current_user

from . import bp
from .codes import generate_referral_code
from .tracking import record_click
from ..extensions import db
from ..auth.decorators import roles_required
from ..models import User, Prospect, Commission, ChannelListing, AffiliateProgramSettings, LinkClick
from ..models.affiliate import PROSPECT_INTEREST_TYPES
from ..uploads import save_image, delete_image, UploadRejected

PROFILE_HEADLINE_MAX = 160
PROFILE_BIO_MAX = 2000


def _find_active_affiliate(code):
    return User.query.filter_by(
        referral_code=code.upper(), role="affiliate", is_active_flag=True
    ).first()


def _landing_url(destination, code):
    """Where an affiliate's bare /r/<code> link sends a visitor, per the
    admin-configured AffiliateProgramSettings.default_landing. ?ref= is
    appended even though session["ref_code"] is already set by the caller —
    every destination route's own ref-resolution prefers the query string,
    and it keeps the URL self-attributing if ever copied/shared onward.
    """
    if destination == "academy_home":
        return url_for("academy.home", ref=code)
    if destination == "marketplace":
        return url_for("marketplace.browse", ref=code)
    if destination == "contact":
        return url_for("affiliate.contact", code=code)
    return url_for("academy.signup", ref=code)


def _delete_stored_profile_photo(photo_url):
    if not photo_url:
        return
    name = photo_url.rstrip("/").split("/")[-1]
    if name.startswith("affiliate-") and ".." not in name and "/" not in name:
        delete_image(name)


def _resolve_profile_photo_url(existing_url):
    """Same save/serve pattern as AcademyCourse.cover_image_url (see
    admin.routes._resolve_course_cover_url): save_image() writes under
    LISTING_UPLOAD_DIR (shared Render disk, no new infra), served back via
    the marketplace blueprint's generic filename-based uploads route.
    """
    clear = request.form.get("clear_photo") == "on"
    upload = request.files.get("profile_photo")
    if upload and upload.filename:
        try:
            filename, _, _, _ = save_image(upload, prefix="affiliate")
        except UploadRejected as e:
            return existing_url, str(e)
        new_url = url_for("marketplace.uploaded_file", filename=filename)
        if existing_url and existing_url != new_url:
            _delete_stored_profile_photo(existing_url)
        return new_url, None
    if clear:
        _delete_stored_profile_photo(existing_url)
        return None, None
    return existing_url, None


def _create_prospect(affiliate_id, referral_code_used=None):
    """Shared by /r/<code> (affiliate-attributed) and /interest (homepage,
    unattributed). Returns the created Prospect, or None + a flash on
    validation failure — caller re-renders its own form template either way.
    """
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    phone = (request.form.get("phone") or "").strip()
    interest_type = request.form.get("interest_type")
    message = (request.form.get("message") or "").strip()

    if not name or not email or interest_type not in PROSPECT_INTEREST_TYPES:
        flash("Name, email, and what you're interested in are required.", "error")
        return None

    prospect = Prospect(
        affiliate_id=affiliate_id,
        name=name,
        email=email,
        phone=phone or None,
        interest_type=interest_type,
        message=message or None,
        referral_code_used=referral_code_used,
        ip_address=request.remote_addr,
    )
    db.session.add(prospect)
    db.session.commit()
    return prospect


@bp.route("/affiliate/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(
            url_for("affiliate.dashboard")
            if current_user.role == "affiliate"
            else url_for("auth.home")
        )

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        display_name = (request.form.get("display_name") or "").strip()

        if not email or "@" not in email:
            flash("A valid email is required.", "error")
            return render_template("affiliate/signup.html")
        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("affiliate/signup.html")
        if User.query.filter_by(email=email).first():
            flash("An account with that email already exists.", "error")
            return render_template("affiliate/signup.html")

        user = User(
            email=email,
            role="affiliate",
            display_name=display_name or None,
            referral_code=generate_referral_code(),
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        login_user(user)
        flash("Welcome to the MoneyTuber affiliate program!", "success")
        return redirect(url_for("affiliate.dashboard"))

    return render_template("affiliate/signup.html")


@bp.route("/r/<code>")
def capture(code):
    """An affiliate's primary share link. Sends the visitor straight to a
    real purchase-ready landing page (admin-configured, defaults to the
    Academy signup) instead of a generic lead-capture form — the form is
    still available as a deliberate fallback at /r/<code>/contact for
    prospects who aren't ready to buy yet.
    """
    affiliate = _find_active_affiliate(code)
    if affiliate is None:
        flash("That referral link isn't valid.", "error")
        return redirect(url_for("auth.home"))

    # Lets a prospect who clicks a referral link but converts later (e.g. on
    # the Master Class sales page, reached via an external Paystack redirect
    # round-trip) still get attributed to this affiliate.
    session["ref_code"] = code.upper()
    destination = AffiliateProgramSettings.get().default_landing
    record_click(code, destination)
    return redirect(_landing_url(destination, code.upper()))


@bp.route("/r/<code>/contact", methods=["GET", "POST"])
def contact(code):
    """The original generic lead-capture form — no longer the default
    destination, but kept as an explicit fallback for a prospect who wants
    to talk before buying, or when admin sets default_landing="contact".
    """
    affiliate = _find_active_affiliate(code)
    if affiliate is None:
        flash("That referral link isn't valid.", "error")
        return redirect(url_for("auth.home"))

    session["ref_code"] = code.upper()

    if request.method == "POST":
        prospect = _create_prospect(affiliate.id, referral_code_used=code.upper())
        if prospect is None:
            return render_template("affiliate/referral_intake.html", code=code, affiliate=affiliate)
        return render_template("affiliate/referral_thanks.html", affiliate=affiliate)

    return render_template("affiliate/referral_intake.html", code=code, affiliate=affiliate)


@bp.route("/interest", methods=["POST"])
def interest():
    """Homepage-direct lead capture — no referral code, so unattributed."""
    prospect = _create_prospect(affiliate_id=None)
    if prospect is None:
        return redirect(url_for("auth.home"))
    flash("Thanks — we'll be in touch shortly.", "success")
    return redirect(url_for("auth.home"))


@bp.route("/affiliate/dashboard")
@login_required
@roles_required("affiliate")
def dashboard():
    if current_user.role == "admin":
        # roles_required always admits admin, but an admin has no referral
        # code/prospects of their own — send them to the admin overview.
        return redirect(url_for("admin.affiliates"))

    prospects = (
        Prospect.query.filter_by(affiliate_id=current_user.id)
        .order_by(Prospect.created_at.desc())
        .all()
    )
    commissions = (
        Commission.query.filter_by(affiliate_id=current_user.id)
        .order_by(Commission.created_at.desc())
        .all()
    )
    totals = {
        status: sum((c.amount for c in commissions if c.status == status), start=0)
        for status in ("pending", "approved", "paid")
    }
    referral_url = url_for("affiliate.capture", code=current_user.referral_code, _external=True)
    contact_url = url_for("affiliate.contact", code=current_user.referral_code, _external=True)
    click_count = LinkClick.query.filter_by(affiliate_id=current_user.id).count()
    marketplace_browse_url = url_for(
        "marketplace.browse", ref=current_user.referral_code, _external=True
    )
    listings = (
        ChannelListing.query.filter_by(status="published", availability="available")
        .order_by(ChannelListing.created_at.desc())
        .all()
    )
    listing_share_links = [
        {
            "title": listing.title,
            "price": listing.price,
            "currency": listing.currency,
            "url": url_for(
                "marketplace.detail",
                listing_id=listing.id,
                ref=current_user.referral_code,
                _external=True,
            ),
        }
        for listing in listings
    ]
    from ..models import AcademyCourse, AcademySettings

    academy_settings = AcademySettings.get()
    academy_signup_url = url_for(
        "academy.signup", ref=current_user.referral_code, _external=True
    )
    academy_home_url = url_for(
        "academy.home", ref=current_user.referral_code, _external=True
    )
    academy_courses = (
        AcademyCourse.query.filter_by(status="published")
        .order_by(AcademyCourse.sort_order.asc(), AcademyCourse.created_at.desc())
        .all()
    )
    academy_course_share_links = [
        {
            "title": course.title,
            "catalog": course.catalog,
            "url": url_for(
                "academy.course_detail",
                slug=course.slug,
                ref=current_user.referral_code,
                _external=True,
            ),
        }
        for course in academy_courses
    ]
    return render_template(
        "affiliate/dashboard.html",
        prospects=prospects,
        commissions=commissions,
        totals=totals,
        referral_url=referral_url,
        contact_url=contact_url,
        click_count=click_count,
        marketplace_browse_url=marketplace_browse_url,
        listing_share_links=listing_share_links,
        academy_signup_url=academy_signup_url,
        academy_home_url=academy_home_url,
        academy_course_share_links=academy_course_share_links,
        academy_settings=academy_settings,
        profile_url=url_for("affiliate.public_profile", code=current_user.referral_code, _external=True),
    )


@bp.route("/affiliate/profile", methods=["GET", "POST"])
@login_required
@roles_required("affiliate")
def edit_profile():
    if current_user.role == "admin":
        return redirect(url_for("admin.affiliates"))

    if request.method == "POST":
        headline = (request.form.get("profile_headline") or "").strip()
        bio = (request.form.get("profile_bio") or "").strip()
        if len(headline) > PROFILE_HEADLINE_MAX:
            flash(f"Headline must be {PROFILE_HEADLINE_MAX} characters or fewer.", "error")
            return render_template("affiliate/profile_edit.html", affiliate=current_user)
        if len(bio) > PROFILE_BIO_MAX:
            flash(f"Bio must be {PROFILE_BIO_MAX} characters or fewer.", "error")
            return render_template("affiliate/profile_edit.html", affiliate=current_user)

        photo_url, photo_err = _resolve_profile_photo_url(current_user.profile_photo_url)
        if photo_err:
            flash(photo_err, "error")
            return render_template("affiliate/profile_edit.html", affiliate=current_user)

        current_user.profile_headline = headline or None
        current_user.profile_bio = bio or None
        current_user.profile_photo_url = photo_url
        db.session.commit()
        flash("Your public profile is updated.", "success")
        return redirect(url_for("affiliate.dashboard"))

    return render_template("affiliate/profile_edit.html", affiliate=current_user)


@bp.route("/a/<code>")
def public_profile(code):
    """A "linktree-style" page an affiliate can share instead of (or
    alongside) the bare /r/<code> link — a name/photo/pitch reads as more
    trustworthy than a raw referral URL at real-audience scale. Every visit
    is a genuine click on this affiliate (this page IS the entry point, not
    a revisitable destination), so it's recorded unconditionally, same as
    /r/<code>'s capture() — no session dedup needed here.
    """
    affiliate = _find_active_affiliate(code)
    if affiliate is None:
        flash("That affiliate page isn't available.", "error")
        return redirect(url_for("auth.home"))

    session["ref_code"] = code.upper()
    record_click(code, "profile")

    has_listings = (
        ChannelListing.query.filter_by(status="published", availability="available").count() > 0
    )
    return render_template(
        "affiliate/public_profile.html",
        affiliate=affiliate,
        academy_url=url_for("academy.signup", ref=code.upper()),
        marketplace_url=url_for("marketplace.browse", ref=code.upper()),
        contact_url=url_for("affiliate.contact", code=code.upper()),
        has_listings=has_listings,
    )

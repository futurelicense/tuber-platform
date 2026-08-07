from flask import render_template, request, redirect, url_for, flash, session
from flask_login import login_required, login_user, current_user

from . import bp
from .codes import generate_referral_code
from ..extensions import db
from ..auth.decorators import roles_required
from ..models import User, Prospect, Commission
from ..models.affiliate import PROSPECT_INTEREST_TYPES


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


@bp.route("/r/<code>", methods=["GET", "POST"])
def capture(code):
    affiliate = User.query.filter_by(
        referral_code=code.upper(), role="affiliate", is_active_flag=True
    ).first()
    if affiliate is None:
        flash("That referral link isn't valid.", "error")
        return redirect(url_for("auth.home"))

    # Lets a prospect who clicks a referral link but converts later (e.g. on
    # the Master Class sales page, reached via an external Paystack redirect
    # round-trip) still get attributed to this affiliate.
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
    return render_template(
        "affiliate/dashboard.html",
        prospects=prospects,
        commissions=commissions,
        totals=totals,
        referral_url=referral_url,
    )

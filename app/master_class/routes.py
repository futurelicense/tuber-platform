import logging
import secrets

from flask import render_template, request, redirect, url_for, flash, session

from . import bp
from .services import mark_enrollment_paid
from .. import paystack
from ..extensions import db
from ..models import User, Prospect, MasterClassEnrollment, MasterClassSettings

logger = logging.getLogger(__name__)


def _expected_kobo(enrollment):
    return int(round(enrollment.amount * 100))


def _find_affiliate(code):
    if not code:
        return None
    return User.query.filter_by(
        referral_code=code.upper(), role="affiliate", is_active_flag=True
    ).first()


def _find_prospect(email):
    return (
        Prospect.query.filter_by(email=email, interest_type="master_class")
        .order_by(Prospect.created_at.desc())
        .first()
    ) or (
        Prospect.query.filter_by(email=email).order_by(Prospect.created_at.desc()).first()
    )


@bp.route("/")
def sales():
    settings = MasterClassSettings.get()
    ref_code = request.args.get("ref") or session.get("ref_code")
    return render_template("master_class/sales.html", settings=settings, ref_code=ref_code)


@bp.route("/enroll", methods=["POST"])
def enroll():
    settings = MasterClassSettings.get()
    if not settings.is_open_for_enrollment:
        flash("Enrollment isn't open yet.", "error")
        return redirect(url_for("master_class.sales"))

    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    phone = (request.form.get("phone") or "").strip()
    ref_code = (request.form.get("ref_code") or "").strip()

    if not name or not email:
        flash("Name and email are required.", "error")
        return redirect(url_for("master_class.sales"))

    affiliate = _find_affiliate(ref_code)
    prospect = _find_prospect(email)

    enrollment = MasterClassEnrollment(
        buyer_name=name,
        buyer_email=email,
        buyer_phone=phone or None,
        prospect_id=prospect.id if prospect else None,
        affiliate_id=affiliate.id if affiliate else None,
        referral_code_used=ref_code.upper() if affiliate else None,
        amount=settings.price_amount,
        currency=settings.currency,
        paystack_reference="pending",  # placeholder until we have an id
        ip_address=request.remote_addr,
    )
    db.session.add(enrollment)
    db.session.flush()  # assigns enrollment.id without committing yet
    enrollment.paystack_reference = f"mc-{enrollment.id}-{secrets.token_hex(6)}"
    db.session.commit()

    try:
        data = paystack.initialize_transaction(
            email=email,
            amount_kobo=_expected_kobo(enrollment),
            reference=enrollment.paystack_reference,
            callback_url=url_for("master_class.callback", _external=True),
            metadata={
                "enrollment_id": enrollment.id,
                "affiliate_id": enrollment.affiliate_id,
                "prospect_id": enrollment.prospect_id,
            },
        )
    except paystack.PaystackError as e:
        enrollment.status = "failed"
        db.session.commit()
        logger.warning("Paystack initialize failed for enrollment %s: %s", enrollment.id, e)
        flash("We couldn't start checkout — please try again in a moment.", "error")
        return redirect(url_for("master_class.sales"))

    return redirect(data["authorization_url"])


@bp.route("/callback")
def callback():
    reference = request.args.get("reference") or request.args.get("trxref")
    if not reference:
        flash("Missing payment reference.", "error")
        return redirect(url_for("master_class.sales"))

    enrollment = MasterClassEnrollment.query.filter_by(paystack_reference=reference).first_or_404()

    if enrollment.status == "paid":
        # The webhook — server-to-server, usually faster than the browser
        # round-trip — already processed this. Nothing left to do.
        return render_template("master_class/success.html", enrollment=enrollment)

    try:
        data = paystack.verify_transaction(reference)
    except paystack.PaystackError as e:
        logger.warning("Paystack verify failed for %s: %s", reference, e)
        return render_template("master_class/pending.html", enrollment=enrollment)

    if data.get("status") == "success" and int(data.get("amount", -1)) == _expected_kobo(enrollment):
        mark_enrollment_paid(enrollment)
        return render_template("master_class/success.html", enrollment=enrollment)

    # Not confirmed yet — may still complete and arrive via webhook shortly.
    return render_template("master_class/pending.html", enrollment=enrollment)


# Paystack's server-to-server webhook is handled at a single consolidated
# endpoint, not here — see app/webhooks/routes.py's module docstring for
# why (Paystack's dashboard only supports one webhook URL per account).

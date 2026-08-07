import logging
import secrets

from flask import render_template, request, redirect, url_for, flash, session

from . import bp
from .services import mark_order_paid, reserve_listing, release_listing, release_stale_reservations
from .. import paystack
from ..extensions import db
from ..models import User, Prospect, ChannelListing, ChannelOrder

logger = logging.getLogger(__name__)


def _expected_kobo(order):
    return int(round(order.amount * 100))


def _find_affiliate(code):
    if not code:
        return None
    return User.query.filter_by(
        referral_code=code.upper(), role="affiliate", is_active_flag=True
    ).first()


def _find_prospect(email):
    return (
        Prospect.query.filter_by(email=email, interest_type="buy_channel")
        .order_by(Prospect.created_at.desc())
        .first()
    ) or (
        Prospect.query.filter_by(email=email).order_by(Prospect.created_at.desc()).first()
    )


@bp.route("/")
def browse():
    release_stale_reservations()
    listings = ChannelListing.query.filter_by(status="published", availability="available").order_by(
        ChannelListing.created_at.desc()
    ).all()
    return render_template("marketplace/browse.html", listings=listings)


@bp.route("/<int:listing_id>")
def detail(listing_id):
    listing = ChannelListing.query.filter_by(id=listing_id, status="published").first_or_404()
    release_stale_reservations()
    db.session.refresh(listing)
    ref_code = request.args.get("ref") or session.get("ref_code")
    return render_template("marketplace/detail.html", listing=listing, ref_code=ref_code)


@bp.route("/<int:listing_id>/buy", methods=["POST"])
def buy(listing_id):
    listing = ChannelListing.query.filter_by(id=listing_id, status="published").first_or_404()

    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    phone = (request.form.get("phone") or "").strip()
    ref_code = (request.form.get("ref_code") or "").strip()

    if not name or not email:
        flash("Name and email are required.", "error")
        return redirect(url_for("marketplace.detail", listing_id=listing.id))

    affiliate = _find_affiliate(ref_code)
    prospect = _find_prospect(email)

    order = ChannelOrder(
        listing_id=listing.id,
        buyer_name=name,
        buyer_email=email,
        buyer_phone=phone or None,
        prospect_id=prospect.id if prospect else None,
        affiliate_id=affiliate.id if affiliate else None,
        referral_code_used=ref_code.upper() if affiliate else None,
        amount=listing.price,
        currency=listing.currency,
        paystack_reference="pending",  # placeholder until we have an id
        ip_address=request.remote_addr,
    )
    db.session.add(order)
    db.session.flush()  # assigns order.id without committing yet
    order.paystack_reference = f"ch-{order.id}-{secrets.token_hex(6)}"

    if not reserve_listing(listing.id, order.id):
        db.session.rollback()  # the flushed order never persists — the race was lost
        flash("Sorry — this listing was just taken by another buyer.", "error")
        return redirect(url_for("marketplace.detail", listing_id=listing.id))

    db.session.commit()  # order + reservation committed together

    try:
        data = paystack.initialize_transaction(
            email=email,
            amount_kobo=_expected_kobo(order),
            reference=order.paystack_reference,
            callback_url=url_for("marketplace.callback", _external=True),
            metadata={
                "order_id": order.id,
                "listing_id": listing.id,
                "affiliate_id": order.affiliate_id,
            },
        )
    except paystack.PaystackError as e:
        order.status = "failed"
        db.session.commit()
        release_listing(listing.id, order.id)
        logger.warning("Paystack initialize failed for order %s: %s", order.id, e)
        flash("We couldn't start checkout — please try again in a moment.", "error")
        return redirect(url_for("marketplace.detail", listing_id=listing.id))

    return redirect(data["authorization_url"])


@bp.route("/callback")
def callback():
    reference = request.args.get("reference") or request.args.get("trxref")
    if not reference:
        flash("Missing payment reference.", "error")
        return redirect(url_for("marketplace.browse"))

    order = ChannelOrder.query.filter_by(paystack_reference=reference).first_or_404()

    if order.status == "paid":
        # The webhook — server-to-server, usually faster than the browser
        # round-trip — already processed this. Nothing left to do.
        return render_template("marketplace/success.html", order=order)

    try:
        data = paystack.verify_transaction(reference)
    except paystack.PaystackError as e:
        logger.warning("Paystack verify failed for %s: %s", reference, e)
        return render_template("marketplace/pending.html", order=order)

    if data.get("status") == "success" and int(data.get("amount", -1)) == _expected_kobo(order):
        mark_order_paid(order)
        db.session.refresh(order)
        if order.status == "paid":
            return render_template("marketplace/success.html", order=order)
        # payment_conflict — payment succeeded but the listing was already
        # sold to another order in the meantime.
        return render_template("marketplace/conflict.html", order=order)

    # Not confirmed yet — may still complete and arrive via webhook shortly.
    return render_template("marketplace/pending.html", order=order)


# Paystack's server-to-server webhook is handled at a single consolidated
# endpoint, not here — see app/webhooks/routes.py's module docstring for
# why (Paystack's dashboard only supports one webhook URL per account).

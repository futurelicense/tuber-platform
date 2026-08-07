from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, update
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import Commission, effective_commission_rate
from ..models.marketplace import ChannelListing

# No cron/job runner exists in this codebase to expire an abandoned
# checkout, so a stale reservation self-heals lazily: the reserve-time
# WHERE clause treats it as claimable again, and browse() sweeps on every
# hit (a stale-reserved listing is otherwise invisible there and would
# never get re-visited to trigger that self-heal).
RESERVATION_TTL_MINUTES = 30


def _stale_cutoff():
    return datetime.now(timezone.utc) - timedelta(minutes=RESERVATION_TTL_MINUTES)


def reserve_listing(listing_id, order_id):
    """Atomic UPDATE...WHERE claim, same pattern as suggestions/routes.py's
    _claim() — a plain read-then-write has a TOCTOU race under real thread
    concurrency (Dockerfile: --threads 24). Returns True if this order won
    the reservation, False if someone else already holds it (and it isn't
    stale enough to reclaim).

    Deliberately does NOT commit — the caller decides the transaction
    boundary. A win should commit together with the just-created order row;
    a loss should roll the whole thing back so a lost-race order never
    persists at all (see marketplace/routes.py:buy()).
    """
    result = db.session.execute(
        update(ChannelListing)
        .where(
            ChannelListing.id == listing_id,
            or_(
                ChannelListing.availability == "available",
                and_(
                    ChannelListing.availability == "reserved",
                    ChannelListing.reserved_at < _stale_cutoff(),
                ),
            ),
        )
        .values(
            availability="reserved",
            reserved_at=datetime.now(timezone.utc),
            holder_order_id=order_id,
        )
        # SQLite returns naive datetimes on read, so the default
        # 'evaluate' sync strategy's in-Python WHERE re-check (against any
        # already-loaded ChannelListing in the identity map) crashes
        # comparing them to _stale_cutoff()'s tz-aware value. We always
        # explicitly refresh() the listing where its fresh state matters,
        # so skipping that in-session re-sync is safe.
        .execution_options(synchronize_session=False)
    )
    return result.rowcount > 0


def release_listing(listing_id, order_id):
    """Frees a reservation immediately (e.g. Paystack initialize failed) —
    scoped to this order's own hold, so it can't clobber someone else's
    reservation that won it in the meantime.
    """
    db.session.execute(
        update(ChannelListing)
        .where(ChannelListing.id == listing_id, ChannelListing.holder_order_id == order_id)
        .values(availability="available", reserved_at=None, holder_order_id=None)
        .execution_options(synchronize_session=False)
    )
    db.session.commit()


def release_stale_reservations():
    db.session.execute(
        update(ChannelListing)
        .where(
            ChannelListing.availability == "reserved",
            ChannelListing.reserved_at < _stale_cutoff(),
        )
        .values(availability="available", reserved_at=None, holder_order_id=None)
        .execution_options(synchronize_session=False)
    )
    db.session.commit()


def mark_order_paid(order):
    """Idempotent, same shape as master_class/services.py's
    mark_enrollment_paid — safe to call from both the callback and the
    webhook, and safe to call twice for the same order. Caller must have
    already confirmed the payment succeeded via Paystack Verify.

    Stage 2 of the two-stage lock: converts THIS order's own reservation
    into a sale via an atomic UPDATE...WHERE holder_order_id=<this order>.
    If that claim fails (rowcount 0) — the reservation lapsed and someone
    else's order won the listing in between — this order is marked
    payment_conflict rather than paid: the buyer was charged but doesn't
    get the listing, and it's surfaced for a manual admin refund. No
    commission is created for a payment_conflict order.
    """
    if order.status in (
        "paid", "payment_conflict", "handoff_in_progress", "completed", "refunded", "cancelled",
    ):
        return

    result = db.session.execute(
        update(ChannelListing)
        .where(ChannelListing.id == order.listing_id, ChannelListing.holder_order_id == order.id)
        .values(availability="sold", sold_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )

    if result.rowcount == 0:
        order.status = "payment_conflict"
        note = (order.admin_note + "\n") if order.admin_note else ""
        order.admin_note = (
            note
            + "AUTO: payment verified but this listing was already sold to another "
            "order by the time payment confirmed. Needs manual refund review."
        )
        db.session.commit()
        return

    order.status = "paid"
    order.paid_at = datetime.now(timezone.utc)

    if order.affiliate_id is not None:
        rate = effective_commission_rate(order.affiliate)
        db.session.add(
            Commission(
                affiliate_id=order.affiliate_id,
                prospect_id=order.prospect_id,
                amount=order.amount * rate / 100,
                rate_percent_snapshot=rate,
                note=f"Auto: channel listing sale, order #{order.id} ({order.buyer_email})",
                created_by_id=None,
                source_order_id=order.id,
            )
        )

    try:
        db.session.commit()
    except IntegrityError:
        # unique(source_order_id) caught a concurrent duplicate webhook
        # delivery — the other caller already recorded the commission.
        db.session.rollback()

from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import Commission, effective_commission_rate


def mark_enrollment_paid(enrollment):
    """Idempotent: safe to call from both the callback and the webhook, and
    safe to call twice for the same enrollment (duplicate webhook delivery).
    Caller is responsible for having already confirmed the payment actually
    succeeded (via Paystack Verify) before calling this — this function
    itself does not talk to Paystack.
    """
    if enrollment.status == "paid":
        return

    enrollment.status = "paid"
    enrollment.paid_at = datetime.now(timezone.utc)

    if enrollment.affiliate_id is not None:
        rate = effective_commission_rate(enrollment.affiliate)
        db.session.add(
            Commission(
                affiliate_id=enrollment.affiliate_id,
                prospect_id=enrollment.prospect_id,
                amount=enrollment.amount * rate / 100,
                rate_percent_snapshot=rate,
                note=f"Auto: Master Class enrollment #{enrollment.id} ({enrollment.buyer_email})",
                created_by_id=None,
                source_enrollment_id=enrollment.id,
            )
        )

    try:
        db.session.commit()
    except IntegrityError:
        # unique(source_enrollment_id) caught a concurrent duplicate — the
        # other caller already recorded the commission, nothing to do here.
        db.session.rollback()

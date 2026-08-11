from ..extensions import db
from ..models import User, LinkClick


def record_click(code, destination):
    """Record a referred visit for `code` at `destination`, if `code`
    resolves to a real, active affiliate. No-ops silently on an unknown or
    inactive code — these are marketing links shared outside our control,
    so a bad/stale code must never break the page the visitor landed on.
    """
    if not code:
        return
    affiliate = User.query.filter_by(
        referral_code=code.upper(), role="affiliate", is_active_flag=True
    ).first()
    if affiliate is None:
        return
    db.session.add(LinkClick(affiliate_id=affiliate.id, destination=destination))
    db.session.commit()

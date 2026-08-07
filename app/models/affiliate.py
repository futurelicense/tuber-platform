from datetime import datetime, timezone

from ..extensions import db

PROSPECT_INTEREST_TYPES = (
    "grow_existing_channel",
    "grow_from_scratch",
    "buy_channel",
    "master_class",
)
PROSPECT_STATUSES = ("new", "contacted", "converted", "lost")
COMMISSION_STATUSES = ("pending", "approved", "paid", "voided")


class Prospect(db.Model):
    """A lead who expressed interest via a referral link or the public
    homepage. affiliate_id is nullable — homepage-direct interest (no
    referral code) is still captured, just unattributed.
    """

    __tablename__ = "prospects"

    id = db.Column(db.Integer, primary_key=True)
    affiliate_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(255), nullable=False, index=True)
    phone = db.Column(db.String(50))
    interest_type = db.Column(db.String(30), nullable=False)
    message = db.Column(db.Text)
    status = db.Column(db.String(20), nullable=False, default="new")
    referral_code_used = db.Column(db.String(12))
    ip_address = db.Column(db.String(64))
    created_at = db.Column(
        db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    affiliate = db.relationship("User", foreign_keys=[affiliate_id])

    __table_args__ = (
        db.CheckConstraint(
            "interest_type in ('grow_existing_channel','grow_from_scratch','buy_channel','master_class')",
            name="ck_prospect_interest_type",
        ),
        db.CheckConstraint(
            "status in ('new','contacted','converted','lost')", name="ck_prospect_status"
        ),
    )


class Commission(db.Model):
    """Admin-controlled (status transitions always go through the admin
    blueprint), but not always admin-created: Master Class checkout and the
    channel marketplace both create these automatically from a paid,
    affiliate-attributed purchase — created_by_id is null in that case,
    since there's no admin in the loop. rate_percent_snapshot freezes the
    rate that was in effect when the commission was created, so a later
    rate change doesn't rewrite history.
    """

    __tablename__ = "commissions"

    id = db.Column(db.Integer, primary_key=True)
    affiliate_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    prospect_id = db.Column(db.Integer, db.ForeignKey("prospects.id"), index=True)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    rate_percent_snapshot = db.Column(db.Numeric(5, 2), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending")
    note = db.Column(db.Text)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    # Set only for commissions auto-created from a paid Master Class
    # enrollment. Unique so a race between duplicate webhook deliveries (or
    # a webhook racing the browser callback) can create at most one
    # Commission per enrollment — the loser hits IntegrityError and is
    # treated as "already handled" rather than double-crediting an affiliate.
    source_enrollment_id = db.Column(
        db.Integer, db.ForeignKey("master_class_enrollments.id"), unique=True
    )
    # Same idempotency role as source_enrollment_id, for a paid channel
    # order instead of a Master Class enrollment. A single FK column can't
    # validly reference two different tables, so this is a second dedicated
    # column rather than a shared polymorphic one — keeps both uniqueness
    # guarantees DB-enforced rather than relying on app-level convention.
    source_order_id = db.Column(db.Integer, db.ForeignKey("channel_orders.id"), unique=True)
    created_at = db.Column(
        db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    approved_at = db.Column(db.DateTime(timezone=True))
    paid_at = db.Column(db.DateTime(timezone=True))

    affiliate = db.relationship("User", foreign_keys=[affiliate_id])
    prospect = db.relationship("Prospect", foreign_keys=[prospect_id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])

    __table_args__ = (
        db.CheckConstraint(
            "status in ('pending','approved','paid','voided')", name="ck_commission_status"
        ),
        db.CheckConstraint(
            "source_enrollment_id is null or source_order_id is null",
            name="ck_commission_single_source",
        ),
    )


def effective_commission_rate(affiliate):
    """The rate a new commission for this affiliate should use: their own
    override if set, else the program-wide default. Shared by the admin
    blueprint's manual commission creation and Master Class's automatic
    commission creation so the two paths can never compute a different rate
    for the same affiliate.
    """
    return affiliate.commission_rate_percent or AffiliateProgramSettings.get().default_commission_rate_percent


class AffiliateProgramSettings(db.Model):
    """Singleton row (id=1) for the admin-configured default commission
    rate. Seeded by migration; get() is a defensive get-or-create fallback.
    """

    __tablename__ = "affiliate_program_settings"

    id = db.Column(db.Integer, primary_key=True)
    default_commission_rate_percent = db.Column(db.Numeric(5, 2), nullable=False, default=10)
    updated_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    @classmethod
    def get(cls):
        row = cls.query.get(1)
        if row is None:
            row = cls(id=1, default_commission_rate_percent=10)
            db.session.add(row)
            db.session.commit()
        return row

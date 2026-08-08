from datetime import datetime, timezone

from ..extensions import db

LISTING_STATUSES = ("draft", "published", "withdrawn")
LISTING_AVAILABILITY = ("available", "reserved", "sold")
MONETIZATION_STATUSES = ("monetized", "unmonetized")
ORDER_STATUSES = (
    "pending",
    "failed",
    "paid",
    "payment_conflict",
    "handoff_in_progress",
    "completed",
    "refunded",
    "cancelled",
)


class ChannelListing(db.Model):
    """A single, unique YouTube channel/account for sale. `status` is
    admin's visibility control (draft/published/withdrawn); `availability`
    is the separate inventory-lock axis (available/reserved/sold) — see
    app/marketplace/services.py for why a listing needs both.
    """

    __tablename__ = "channel_listings"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    niche = db.Column(db.String(100))
    monetization_status = db.Column(db.String(20), nullable=False)
    subscriber_count = db.Column(db.Integer)
    total_views = db.Column(db.Integer)
    stats_note = db.Column(db.Text)
    price = db.Column(db.Numeric(10, 2), nullable=False)
    currency = db.Column(db.String(3), nullable=False, default="NGN")
    status = db.Column(db.String(20), nullable=False, default="draft")
    availability = db.Column(db.String(20), nullable=False, default="available")
    # FK added via a later batch_alter_table in the migration — channel_orders
    # doesn't exist yet at the point this table is created (mutual reference).
    holder_order_id = db.Column(db.Integer)
    reserved_at = db.Column(db.DateTime(timezone=True))
    sold_at = db.Column(db.DateTime(timezone=True))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    created_by = db.relationship("User", foreign_keys=[created_by_id])
    attachments = db.relationship(
        "ListingAttachment", order_by="ListingAttachment.created_at", viewonly=True
    )

    __table_args__ = (
        db.CheckConstraint(
            "monetization_status in ('monetized','unmonetized')",
            name="ck_listing_monetization_status",
        ),
        db.CheckConstraint(
            "status in ('draft','published','withdrawn')", name="ck_listing_status"
        ),
        db.CheckConstraint(
            "availability in ('available','reserved','sold')", name="ck_listing_availability"
        ),
    )


class ListingAttachment(db.Model):
    """A proof-of-ownership image (analytics screenshot, verification
    image) attached to a listing. filename is the safe, server-generated
    on-disk name (never the buyer/admin's original) — see
    app/admin/routes.py's upload handling for why. Files live under
    Config.LISTING_UPLOAD_DIR, served publicly (no auth — buyers need to
    see these) via GET /marketplace/uploads/<filename>.
    """

    __tablename__ = "listing_attachments"

    id = db.Column(db.Integer, primary_key=True)
    listing_id = db.Column(
        db.Integer, db.ForeignKey("channel_listings.id"), nullable=False, index=True
    )
    filename = db.Column(db.String(150), nullable=False, unique=True)
    original_filename = db.Column(db.String(255))
    content_type = db.Column(db.String(50))
    size_bytes = db.Column(db.Integer)
    created_at = db.Column(
        db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


class ChannelOrder(db.Model):
    """Guest checkout (no account) for a single ChannelListing. Payment is
    real (Paystack); the actual channel handoff is manual/admin-mediated —
    this row just tracks payment + handoff status, never any credentials.
    """

    __tablename__ = "channel_orders"

    id = db.Column(db.Integer, primary_key=True)
    listing_id = db.Column(db.Integer, db.ForeignKey("channel_listings.id"), nullable=False, index=True)
    buyer_name = db.Column(db.String(200), nullable=False)
    buyer_email = db.Column(db.String(255), nullable=False, index=True)
    buyer_phone = db.Column(db.String(50))
    prospect_id = db.Column(db.Integer, db.ForeignKey("prospects.id"), index=True)
    affiliate_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    referral_code_used = db.Column(db.String(12))
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    currency = db.Column(db.String(3), nullable=False, default="NGN")
    paystack_reference = db.Column(db.String(100), nullable=False, unique=True, index=True)
    status = db.Column(db.String(20), nullable=False, default="pending")
    admin_note = db.Column(db.Text)
    paid_at = db.Column(db.DateTime(timezone=True))
    completed_at = db.Column(db.DateTime(timezone=True))
    ip_address = db.Column(db.String(64))
    created_at = db.Column(
        db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    listing = db.relationship("ChannelListing", foreign_keys=[listing_id])
    prospect = db.relationship("Prospect", foreign_keys=[prospect_id])
    affiliate = db.relationship("User", foreign_keys=[affiliate_id])

    __table_args__ = (
        db.CheckConstraint(
            "status in ('pending','failed','paid','payment_conflict','handoff_in_progress',"
            "'completed','refunded','cancelled')",
            name="ck_order_status",
        ),
    )

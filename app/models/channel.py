from datetime import datetime, timezone

from ..extensions import db


class ConnectedChannel(db.Model):
    """Schema placeholder for per-tuber OAuth-connected channels.

    Not wired to a real OAuth flow in Phase 1 — Clipper still uses its own
    shared google_token.json/tiktok_token.json files. This table exists so
    that swapping Clipper over to per-tuber tokens later is additive rather
    than a schema rework.
    """

    __tablename__ = "connected_channels"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    provider = db.Column(db.String(30), nullable=False)
    external_account_id = db.Column(db.String(255))
    channel_id = db.Column(db.String(255))
    channel_title = db.Column(db.String(255))
    token_blob = db.Column(db.Text)  # encrypted (or plain-JSON in dev) via app/crypto.py
    scopes = db.Column(db.String(500))
    status = db.Column(db.String(20), nullable=False, default="disconnected")
    connected_at = db.Column(db.DateTime(timezone=True))
    updated_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        db.CheckConstraint(
            "provider in ('youtube','tiktok','google_drive')", name="ck_channels_provider"
        ),
        db.CheckConstraint(
            "status in ('connected','disconnected','error')", name="ck_channels_status"
        ),
        db.UniqueConstraint("user_id", "provider", "channel_id", name="uq_channel_per_user"),
    )

    user = db.relationship("User", foreign_keys=[user_id])

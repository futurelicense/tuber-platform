from datetime import datetime, timezone

from ..extensions import db


class LoginEvent(db.Model):
    __tablename__ = "login_events"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    ip_address = db.Column(db.String(64), nullable=False)
    geo_country = db.Column(db.String(120))
    geo_region = db.Column(db.String(120))
    geo_city = db.Column(db.String(120))
    geo_lat = db.Column(db.Numeric)
    geo_lon = db.Column(db.Numeric)
    geo_raw = db.Column(db.JSON)
    user_agent = db.Column(db.String(500))
    success = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(
        db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )

    user = db.relationship("User", foreign_keys=[user_id])


class ActivityLog(db.Model):
    __tablename__ = "activity_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    action = db.Column(db.String(120), nullable=False, index=True)
    route = db.Column(db.String(255))
    method = db.Column(db.String(10))
    status_code = db.Column(db.Integer)
    meta = db.Column(db.JSON)
    ip_address = db.Column(db.String(64))
    created_at = db.Column(
        db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )

    user = db.relationship("User", foreign_keys=[user_id])

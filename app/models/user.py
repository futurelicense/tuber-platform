from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from ..extensions import db

ROLES = ("admin", "clipper", "producer")


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    display_name = db.Column(db.String(120))
    is_active_flag = db.Column("is_active", db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    last_login_at = db.Column(db.DateTime(timezone=True))

    __table_args__ = (
        db.CheckConstraint("role in ('admin','clipper','producer')", name="ck_users_role"),
    )

    def set_password(self, raw_password):
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        return check_password_hash(self.password_hash, raw_password)

    # Flask-Login expects `is_active` as a property; the DB column is named
    # is_active_flag to avoid shadowing UserMixin's default implementation.
    @property
    def is_active(self):
        return self.is_active_flag

    def can_access(self, section):
        """section is 'clip' or 'produce'."""
        if self.role == "admin":
            return True
        return (section == "clip" and self.role == "clipper") or (
            section == "produce" and self.role == "producer"
        )

    def __repr__(self):
        return f"<User {self.email} ({self.role})>"

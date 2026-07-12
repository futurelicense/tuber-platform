from datetime import datetime, timezone

from ..extensions import db


class MetricDefinition(db.Model):
    """Admin-configurable metric. Generic on purpose — real metrics (clips
    published, views, etc) aren't defined yet, so this is just a named,
    typed slot admins can create and later feed MetricEvent rows into.
    """

    __tablename__ = "metric_definitions"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(80), unique=True, nullable=False)
    label = db.Column(db.String(200), nullable=False)
    data_type = db.Column(db.String(20), nullable=False)
    applies_to_role = db.Column(db.String(20))  # null = both clipper and producer
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.CheckConstraint(
            "data_type in ('integer','float','boolean','duration_seconds')",
            name="ck_metric_data_type",
        ),
        db.CheckConstraint(
            "applies_to_role is null or applies_to_role in ('clipper','producer')",
            name="ck_metric_applies_to_role",
        ),
    )


class MetricEvent(db.Model):
    """Raw observation feeding a metric. Phase 1: admin-entered manually.
    Later: derived automatically from ActivityLog once Clipper/ytproduction
    routes are instrumented with real action semantics.
    """

    __tablename__ = "metric_events"

    id = db.Column(db.Integer, primary_key=True)
    metric_id = db.Column(db.Integer, db.ForeignKey("metric_definitions.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    value = db.Column(db.Numeric, nullable=False)
    occurred_at = db.Column(
        db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    source = db.Column(db.String(40), default="manual_admin_entry")
    meta = db.Column(db.JSON)

    metric = db.relationship("MetricDefinition")
    user = db.relationship("User")


class RewardRule(db.Model):
    """Admin-configurable rule tying a metric threshold to a reward
    description. CRUD only in Phase 1 — no evaluation/payout engine yet.
    """

    __tablename__ = "reward_rules"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    metric_id = db.Column(db.Integer, db.ForeignKey("metric_definitions.id"), nullable=False)
    operator = db.Column(db.String(10), nullable=False)
    threshold_low = db.Column(db.Numeric)
    threshold_high = db.Column(db.Numeric)
    reward_description = db.Column(db.Text)
    applies_to_role = db.Column(db.String(20))
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    metric = db.relationship("MetricDefinition")

    __table_args__ = (
        db.CheckConstraint("operator in ('gte','lte','eq','between')", name="ck_reward_operator"),
        db.CheckConstraint(
            "applies_to_role is null or applies_to_role in ('clipper','producer')",
            name="ck_reward_applies_to_role",
        ),
    )

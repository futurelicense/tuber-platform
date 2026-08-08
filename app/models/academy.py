from datetime import datetime, timezone

from ..extensions import db

COURSE_STATUSES = ("draft", "published", "archived")
COURSE_CATALOGS = ("tool", "use_case", "challenge")
LESSON_TYPES = ("read", "listen", "interactive", "video")
PROGRESS_STATUSES = ("not_started", "in_progress", "completed")
SUBSCRIPTION_STATUSES = ("trialing", "active", "past_due", "canceled")


class AcademySettings(db.Model):
    """Singleton (id=1). Controls whether Academy is open and whether a
    paid subscription is required to take lessons.
    """

    __tablename__ = "academy_settings"

    id = db.Column(db.Integer, primary_key=True)
    is_open = db.Column(db.Boolean, nullable=False, default=True)
    require_subscription = db.Column(db.Boolean, nullable=False, default=False)
    price_amount = db.Column(db.Numeric(10, 2), nullable=False, default=5000)
    currency = db.Column(db.String(3), nullable=False, default="NGN")
    updated_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    @classmethod
    def get(cls):
        row = cls.query.get(1)
        if row is None:
            row = cls(
                id=1,
                is_open=True,
                require_subscription=False,
                price_amount=5000,
                currency="NGN",
            )
            db.session.add(row)
            db.session.commit()
        return row


class AcademySubscription(db.Model):
    """Learner subscription row — Paystack wiring comes later; admin can
    flip status manually for now.
    """

    __tablename__ = "academy_subscriptions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True, index=True)
    status = db.Column(db.String(20), nullable=False, default="active")
    started_at = db.Column(
        db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    ends_at = db.Column(db.DateTime(timezone=True))
    updated_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user = db.relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        db.CheckConstraint(
            "status in ('trialing','active','past_due','canceled')",
            name="ck_academy_subscription_status",
        ),
    )


class AcademyCourse(db.Model):
    """A learner-facing course (tool deep-dive, use-case path, or challenge).
    Admin authors the curriculum as units → lessons; learner UI comes later.
    """

    __tablename__ = "academy_courses"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(220), nullable=False, unique=True, index=True)
    summary = db.Column(db.String(400))
    description = db.Column(db.Text)
    catalog = db.Column(db.String(20), nullable=False, default="tool")
    category = db.Column(db.String(100))
    tags = db.Column(db.String(255))
    estimated_lessons = db.Column(db.Integer)
    estimated_hours = db.Column(db.Numeric(5, 1))
    cover_image_url = db.Column(db.String(500))
    status = db.Column(db.String(20), nullable=False, default="draft")
    is_featured = db.Column(db.Boolean, nullable=False, default=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
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
    units = db.relationship(
        "AcademyUnit",
        back_populates="course",
        order_by="AcademyUnit.sort_order",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        db.CheckConstraint(
            "catalog in ('tool','use_case','challenge')", name="ck_academy_course_catalog"
        ),
        db.CheckConstraint(
            "status in ('draft','published','archived')", name="ck_academy_course_status"
        ),
    )


class AcademyUnit(db.Model):
    """A path segment inside a course (e.g. UNIT 1 Gemini)."""

    __tablename__ = "academy_units"

    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(
        db.Integer, db.ForeignKey("academy_courses.id"), nullable=False, index=True
    )
    title = db.Column(db.String(200), nullable=False)
    section_label = db.Column(db.String(200))
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(
        db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    course = db.relationship("AcademyCourse", back_populates="units")
    lessons = db.relationship(
        "AcademyLesson",
        back_populates="unit",
        order_by="AcademyLesson.sort_order",
        cascade="all, delete-orphan",
    )


class AcademyLesson(db.Model):
    """A single node on the learning path."""

    __tablename__ = "academy_lessons"

    id = db.Column(db.Integer, primary_key=True)
    unit_id = db.Column(
        db.Integer, db.ForeignKey("academy_units.id"), nullable=False, index=True
    )
    title = db.Column(db.String(200), nullable=False)
    lesson_type = db.Column(db.String(20), nullable=False, default="read")
    content = db.Column(db.Text)
    estimated_minutes = db.Column(db.Integer)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    is_published = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(
        db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    unit = db.relationship("AcademyUnit", back_populates="lessons")

    __table_args__ = (
        db.CheckConstraint(
            "lesson_type in ('read','listen','interactive','video')",
            name="ck_academy_lesson_type",
        ),
    )


class AcademyLessonProgress(db.Model):
    """Per-learner lesson state — scaffold for the future learner app."""

    __tablename__ = "academy_lesson_progress"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    lesson_id = db.Column(
        db.Integer, db.ForeignKey("academy_lessons.id"), nullable=False, index=True
    )
    status = db.Column(db.String(20), nullable=False, default="not_started")
    progress_percent = db.Column(db.Integer, nullable=False, default=0)
    completed_at = db.Column(db.DateTime(timezone=True))
    updated_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user = db.relationship("User", foreign_keys=[user_id])
    lesson = db.relationship("AcademyLesson", foreign_keys=[lesson_id])

    __table_args__ = (
        db.UniqueConstraint("user_id", "lesson_id", name="uq_academy_progress_user_lesson"),
        db.CheckConstraint(
            "status in ('not_started','in_progress','completed')",
            name="ck_academy_progress_status",
        ),
    )

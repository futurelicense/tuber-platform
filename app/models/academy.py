from datetime import datetime, timezone

from ..extensions import db

COURSE_STATUSES = ("draft", "published", "archived")
COURSE_CATALOGS = ("tool", "use_case", "challenge")
LESSON_TYPES = ("read", "listen", "interactive", "video")
PROGRESS_STATUSES = ("not_started", "in_progress", "completed")

# Predefined category pills shown in Admin + learner filters (keyed by catalog).
COURSE_CATEGORIES_BY_CATALOG = {
    "tool": (
        "New",
        "Research & Analysis",
        "No-Code Apps",
        "Writing",
        "Business",
        "Operations",
        "Image Generation",
    ),
    "use_case": (
        "New",
        "Business",
        "Operations",
        "Marketing and Growth",
        "Self-improvement",
        "Creation",
    ),
    "challenge": (
        "New",
        "Beginner",
        "Intermediate",
        "Certificate",
    ),
}
COURSE_CATEGORIES = tuple(
    sorted({c for cats in COURSE_CATEGORIES_BY_CATALOG.values() for c in cats})
)
ENTRY_PATHS = ("direct_pay", "relate_admin")
SUBSCRIPTION_STATUSES = (
    "pending",
    "active",
    "relate_only",
    "failed",
    "past_due",
    "canceled",
)
MESSAGE_SENDERS = ("learner", "admin")


class AcademySettings(db.Model):
    """Singleton (id=1). Direct-entry price and open flag."""

    __tablename__ = "academy_settings"

    id = db.Column(db.Integer, primary_key=True)
    is_open = db.Column(db.Boolean, nullable=False, default=True)
    require_subscription = db.Column(db.Boolean, nullable=False, default=True)
    price_amount = db.Column(db.Numeric(10, 2), nullable=False, default=25)
    currency = db.Column(db.String(3), nullable=False, default="USD")
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
                require_subscription=True,
                price_amount=25,
                currency="USD",
            )
            db.session.add(row)
            db.session.commit()
        return row


class AcademySubscription(db.Model):
    """Learner enrollment: either paid direct entry or relate-with-admin."""

    __tablename__ = "academy_subscriptions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True, index=True)
    status = db.Column(db.String(20), nullable=False, default="pending")
    entry_path = db.Column(db.String(20), nullable=False, default="direct_pay")
    whatsapp = db.Column(db.String(50))
    ai_knowledge = db.Column(db.Integer)  # 1–5
    affiliate_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    referral_code_used = db.Column(db.String(12))
    amount = db.Column(db.Numeric(10, 2))
    currency = db.Column(db.String(3))
    paystack_reference = db.Column(db.String(100), unique=True, index=True)
    paid_at = db.Column(db.DateTime(timezone=True))
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
    affiliate = db.relationship("User", foreign_keys=[affiliate_id])

    __table_args__ = (
        db.CheckConstraint(
            "status in ('pending','active','relate_only','failed','past_due','canceled')",
            name="ck_academy_subscription_status",
        ),
        db.CheckConstraint(
            "entry_path in ('direct_pay','relate_admin')",
            name="ck_academy_subscription_entry_path",
        ),
        db.CheckConstraint(
            "ai_knowledge is null or (ai_knowledge >= 1 and ai_knowledge <= 5)",
            name="ck_academy_subscription_ai_knowledge",
        ),
    )


class AcademyMessage(db.Model):
    """Learner ↔ admin thread for relate-with-admin path (and support)."""

    __tablename__ = "academy_messages"

    id = db.Column(db.Integer, primary_key=True)
    learner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    sender_role = db.Column(db.String(20), nullable=False)
    sender_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    read_at = db.Column(db.DateTime(timezone=True))

    learner = db.relationship("User", foreign_keys=[learner_id])
    sender = db.relationship("User", foreign_keys=[sender_id])

    __table_args__ = (
        db.CheckConstraint(
            "sender_role in ('learner','admin')", name="ck_academy_message_sender_role"
        ),
    )


class AcademyCatalog(db.Model):
    """Admin-managed course catalog (e.g. tool, use_case, challenge)."""

    __tablename__ = "academy_catalogs"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(50), nullable=False, unique=True, index=True)
    label = db.Column(db.String(100), nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(
        db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    categories = db.relationship(
        "AcademyCategory",
        back_populates="catalog",
        order_by="AcademyCategory.sort_order",
        cascade="all, delete-orphan",
    )


class AcademyCategory(db.Model):
    """Admin-managed category pill within a catalog."""

    __tablename__ = "academy_categories"

    id = db.Column(db.Integer, primary_key=True)
    catalog_id = db.Column(
        db.Integer, db.ForeignKey("academy_catalogs.id"), nullable=False, index=True
    )
    name = db.Column(db.String(100), nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(
        db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    catalog = db.relationship("AcademyCatalog", back_populates="categories")

    __table_args__ = (
        db.UniqueConstraint("catalog_id", "name", name="uq_academy_category_catalog_name"),
    )


class AcademyCourse(db.Model):
    """A learner-facing course (tool deep-dive, use-case path, or challenge)."""

    __tablename__ = "academy_courses"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(220), nullable=False, unique=True, index=True)
    summary = db.Column(db.String(400))
    description = db.Column(db.Text)
    catalog = db.Column(db.String(50), nullable=False, default="tool")
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
            "status in ('draft','published','archived')", name="ck_academy_course_status"
        ),
    )


class AcademyUnit(db.Model):
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
    __tablename__ = "academy_lessons"

    id = db.Column(db.Integer, primary_key=True)
    unit_id = db.Column(
        db.Integer, db.ForeignKey("academy_units.id"), nullable=False, index=True
    )
    title = db.Column(db.String(200), nullable=False)
    lesson_type = db.Column(db.String(20), nullable=False, default="read")
    # Rich HTML for read body / listen transcript / video notes / interactive intro.
    content = db.Column(db.Text)
    # Type-specific media (filenames under LISTING_UPLOAD_DIR, served via marketplace.uploads).
    audio_filename = db.Column(db.String(255))
    video_filename = db.Column(db.String(255))
    video_embed_url = db.Column(db.String(500))  # YouTube/Vimeo/etc. when not uploading
    # Interactive prompt-builder fields
    interactive_instruction = db.Column(db.Text)
    interactive_template = db.Column(db.Text)  # e.g. "A beautiful [subject] in [setting]"
    interactive_choices = db.Column(db.Text)  # newline-separated chip options
    interactive_check_tip = db.Column(db.Text)
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

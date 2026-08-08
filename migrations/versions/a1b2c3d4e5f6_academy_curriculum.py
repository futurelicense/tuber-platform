"""academy curriculum

Revision ID: a1b2c3d4e5f6
Revises: fc3bf5a9a5c6
Create Date: 2026-08-08 19:45:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d4e5f6"
down_revision = "fc3bf5a9a5c6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "academy_courses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=220), nullable=False),
        sa.Column("summary", sa.String(length=400), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("catalog", sa.String(length=20), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=True),
        sa.Column("tags", sa.String(length=255), nullable=True),
        sa.Column("estimated_lessons", sa.Integer(), nullable=True),
        sa.Column("estimated_hours", sa.Numeric(precision=5, scale=1), nullable=True),
        sa.Column("cover_image_url", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("is_featured", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "catalog in ('tool','use_case','challenge')", name="ck_academy_course_catalog"
        ),
        sa.CheckConstraint(
            "status in ('draft','published','archived')", name="ck_academy_course_status"
        ),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    with op.batch_alter_table("academy_courses", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_academy_courses_created_at"), ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_academy_courses_slug"), ["slug"], unique=True)

    op.create_table(
        "academy_units",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("course_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("section_label", sa.String(length=200), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["course_id"], ["academy_courses.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("academy_units", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_academy_units_course_id"), ["course_id"], unique=False)

    op.create_table(
        "academy_lessons",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("unit_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("lesson_type", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("estimated_minutes", sa.Integer(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_published", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "lesson_type in ('read','listen','interactive','video')",
            name="ck_academy_lesson_type",
        ),
        sa.ForeignKeyConstraint(["unit_id"], ["academy_units.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("academy_lessons", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_academy_lessons_unit_id"), ["unit_id"], unique=False)

    op.create_table(
        "academy_lesson_progress",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("lesson_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("progress_percent", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status in ('not_started','in_progress','completed')",
            name="ck_academy_progress_status",
        ),
        sa.ForeignKeyConstraint(["lesson_id"], ["academy_lessons.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "lesson_id", name="uq_academy_progress_user_lesson"),
    )
    with op.batch_alter_table("academy_lesson_progress", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_academy_lesson_progress_lesson_id"), ["lesson_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_academy_lesson_progress_user_id"), ["user_id"], unique=False
        )


def downgrade():
    with op.batch_alter_table("academy_lesson_progress", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_academy_lesson_progress_user_id"))
        batch_op.drop_index(batch_op.f("ix_academy_lesson_progress_lesson_id"))
    op.drop_table("academy_lesson_progress")

    with op.batch_alter_table("academy_lessons", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_academy_lessons_unit_id"))
    op.drop_table("academy_lessons")

    with op.batch_alter_table("academy_units", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_academy_units_course_id"))
    op.drop_table("academy_units")

    with op.batch_alter_table("academy_courses", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_academy_courses_slug"))
        batch_op.drop_index(batch_op.f("ix_academy_courses_created_at"))
    op.drop_table("academy_courses")

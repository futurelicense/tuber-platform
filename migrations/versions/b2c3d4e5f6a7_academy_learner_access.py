"""academy learner access

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-08 20:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_constraint("ck_users_role", type_="check")
        batch_op.create_check_constraint(
            "ck_users_role",
            "role in ('admin','clipper','producer','affiliate','learner')",
        )

    op.create_table(
        "academy_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("is_open", sa.Boolean(), nullable=False),
        sa.Column("require_subscription", sa.Boolean(), nullable=False),
        sa.Column("price_amount", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    academy_settings = sa.table(
        "academy_settings",
        sa.column("id", sa.Integer),
        sa.column("is_open", sa.Boolean),
        sa.column("require_subscription", sa.Boolean),
        sa.column("price_amount", sa.Numeric),
        sa.column("currency", sa.String),
    )
    op.bulk_insert(
        academy_settings,
        [{"id": 1, "is_open": True, "require_subscription": False, "price_amount": 5000, "currency": "NGN"}],
    )

    op.create_table(
        "academy_subscriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status in ('trialing','active','past_due','canceled')",
            name="ck_academy_subscription_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )
    with op.batch_alter_table("academy_subscriptions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_academy_subscriptions_user_id"), ["user_id"], unique=True)


def downgrade():
    with op.batch_alter_table("academy_subscriptions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_academy_subscriptions_user_id"))
    op.drop_table("academy_subscriptions")
    op.drop_table("academy_settings")

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_constraint("ck_users_role", type_="check")
        batch_op.create_check_constraint(
            "ck_users_role",
            "role in ('admin','clipper','producer','affiliate')",
        )

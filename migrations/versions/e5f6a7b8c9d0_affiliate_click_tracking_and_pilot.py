"""affiliate click tracking, pilot cohort flag, default landing setting

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-08-11 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "link_clicks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("affiliate_id", sa.Integer(), nullable=False),
        sa.Column("destination", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["affiliate_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("link_clicks", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_link_clicks_affiliate_id"), ["affiliate_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_link_clicks_created_at"), ["created_at"], unique=False
        )

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("is_pilot", sa.Boolean(), nullable=False, server_default=sa.false())
        )

    with op.batch_alter_table("affiliate_program_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "default_landing",
                sa.String(length=20),
                nullable=False,
                server_default="academy_signup",
            )
        )
        batch_op.create_check_constraint(
            "ck_affiliate_settings_default_landing",
            "default_landing in ('academy_signup','academy_home','marketplace','contact')",
        )


def downgrade():
    with op.batch_alter_table("affiliate_program_settings", schema=None) as batch_op:
        batch_op.drop_constraint("ck_affiliate_settings_default_landing", type_="check")
        batch_op.drop_column("default_landing")

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("is_pilot")

    with op.batch_alter_table("link_clicks", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_link_clicks_created_at"))
        batch_op.drop_index(batch_op.f("ix_link_clicks_affiliate_id"))
    op.drop_table("link_clicks")

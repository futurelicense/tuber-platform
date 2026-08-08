"""academy entry paths messaging affiliates

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-08 20:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("academy_subscriptions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("entry_path", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("whatsapp", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("ai_knowledge", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("affiliate_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("referral_code_used", sa.String(length=12), nullable=True))
        batch_op.add_column(sa.Column("amount", sa.Numeric(precision=10, scale=2), nullable=True))
        batch_op.add_column(sa.Column("currency", sa.String(length=3), nullable=True))
        batch_op.add_column(sa.Column("paystack_reference", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_academy_subscriptions_affiliate_id"), ["affiliate_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_academy_subscriptions_paystack_reference"),
            ["paystack_reference"],
            unique=True,
        )
        batch_op.create_foreign_key(
            "fk_academy_subscriptions_affiliate_id",
            "users",
            ["affiliate_id"],
            ["id"],
        )

    op.execute(
        "UPDATE academy_subscriptions SET entry_path = 'direct_pay' WHERE entry_path IS NULL"
    )
    op.execute(
        "UPDATE academy_subscriptions SET status = 'active' "
        "WHERE status NOT IN ('pending','active','relate_only','failed','past_due','canceled')"
    )

    with op.batch_alter_table("academy_subscriptions", schema=None) as batch_op:
        batch_op.alter_column("entry_path", existing_type=sa.String(length=20), nullable=False)
        batch_op.drop_constraint("ck_academy_subscription_status", type_="check")
        batch_op.create_check_constraint(
            "ck_academy_subscription_status",
            "status in ('pending','active','relate_only','failed','past_due','canceled')",
        )
        batch_op.create_check_constraint(
            "ck_academy_subscription_entry_path",
            "entry_path in ('direct_pay','relate_admin')",
        )
        batch_op.create_check_constraint(
            "ck_academy_subscription_ai_knowledge",
            "ai_knowledge is null or (ai_knowledge >= 1 and ai_knowledge <= 5)",
        )

    op.execute(
        "UPDATE academy_settings SET require_subscription = true, "
        "price_amount = 25, currency = 'USD' WHERE id = 1"
    )

    op.create_table(
        "academy_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("learner_id", sa.Integer(), nullable=False),
        sa.Column("sender_role", sa.String(length=20), nullable=False),
        sa.Column("sender_id", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "sender_role in ('learner','admin')", name="ck_academy_message_sender_role"
        ),
        sa.ForeignKeyConstraint(["learner_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["sender_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("academy_messages", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_academy_messages_created_at"), ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_academy_messages_learner_id"), ["learner_id"], unique=False)

    with op.batch_alter_table("commissions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("source_academy_subscription_id", sa.Integer(), nullable=True)
        )
        batch_op.drop_constraint("ck_commission_single_source", type_="check")
        batch_op.create_check_constraint(
            "ck_commission_single_source",
            "(CASE WHEN source_enrollment_id IS NULL THEN 0 ELSE 1 END)"
            " + (CASE WHEN source_order_id IS NULL THEN 0 ELSE 1 END)"
            " + (CASE WHEN source_academy_subscription_id IS NULL THEN 0 ELSE 1 END) <= 1",
        )
        batch_op.create_unique_constraint(
            "uq_commissions_source_academy_subscription_id",
            ["source_academy_subscription_id"],
        )
        batch_op.create_foreign_key(
            "fk_commissions_source_academy_subscription_id",
            "academy_subscriptions",
            ["source_academy_subscription_id"],
            ["id"],
        )


def downgrade():
    with op.batch_alter_table("commissions", schema=None) as batch_op:
        batch_op.drop_constraint("fk_commissions_source_academy_subscription_id", type_="foreignkey")
        batch_op.drop_constraint("uq_commissions_source_academy_subscription_id", type_="unique")
        batch_op.drop_constraint("ck_commission_single_source", type_="check")
        batch_op.create_check_constraint(
            "ck_commission_single_source",
            "source_enrollment_id is null or source_order_id is null",
        )
        batch_op.drop_column("source_academy_subscription_id")

    with op.batch_alter_table("academy_messages", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_academy_messages_learner_id"))
        batch_op.drop_index(batch_op.f("ix_academy_messages_created_at"))
    op.drop_table("academy_messages")

    with op.batch_alter_table("academy_subscriptions", schema=None) as batch_op:
        batch_op.drop_constraint("ck_academy_subscription_ai_knowledge", type_="check")
        batch_op.drop_constraint("ck_academy_subscription_entry_path", type_="check")
        batch_op.drop_constraint("ck_academy_subscription_status", type_="check")
        batch_op.create_check_constraint(
            "ck_academy_subscription_status",
            "status in ('trialing','active','past_due','canceled')",
        )
        batch_op.drop_constraint("fk_academy_subscriptions_affiliate_id", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_academy_subscriptions_paystack_reference"))
        batch_op.drop_index(batch_op.f("ix_academy_subscriptions_affiliate_id"))
        batch_op.drop_column("paid_at")
        batch_op.drop_column("paystack_reference")
        batch_op.drop_column("currency")
        batch_op.drop_column("amount")
        batch_op.drop_column("referral_code_used")
        batch_op.drop_column("affiliate_id")
        batch_op.drop_column("ai_knowledge")
        batch_op.drop_column("whatsapp")
        batch_op.drop_column("entry_path")

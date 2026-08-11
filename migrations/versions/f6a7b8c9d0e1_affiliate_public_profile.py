"""affiliate public profile fields

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-08-11 15:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("profile_photo_url", sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column("profile_headline", sa.String(length=160), nullable=True))
        batch_op.add_column(sa.Column("profile_bio", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("profile_bio")
        batch_op.drop_column("profile_headline")
        batch_op.drop_column("profile_photo_url")

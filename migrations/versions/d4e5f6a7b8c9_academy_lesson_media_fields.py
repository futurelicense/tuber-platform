"""academy lesson media and interactive fields

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-08-09 02:50:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("academy_lessons", schema=None) as batch_op:
        batch_op.add_column(sa.Column("audio_filename", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("video_filename", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("video_embed_url", sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column("interactive_instruction", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("interactive_template", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("interactive_choices", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("interactive_check_tip", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("academy_lessons", schema=None) as batch_op:
        batch_op.drop_column("interactive_check_tip")
        batch_op.drop_column("interactive_choices")
        batch_op.drop_column("interactive_template")
        batch_op.drop_column("interactive_instruction")
        batch_op.drop_column("video_embed_url")
        batch_op.drop_column("video_filename")
        batch_op.drop_column("audio_filename")

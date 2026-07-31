"""Record login attempts for unknown emails: nullable user_id + attempted_email

Revision ID: c8a41d27f5b9
Revises: e441130b504a
Create Date: 2026-07-31

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'c8a41d27f5b9'
down_revision = 'e441130b504a'
branch_labels = None
depends_on = None


def upgrade():
    # batch_alter_table so the ALTER also works on local SQLite dev DBs.
    with op.batch_alter_table('login_events') as batch_op:
        batch_op.alter_column('user_id', existing_type=sa.Integer(), nullable=True)
        batch_op.add_column(sa.Column('attempted_email', sa.String(length=255), nullable=True))


def downgrade():
    op.execute("DELETE FROM login_events WHERE user_id IS NULL")
    with op.batch_alter_table('login_events') as batch_op:
        batch_op.drop_column('attempted_email')
        batch_op.alter_column('user_id', existing_type=sa.Integer(), nullable=False)

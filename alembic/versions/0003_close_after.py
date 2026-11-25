"""session close_after (auto-close 5 minutes after the last push / undo)

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-23
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_sessions", sa.Column("close_after", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("agent_sessions", "close_after")

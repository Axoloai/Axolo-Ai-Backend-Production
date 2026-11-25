"""session completed_at (set on the first successful push)

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-23
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_sessions", sa.Column("completed_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("agent_sessions", "completed_at")

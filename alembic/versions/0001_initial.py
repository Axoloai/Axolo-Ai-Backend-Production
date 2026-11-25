"""initial tables for the git agent

Revision ID: 0001
Revises:
Create Date: 2026-11-23
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("client_id", sa.String(128), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("provider_user", sa.String(255)),
        sa.Column("token_enc", sa.Text()),
        sa.Column("repo", sa.String(255)),
        sa.Column("branch", sa.String(255)),
        sa.Column("live_url", sa.Text()),
        sa.Column("agent_session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pending_note", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_agent_sessions_client_id", "agent_sessions", ["client_id"])

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("client_id", sa.String(128), nullable=False),
        sa.Column("who", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_chat_session_id", "chat_messages", ["session_id", "id"])

    op.create_table(
        "changes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("client_id", sa.String(128), nullable=False),
        sa.Column("sha", sa.String(64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("files", postgresql.JSONB(), nullable=False),
        sa.Column("undone", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_changes_session_id", "changes", ["session_id", "id"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("client_id", sa.String(128), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_jobs_session_id", "jobs", ["session_id"])

    op.create_table(
        "agent_transcripts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("agent_session_id", sa.String(64), nullable=False),
        sa.Column("subpath", sa.String(255), nullable=False),
        sa.Column("entry_uuid", sa.String(64)),
        sa.Column("entry", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_transcript_key", "agent_transcripts", ["agent_session_id", "subpath", "id"])
    op.create_index("uq_transcript_uuid", "agent_transcripts", ["agent_session_id", "subpath", "entry_uuid"],
                    unique=True, postgresql_where=sa.text("entry_uuid IS NOT NULL"))


def downgrade() -> None:
    op.drop_table("agent_transcripts")
    op.drop_table("jobs")
    op.drop_table("changes")
    op.drop_table("chat_messages")
    op.drop_table("agent_sessions")

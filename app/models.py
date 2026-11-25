"""Database tables. EVERY row carries client_id, and every query filters by it, so clients never mix."""
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AgentSession(Base):
    """One client connecting one website (one popup -> one repo -> one chat)."""
    __tablename__ = "agent_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[str] = mapped_column(String(128), index=True)
    platform: Mapped[str] = mapped_column(String(32))          # button clicked: vercel, netlify, github...
    provider: Mapped[str] = mapped_column(String(32))          # login used: github (gitlab, bitbucket later)
    status: Mapped[str] = mapped_column(String(16), default="connecting")  # connecting|connected|selected|closed
    provider_user: Mapped[str | None] = mapped_column(String(255))
    token_enc: Mapped[str | None] = mapped_column(Text)        # encrypted access token (never plain)
    repo: Mapped[str | None] = mapped_column(String(255))
    branch: Mapped[str | None] = mapped_column(String(255))
    live_url: Mapped[str | None] = mapped_column(Text)
    agent_session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=uuid.uuid4)
    pending_note: Mapped[str | None] = mapped_column(Text)     # told to the agent on its next turn (e.g. Undo)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # first successful push
    close_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))   # auto-close time (5 min after last push/undo)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (Index("ix_chat_session_id", "session_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_sessions.id", ondelete="CASCADE"))
    client_id: Mapped[str] = mapped_column(String(128))
    who: Mapped[str] = mapped_column(String(16))               # agent|client|step|system
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Change(Base):
    """A commit the agent pushed (what Undo reverts)."""
    __tablename__ = "changes"
    __table_args__ = (Index("ix_changes_session_id", "session_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_sessions.id", ondelete="CASCADE"))
    client_id: Mapped[str] = mapped_column(String(128))
    sha: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text)
    files: Mapped[list] = mapped_column(JSONB, default=list)
    undone: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Job(Base):
    """One queued task. Lets a retried job know it already ran (never push twice)."""
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_sessions.id", ondelete="CASCADE"), index=True)
    client_id: Mapped[str] = mapped_column(String(128))
    type: Mapped[str] = mapped_column(String(32))              # agent_turn|undo
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued|running|done|failed
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentTranscript(Base):
    """The Claude agent's conversation, so ANY worker can resume ANY session (stateless workers)."""
    __tablename__ = "agent_transcripts"
    __table_args__ = (
        Index("ix_transcript_key", "agent_session_id", "subpath", "id"),
        Index("uq_transcript_uuid", "agent_session_id", "subpath", "entry_uuid",
              unique=True, postgresql_where=text("entry_uuid IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_session_id: Mapped[str] = mapped_column(String(64))
    subpath: Mapped[str] = mapped_column(String(255), default="")
    entry_uuid: Mapped[str | None] = mapped_column(String(64))
    entry: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

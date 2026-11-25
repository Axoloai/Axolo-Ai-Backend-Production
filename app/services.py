"""Data helpers shared by the router and the worker. Every lookup is scoped by client_id."""
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import AppError
from app.models import AgentSession, Change, ChatMessage, Job
from app.redis import invalidate_status


def parse_session_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(raw))
    except ValueError as e:
        raise AppError("bad_session_id", "session_id is not valid.", 400) from e


async def get_session(db: AsyncSession, client_id: str, session_id: str | uuid.UUID,
                      *, for_update: bool = False, allow_closed: bool = False) -> AgentSession:
    """The session, ONLY if it belongs to this client (otherwise it looks like it doesn't exist)."""
    sid = session_id if isinstance(session_id, uuid.UUID) else parse_session_id(session_id)
    q = select(AgentSession).where(AgentSession.id == sid, AgentSession.client_id == client_id)
    if for_update:
        q = q.with_for_update()
    s = (await db.execute(q)).scalar_one_or_none()
    if s is None:
        raise AppError("session_not_found", "Session not found.", 404)
    if s.status == "closed" and not allow_closed:
        raise AppError("session_closed", "This session has ended. Start again to make another change.", 409)
    return s


def _minutes() -> int:
    return max(1, round(get_settings().auto_close_seconds / 60))


DEPLOYING_MESSAGE = ("I've added the Team Portal button to your website ✓ It's deploying now. "
                     "Once it's done, please check your website. Thanks!")


def done_message(s: AgentSession, live: bool = True) -> str:
    """live=True: we saw Team Portal on the live website ourselves. Otherwise (AWS Amplify, no address, or not
    online within the wait time) the same plain message for every platform."""
    if live and s.live_url:
        return (f"Done ✓ Team Portal is live on your website: {s.live_url}\n"
                f"You can still click Undo for the next {_minutes()} minutes, then this session closes by itself.")
    return DEPLOYING_MESSAGE


def undone_message(s: AgentSession, live: bool = True) -> str:
    if live and s.live_url:
        head = f"Undone ✓ Your website is back to how it was: {s.live_url}"
    else:
        head = "Undone ✓ Your website goes back to how it was as soon as your host finishes deploying."
    return (f"{head}\nYou can ask for it somewhere else in the next {_minutes()} minutes, "
            "then this session closes by itself.")


def closed_message(s: AgentSession) -> str:
    where = f" Check it here: {s.live_url}" if s.live_url else ""
    return f"Session closed ✓ Your website is updated.{where}"


async def schedule_auto_close(db: AsyncSession, s: AgentSession, pool) -> None:
    """(Re)start the auto-close timer. Each push / undo moves it to 5 minutes from now; an older timer
    that fires early sees the newer close_after and does nothing."""
    delay = get_settings().auto_close_seconds
    s.close_after = datetime.now(UTC) + timedelta(seconds=delay)
    await db.commit()
    await pool.enqueue_job("auto_close", s.client_id, str(s.id),
                           _job_id=f"autoclose-{uuid.uuid4().hex}", _defer_by=delay)


async def close_session(db: AsyncSession, s: AgentSession, pool) -> None:
    """End a session: forget the client's access and the agent's conversation, tell the Axolo backend."""
    from app.agent.session_store import PostgresSessionStore   # local imports: avoid a cycle
    from app.db import get_sessionmaker
    from app.redis import get_redis, repos_key, status_key
    from app.webhooks import build_event, enqueue_event

    s.status, s.token_enc, s.close_after = "closed", None, None
    await db.commit()
    await PostgresSessionStore(get_sessionmaker()).delete(
        {"project_key": "", "session_id": str(s.agent_session_id)})
    await get_redis().delete(repos_key(s.client_id, str(s.id)), status_key(s.client_id, str(s.id)))
    await enqueue_event(pool, build_event("closed", client_id=s.client_id, session_id=str(s.id),
                                          platform=s.platform, repo=s.repo, branch=s.branch, live_url=s.live_url))


async def add_message(db: AsyncSession, s: AgentSession, who: str, text: str) -> None:
    db.add(ChatMessage(session_id=s.id, client_id=s.client_id, who=who, text=text[:8000]))
    await db.commit()
    await invalidate_status(s.client_id, str(s.id))


async def last_change(db: AsyncSession, s: AgentSession) -> Change | None:
    return (await db.execute(
        select(Change).where(Change.session_id == s.id, Change.client_id == s.client_id)
        .order_by(Change.id.desc()).limit(1))).scalar_one_or_none()


def new_job_id(job_type: str) -> str:
    return f"{job_type}-{uuid.uuid4().hex}"


async def create_job(db: AsyncSession, s: AgentSession, job_id: str, job_type: str) -> None:
    db.add(Job(id=job_id, session_id=s.id, client_id=s.client_id, type=job_type))
    await db.commit()


async def start_job(db: AsyncSession, job_id: str, client_id: str) -> Job | None:
    """Mark a job running. Returns None if it already ran (a retried job must never run twice)."""
    res = await db.execute(
        update(Job).where(Job.id == job_id, Job.client_id == client_id, Job.status == "queued")
        .values(status="running").returning(Job))
    await db.commit()
    return res.scalar_one_or_none()


async def finish_job(db: AsyncSession, job_id: str, status: str, error: str | None = None) -> None:
    await db.execute(update(Job).where(Job.id == job_id)
                     .values(status=status, error=(error or None) and error[:2000], finished_at=datetime.now(UTC)))
    await db.commit()

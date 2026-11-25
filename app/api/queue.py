"""Putting work on the queue. The router never does slow work itself: it queues a job and answers at once."""
from arq import ArqRedis, create_pool
from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import AppError
from app.models import AgentSession
from app.redis import busy_key, get_redis, invalidate_status, release_busy
from app.services import create_job, new_job_id

_pool: ArqRedis | None = None


async def get_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
    _pool = None


async def is_busy(s: AgentSession) -> bool:
    return bool(await get_redis().exists(busy_key(s.client_id, str(s.id))))


async def enqueue(db: AsyncSession, s: AgentSession, function: str, job_type: str, *args) -> str:
    """One job at a time per session: take the session's busy lock, save the job, queue it."""
    job_id = new_job_id(job_type)
    client_id, session_id = s.client_id, str(s.id)
    ttl = get_settings().job_timeout_seconds + 120
    if not await get_redis().set(busy_key(client_id, session_id), job_id, nx=True, ex=ttl):
        raise AppError("agent_busy", "The agent is still working.", 409)
    try:
        await create_job(db, s, job_id, job_type)
        pool = await get_pool()
        await pool.enqueue_job(function, job_id, client_id, session_id, *args, _job_id=job_id)
    except Exception:
        await release_busy(get_redis(), client_id, session_id, job_id)
        raise
    await invalidate_status(client_id, session_id)
    return job_id

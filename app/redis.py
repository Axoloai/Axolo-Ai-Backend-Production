"""Redis: one shared async client + every key name in one place (all scoped by client/session)."""
from redis.asyncio import Redis

from app.config import get_settings

_redis: Redis | None = None


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
    _redis = None


# ---- key names ----
def busy_key(client_id: str, session_id: str) -> str:
    """Set while an agent job runs for this session (one job at a time per session)."""
    return f"axolo:client:{client_id}:session:{session_id}:busy"


def status_key(client_id: str, session_id: str) -> str:
    """Cached /status answer (keeps 1000 pollers off PostgreSQL)."""
    return f"axolo:client:{client_id}:session:{session_id}:status"


def repos_key(client_id: str, session_id: str) -> str:
    return f"axolo:client:{client_id}:session:{session_id}:repos"


def branches_key(client_id: str, session_id: str, repo: str) -> str:
    return f"axolo:client:{client_id}:session:{session_id}:branches:{repo}"


def site_candidates_key(client_id: str, session_id: str) -> str:
    """Addresses where the website may be online (not confirmed yet - never shown until confirmed)."""
    return f"axolo:client:{client_id}:session:{session_id}:site_candidates"


def oauth_state_key(state: str) -> str:
    return f"axolo:oauth_state:{state}"


def rate_key(client_id: str, minute: int) -> str:
    return f"axolo:client:{client_id}:rate:{minute}"


CLAUDE_SLOTS_KEY = "axolo:claude_slots"

_RELEASE_IF_OWNER = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


async def release_busy(redis: Redis, client_id: str, session_id: str, job_id: str) -> None:
    """Free the session's busy lock, but only if THIS job holds it (never a newer job's lock)."""
    await redis.eval(_RELEASE_IF_OWNER, 1, busy_key(client_id, session_id), job_id)


async def invalidate_status(client_id: str, session_id: str) -> None:
    await get_redis().delete(status_key(client_id, session_id))

"""arq worker. Run:  arq app.worker.settings.WorkerSettings
Scale by running more worker containers; each runs at most WORKER_MAX_JOBS jobs at once."""
import logging

from arq.connections import RedisSettings

from app.config import get_settings
from app.db import dispose_engine
from app.redis import close_redis
from app.worker.tasks import agent_turn, auto_close, deliver_webhook, undo_change, watch_deploy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_s = get_settings()


async def startup(ctx: dict) -> None:
    _s.work_root.mkdir(parents=True, exist_ok=True)


async def shutdown(ctx: dict) -> None:
    await dispose_engine()
    await close_redis()


class WorkerSettings:
    functions = [agent_turn, undo_change, deliver_webhook, auto_close, watch_deploy]
    redis_settings = RedisSettings.from_dsn(_s.redis_url)
    max_jobs = _s.worker_max_jobs
    job_timeout = _s.job_timeout_seconds
    max_tries = 200          # only our "all Claude slots busy" Retry re-queues a job
    keep_result = 3600
    on_startup = startup
    on_shutdown = shutdown

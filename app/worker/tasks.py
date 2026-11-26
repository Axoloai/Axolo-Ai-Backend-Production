"""Queue jobs (arq). Any worker can run any job: everything it needs is loaded from PostgreSQL by
client_id + session_id, and nothing is kept on the worker after the job ends."""
import asyncio
import json
import logging
import time
import traceback
import uuid
from datetime import UTC, datetime, timedelta

from arq import Retry
from sqlalchemy import update

from app.agent.git_agent import GitAgentTurn, TurnContext, session_dir
from app.agent.git_ops import Git, GitError, rmtree
from app.agent.prompt import find_existing
from app.agent.session_store import PostgresSessionStore
from app.config import get_settings
from app.db import get_sessionmaker
from app.errors import AppError
from app import live_check, site_address
from app.providers import github
from app.models import AgentSession, Change, Job
from app.platforms import PROVIDERS
from app.redis import CLAUDE_SLOTS_KEY, busy_key, invalidate_status, release_busy, site_candidates_key
from app.security import decrypt_token
from app.services import (
    add_message,
    close_session,
    closed_message,
    done_message,
    finish_job,
    get_session,
    schedule_auto_close,
    start_job,
    undone_message,
)
from app.webhooks import build_event, deliver, enqueue_event

log = logging.getLogger("axolo.worker")

# Take one of N Claude slots shared by ALL workers (protects the Claude API rate limit at 1000 users).
# Stale slots (a crashed worker) expire after the job timeout.
_TAKE_SLOT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1] - ARGV[3])
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[2]) then
  redis.call('ZADD', KEYS[1], ARGV[1], ARGV[4])
  return 1
end
return 0
"""


async def _take_claude_slot(redis, job_id: str) -> bool:
    s = get_settings()
    got = await redis.eval(_TAKE_SLOT, 1, CLAUDE_SLOTS_KEY, int(time.time()), s.claude_max_concurrent,
                           s.job_timeout_seconds + 60, job_id)
    return bool(got)


def _event(name: str, s: AgentSession, result: dict | None) -> dict:
    return build_event(name, client_id=s.client_id, session_id=str(s.id), platform=s.platform,
                       repo=s.repo, branch=s.branch, live_url=s.live_url, result=result)


async def deliver_webhook(ctx: dict, event: dict) -> str:
    """Send one event to the Axolo backend; retry with growing waits if it is down."""
    delivered, detail = await deliver(event)
    if delivered:
        return f"delivered ({detail})"
    tries = ctx.get("job_try", 1)
    if tries >= get_settings().webhook_max_tries:
        log.error("webhook %s %s gave up after %s tries: %s", event["event"], event["event_id"], tries, detail)
        return f"gave up ({detail})"
    raise Retry(defer=min(10 * 2 ** (tries - 1), 600))   # 10s, 20s, 40s ... max 10 min


async def auto_close(ctx: dict, client_id: str, session_id: str) -> str:
    """Runs 5 minutes after the last push / undo: closes the session by itself."""
    redis, sm = ctx["redis"], get_sessionmaker()
    async with sm() as db:
        try:
            s = await get_session(db, client_id, session_id, for_update=True)
        except AppError:
            return "skipped: already closed"
        if s.close_after is None or s.close_after > datetime.now(UTC) + timedelta(seconds=1):
            await db.rollback()
            return "skipped: timer was moved"      # a newer push/undo restarted the timer
        if await redis.exists(busy_key(client_id, session_id)):
            await db.rollback()
            raise Retry(defer=30)                    # the agent is working right now: try again shortly
        await close_session(db, s, redis)
        await add_message(db, s, "system", closed_message(s))
    return "closed"


async def _already_ran(db, job_id: str, client_id: str, session_id: str) -> str:
    """A job came back after a worker crash. Never run it twice (it may have pushed already):
    if it was cut off mid-run, mark it failed and tell the client to try again."""
    job = await db.get(Job, job_id)
    if job is not None and job.client_id == client_id and job.status == "running":
        await finish_job(db, job_id, "failed", "worker stopped mid-job")
        try:
            await add_message(db, await get_session(db, client_id, session_id), "system",
                              "Something went wrong. Please send your message again.")
        except Exception:  # noqa: BLE001
            pass
    return "skipped: already ran"


_HAND_OVER = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
  return 1
end
return 0
"""


async def _finish_publish(redis, client_id: str, session_id: str, change_id: int, kind: str, live: bool,
                          url: str | None = None) -> None:
    """The change is online (live=True: we saw it on the live website `url` ourselves - only then is that address
    saved and shown as the client's website). Tell the client and the Axolo backend, start the auto-close timer."""
    async with get_sessionmaker()() as db:
        s = await get_session(db, client_id, session_id, for_update=True)
        if live and url:
            s.live_url = url        # confirmed: Team Portal really shows there
        change = await db.get(Change, change_id)
        if kind == "undo":
            event, text = "undone", undone_message(s, live)
        else:
            first = s.completed_at is None
            if first:
                s.completed_at = datetime.now(UTC)
            event, text = ("completed" if first else "updated"), done_message(s, live)
        await db.commit()
        await add_message(db, s, "system", text)
        await schedule_auto_close(db, s, redis)
    await enqueue_event(redis, _event(event, s, {"sha": change.sha, "summary": change.summary,
                                                 "files": change.files, "live": live}))


async def _snapshots(urls: list[str]) -> dict[str, list[str] | None]:
    """How each candidate address looks now (where the Team Portal link appears, None = could not open)."""
    snaps = await asyncio.gather(*(live_check.snapshot(u) for u in urls), return_exceptions=True)
    return {u: (None if isinstance(sn, BaseException) else sn) for u, sn in zip(urls, snaps)}


async def _candidates(redis, client_id: str, session_id: str, confirmed: str | None, repo_dir=None) -> list[str]:
    """Addresses to check: the confirmed one first, then GitHub's (saved at /select), then the site's own code."""
    raw = await redis.get(site_candidates_key(client_id, session_id))
    saved = json.loads(raw) if raw else []
    code = await asyncio.to_thread(site_address.from_code, repo_dir) if repo_dir is not None else []
    urls = site_address.merge([confirmed], saved, code)
    await redis.set(site_candidates_key(client_id, session_id), json.dumps(urls), ex=7 * 24 * 3600)
    return urls


async def _after_push(redis, client_id: str, session_id: str, owner_job: str, change_id: int, kind: str,
                      expect_link: bool, before: dict[str, list[str] | None]) -> None:
    """Pushed. Deploy watch switched off (settings.deploy_watch_enabled) or AWS Amplify (it tells GitHub nothing):
    finish at once with the plain "deploying now" message. Otherwise, if we have addresses to check, keep the
    session busy and watch them until the change shows on one."""
    if not get_settings().deploy_watch_enabled:
        before = {}
    if before:
        async with get_sessionmaker()() as db:
            if (await get_session(db, client_id, session_id)).platform == "aws_amplify":
                before = {}
    if not before:
        await _finish_publish(redis, client_id, session_id, change_id, kind, live=False)
        return
    s = get_settings()
    watch_id = f"deploy-{uuid.uuid4().hex}"
    # hand the session's busy lock from this job to the watcher (the client waits while the site deploys)
    await redis.eval(_HAND_OVER, 1, busy_key(client_id, session_id), owner_job, watch_id,
                     s.deploy_check_seconds * 4 + 120)
    await redis.enqueue_job("watch_deploy", watch_id, client_id, session_id, change_id, kind, expect_link,
                            before, time.time(), _job_id=watch_id, _defer_by=s.deploy_check_seconds)


async def watch_deploy(ctx: dict, watch_id: str, client_id: str, session_id: str, change_id: int, kind: str,
                       expect_link: bool, before: dict[str, list[str] | None], started: float) -> str:
    """Open every candidate address like a visitor, every few seconds, until the new version is online on one.
    That address becomes the client's website (live_url) - nothing else is ever shown as their website."""
    redis, settings = ctx["redis"], get_settings()
    key = busy_key(client_id, session_id)
    retrying = False
    try:
        before = dict(before or {})
        if kind == "change":
            # Vercel / Netlify make a new deployment for our push: pick up its address from GitHub too
            async with get_sessionmaker()() as db:
                s = await get_session(db, client_id, session_id)
                token, repo = decrypt_token(s.token_enc or ""), s.repo
            try:
                extra = site_address.merge(await github.find_site_addresses(token, repo))
            except Exception:  # noqa: BLE001 - GitHub down: just check the ones we have
                extra = []
            for u in extra:
                if u not in before and len(before) < site_address.MAX_CANDIDATES + 2:
                    before[u] = None
        now = await _snapshots(list(before))
        for url in before:
            # a NEW address (before=None) only counts when the link is really there
            if live_check.deployed(now[url], before[url], expect_link) and (now[url] or before[url] is not None):
                await _finish_publish(redis, client_id, session_id, change_id, kind, live=True, url=url)
                return f"live on {url}"
        if time.time() - started < settings.deploy_wait_seconds:
            retrying = True
            if await redis.get(key) == watch_id:
                await redis.expire(key, settings.deploy_check_seconds * 4 + 120)
            raise Retry(defer=settings.deploy_check_seconds)
        await _finish_publish(redis, client_id, session_id, change_id, kind, live=False)
        return "gave up waiting"
    except Retry:
        raise
    except AppError:
        return "skipped: session closed"
    except Exception:  # noqa: BLE001 - never leave the client waiting
        log.error("watch_deploy %s failed: %s", watch_id, traceback.format_exc())
        try:
            await _finish_publish(redis, client_id, session_id, change_id, kind, live=False)
        except Exception:  # noqa: BLE001
            pass
        return "failed"
    finally:
        if not retrying:
            await release_busy(redis, client_id, session_id, watch_id)
            await invalidate_status(client_id, session_id)


async def agent_turn(ctx: dict, job_id: str, client_id: str, session_id: str, message: str | None = None) -> str:
    """One chat turn: client message in -> agent replies (and maybe publishes) -> chat lines saved."""
    redis, sm, settings = ctx["redis"], get_sessionmaker(), get_settings()

    if not await _take_claude_slot(redis, job_id):
        raise Retry(defer=5)  # all Claude slots busy: wait in the queue, nothing breaks

    try:
        async with sm() as db:
            if await start_job(db, job_id, client_id) is None:
                return await _already_ran(db, job_id, client_id, session_id)
            s = await get_session(db, client_id, session_id, for_update=True)
            note, s.pending_note = s.pending_note, None
            await db.commit()
            provider = PROVIDERS[s.provider]
            token = decrypt_token(s.token_enc or "")
            agent_session_id, repo, branch = str(s.agent_session_id), s.repo, s.branch
            live_url = s.live_url

        async def say(who: str, text: str) -> None:
            async with sm() as db:
                await add_message(db, await get_session(db, client_id, session_id), who, text)

        pushed_change: int | None = None
        before: dict = {}

        async def before_push() -> None:
            # every address where the site may be online (GitHub + its own code), and how each looks BEFORE our
            # first push - to see when the new version is online, and on which address
            if "snaps" not in before and not settings.deploy_watch_enabled:
                before["snaps"] = {}            # deploy watch is off: nothing to look at
            if "snaps" not in before:
                urls = await _candidates(redis, client_id, session_id, live_url,
                                         session_dir(settings, client_id, session_id) / "repo")
                before["snaps"] = await _snapshots(urls)

        async def record_change(sha: str, summary: str, files: list[str]) -> None:
            nonlocal pushed_change
            async with sm() as db:
                sess = await get_session(db, client_id, session_id)
                change = Change(session_id=sess.id, client_id=client_id, sha=sha, summary=summary, files=files)
                db.add(change)
                await db.commit()
                pushed_change = change.id
                await invalidate_status(client_id, session_id)

        turn = GitAgentTurn(TurnContext(
            settings=settings, client_id=client_id, session_id=session_id, agent_session_id=agent_session_id,
            provider=provider, token=token, repo=repo, branch=branch,
            store=PostgresSessionStore(sm), say=say, record_change=record_change, before_push=before_push))
        await turn.run(message, note)

        async with sm() as db:
            await finish_job(db, job_id, "done")
            if turn.gave_up and pushed_change is None:
                # nothing could be added: close now (nothing published). The chat then offers the snippet.
                await close_session(db, await get_session(db, client_id, session_id, for_update=True), redis)
                return "closed: nothing added"
        if pushed_change is not None:
            # watch the live website: "Done ✓ Team Portal is live" only once it really shows there
            await _after_push(redis, client_id, session_id, job_id, pushed_change, "change", True,
                              before.get("snaps") or {})
        return "done"
    except Exception as e:  # noqa: BLE001 - report every failure to the client, never crash the worker
        log.error("agent_turn %s failed: %s", job_id, traceback.format_exc())
        async with sm() as db:
            await finish_job(db, job_id, "failed", repr(e))
            try:
                await add_message(db, await get_session(db, client_id, session_id), "system",
                                  "Something went wrong. Please send your message again.")
            except Exception:  # noqa: BLE001
                pass
        return "failed"
    finally:
        await redis.zrem(CLAUDE_SLOTS_KEY, job_id)
        await release_busy(redis, client_id, session_id, job_id)
        await invalidate_status(client_id, session_id)


async def undo_change(ctx: dict, job_id: str, client_id: str, session_id: str, change_id: int) -> str:
    """Undo (our code, not Claude): revert the agent's commit and push the revert."""
    redis, sm, settings = ctx["redis"], get_sessionmaker(), get_settings()
    base = session_dir(settings, client_id, session_id) / f"undo-{job_id}"
    try:
        async with sm() as db:
            if await start_job(db, job_id, client_id) is None:
                return await _already_ran(db, job_id, client_id, session_id)
            s = await get_session(db, client_id, session_id)
            change = await db.get(Change, change_id)
            if change is None or change.client_id != client_id or change.session_id != s.id or change.undone:
                await finish_job(db, job_id, "failed", "change not found")
                return "failed"
            git = Git(PROVIDERS[s.provider], decrypt_token(s.token_enc or ""), s.repo, s.branch,
                      base / "repo", base / "home")
            sha = change.sha

        (base / "home").mkdir(parents=True, exist_ok=True)
        await git.clone(depth=50)
        if not await git.has_commit(sha):
            await git("fetch", "--deepen", "500", "origin", s.branch, auth=True)
        if not await git.has_commit(sha):
            raise GitError("The change is too far back in the history to undo.")
        await git("-c", "user.name=Axolo Agent", "-c", "user.email=agent@axolo.ai", "revert", "--no-edit", sha)
        expect_link = bool(find_existing(base / "repo"))     # after the undo: is a Team Portal link still there?
        before = await _snapshots([s.live_url]) if s.live_url else {}   # only the confirmed address
        await git.push()

        async with sm() as db:
            await db.execute(update(Change).where(Change.id == change_id, Change.client_id == client_id)
                             .values(undone=True))
            await db.execute(update(AgentSession).where(AgentSession.id == s.id, AgentSession.client_id == client_id)
                             .values(pending_note=f"Note: the client clicked Undo. Your commit {sha[:7]} "
                                                  f"was reverted and pushed, so it is no longer on the site."))
            await db.commit()
            await finish_job(db, job_id, "done")
        await _after_push(redis, client_id, session_id, job_id, change_id, "undo", expect_link, before)
        return "done"
    except Exception as e:  # noqa: BLE001
        log.error("undo %s failed: %s", job_id, traceback.format_exc())
        async with sm() as db:
            await finish_job(db, job_id, "failed", repr(e))
            try:
                await add_message(db, await get_session(db, client_id, session_id), "system",
                                  "Undo did not work. Please try again.")
            except Exception:  # noqa: BLE001
                pass
        return "failed"
    finally:
        rmtree(base)
        await release_busy(redis, client_id, session_id, job_id)
        await invalidate_status(client_id, session_id)

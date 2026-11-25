"""THE one router. Every frontend button for the git agent (GitHub, Vercel, Netlify, AWS Amplify) comes here.

Called server-to-server by the main backend with headers:
    X-Api-Key: <shared key>     X-Client-Id: <the logged-in client>
Only the OAuth callback is opened by the client's browser popup itself (it is protected by a one-time state).
"""
import json
import secrets
import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import queue
from app.api.queue import enqueue, is_busy
from app.api.schemas import ChatIn, ConnectIn, SelectIn, SessionIn
from app.agent.prompt import place_options
from app.config import get_settings
from app.db import get_db
from app.errors import AppError, ok
from app.models import AgentSession, ChatMessage
from app.origins import allowed_origin
from app.platforms import COMING_LATER, PLATFORMS, provider_for
from app.providers import github
from app import site_address
from app.redis import (branches_key, busy_key, get_redis, invalidate_status, oauth_state_key, repos_key,
                       site_candidates_key, status_key)
from app.security import decrypt_token, encrypt_token, require_client
from app.services import add_message, close_session, get_session, last_change

router = APIRouter(prefix="/api/v1")

_STATUS_CHAT_LIMIT = 200
_REPOS_CACHE_SECONDS = 300


# ---------------------------------------------------------------- 1. connect (button clicked)
@router.post("/connect")
async def connect(body: ConnectIn, client_id: str = Depends(require_client), db: AsyncSession = Depends(get_db)):
    platform = body.platform.strip().lower()
    provider = provider_for(platform)
    if provider is None:
        if platform in COMING_LATER:
            raise AppError("not_available", f"{platform} is coming soon.", 400)
        raise AppError("bad_platform", f"Unknown platform. Use one of: {', '.join(PLATFORMS)}.", 400)
    if not get_settings().github_client_id:
        raise AppError("setup_missing", "GitHub login is not configured on the server.", 500)
    origin = allowed_origin(body.frontend_origin)   # the exact company site the popup reports back to
    if origin is None:
        raise AppError("origin_not_allowed", "frontend_origin is not an allowed Axolo site.", 400)

    s = AgentSession(client_id=client_id, platform=platform, provider=provider.name, status="connecting")
    db.add(s)
    await db.commit()

    state = secrets.token_urlsafe(32)
    await get_redis().set(oauth_state_key(state),
                          json.dumps({"client_id": client_id, "session_id": str(s.id), "origin": origin}),
                          ex=get_settings().oauth_state_ttl_seconds)
    return ok({"session_id": str(s.id), "platform": platform, "login": provider.name,
               "popup_url": github.authorize_url(state)})


# ---------------------------------------------------------------- 2. popup comes back from GitHub
@router.get("/oauth/github/callback", response_class=HTMLResponse, include_in_schema=False)
async def github_callback(code: str = "", state: str = "", error: str = "", db: AsyncSession = Depends(get_db)):
    raw = await get_redis().getdel(oauth_state_key(state)) if state else None   # one-time use
    info = json.loads(raw) if raw else None
    origin = (info or {}).get("origin") or allowed_origin(None)                  # the client's company site
    if error:
        return _popup_page("Not connected: access was not given.", {"type": "axolo-git-error", "error": "cancelled"},
                           origin)
    if not code or info is None:
        return _popup_page("Something went wrong. Please try again.", {"type": "axolo-git-error", "error": "invalid"},
                           origin)
    try:
        s = await get_session(db, info["client_id"], info["session_id"])
        token = await github.exchange_code(code)
        user = await github.get_user(token)
    except AppError as e:
        return _popup_page("Could not connect. Please try again.", {"type": "axolo-git-error", "error": e.code},
                           origin)
    s.token_enc, s.provider_user, s.status = encrypt_token(token), user, "connected"
    await db.commit()
    await invalidate_status(s.client_id, str(s.id))
    return _popup_page("Connected ✓ You can close this window.",
                       {"type": "axolo-git-connected", "session_id": str(s.id), "platform": s.platform, "user": user},
                       origin)


def _popup_page(text: str, message: dict, origin: str) -> HTMLResponse:
    origin = json.dumps(origin)
    payload = json.dumps(message).replace("<", "\\u003c")
    safe_text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = f"""<!doctype html><meta charset="utf-8"><title>Axolo</title>
<p style="font-family:system-ui;padding:24px">{safe_text}</p>
<script>
  if (window.opener) {{ window.opener.postMessage({payload}, {origin}); window.close(); }}
</script>"""
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


# ---------------------------------------------------------------- 3. the client's repositories
@router.get("/repos")
async def repos(session_id: str = Query(max_length=64), client_id: str = Depends(require_client),
                db: AsyncSession = Depends(get_db)):
    s = await _connected(db, client_id, session_id)
    r = get_redis()
    cached = await r.get(repos_key(client_id, session_id))
    if cached:
        return ok({"repos": json.loads(cached)})
    items = await github.list_repos(decrypt_token(s.token_enc))
    await r.set(repos_key(client_id, session_id), json.dumps(items), ex=_REPOS_CACHE_SECONDS)
    return ok({"repos": items})


# ---------------------------------------------------------------- 3b. the website's branches
@router.get("/branches")
async def branches(session_id: str = Query(max_length=64), repo: str = Query(min_length=3, max_length=255),
                   client_id: str = Depends(require_client), db: AsyncSession = Depends(get_db)):
    s = await _connected(db, client_id, session_id)
    found = await github.get_repo(decrypt_token(s.token_enc), repo.strip())
    if found is None:
        raise AppError("repo_not_found", "Repository not found, or you cannot push to it.", 404)
    return ok({"branches": await _branches(client_id, session_id, s, found)})


async def _branches(client_id: str, session_id: str, s: AgentSession, repo: dict) -> list[dict]:
    r = get_redis()
    key = branches_key(client_id, session_id, repo["full_name"])
    cached = await r.get(key)
    if cached:
        return json.loads(cached)
    items = await github.list_branches(decrypt_token(s.token_enc), repo["full_name"], repo["default_branch"])
    await r.set(key, json.dumps(items), ex=_REPOS_CACHE_SECONDS)
    return items


# ---------------------------------------------------------------- 4. pick the website (+ branch) -> agent starts
@router.post("/select")
async def select_repo(body: SelectIn, client_id: str = Depends(require_client), db: AsyncSession = Depends(get_db)):
    s = await _connected(db, client_id, body.session_id)
    if await is_busy(s):
        raise AppError("agent_busy", "The agent is still working.", 409)
    token = decrypt_token(s.token_enc)
    repo = await github.get_repo(token, body.repo.strip())
    if repo is None:
        raise AppError("repo_not_found", "Repository not found, or you cannot push to it.", 404)

    branch = (body.branch or "").strip() or repo["default_branch"]   # not sent -> default branch
    if branch != repo["default_branch"]:
        names = {b["name"] for b in await _branches(client_id, body.session_id, s, repo)}
        if branch not in names:
            raise AppError("branch_not_found", f"Branch '{branch}' was not found in {repo['full_name']}.", 404)

    new_site = s.repo != repo["full_name"] or s.branch != branch
    s.repo, s.branch, s.status = repo["full_name"], branch, "selected"
    # Only CANDIDATE addresses, and only from GitHub (deployments, homepage, Pages; the worker adds the ones in the
    # site's code). The website the client typed (body.live_url) is NOT used. live_url is set only once the worker
    # SEES Team Portal appear on one of them after our push; only public addresses are opened.
    if new_site:
        s.agent_session_id = uuid.uuid4()   # new website or branch -> fresh conversation
        s.live_url = None
    found = await github.find_site_addresses(token, repo["full_name"])
    await get_redis().set(site_candidates_key(client_id, str(s.id)),
                          json.dumps(site_address.merge(found)), ex=7 * 24 * 3600)
    await db.commit()

    job_id = await enqueue(db, s, "agent_turn", "agent_turn", None)
    return ok({"site": _site(s), "job_id": job_id})


# ---------------------------------------------------------------- 5. client sends a chat message
@router.post("/chat")
async def chat(body: ChatIn, client_id: str = Depends(require_client), db: AsyncSession = Depends(get_db)):
    s = await _selected(db, client_id, body.session_id)
    text = body.message.strip()
    if not text:
        raise AppError("empty_message", "Message is empty.", 400)
    if await is_busy(s):
        raise AppError("agent_busy", "The agent is still working.", 409)
    await add_message(db, s, "client", text)      # saved first, so it shows before the agent's reply
    job_id = await enqueue(db, s, "agent_turn", "agent_turn", text)
    return ok({"job_id": job_id})


# ---------------------------------------------------------------- 6. status (the frontend polls this)
@router.get("/status")
async def status(session_id: str = Query(max_length=64), after: int = Query(default=0, ge=0),
                 client_id: str = Depends(require_client), db: AsyncSession = Depends(get_db)):
    r = get_redis()
    key = status_key(client_id, session_id)
    cached = await r.get(key)
    if cached:
        data = json.loads(cached)
    else:
        s = await get_session(db, client_id, session_id, allow_closed=True)   # still readable after closing
        rows = (await db.execute(
            select(ChatMessage).where(ChatMessage.session_id == s.id, ChatMessage.client_id == client_id)
            .order_by(ChatMessage.id.desc()).limit(_STATUS_CHAT_LIMIT))).scalars().all()
        change = await last_change(db, s)
        busy = await r.get(busy_key(client_id, session_id))
        data = {
            "session_id": str(s.id),
            "connected": s.status in ("connected", "selected"),
            "platform": s.platform,
            "user": s.provider_user,
            "site": _site(s) if s.repo else None,
            "state": "working" if busy else "idle",
            "deploying": bool(busy and busy.startswith("deploy-")),   # pushed, waiting for the live website
            "completed": s.completed_at is not None,
            "completed_at": s.completed_at and s.completed_at.isoformat(),
            "closed": s.status == "closed",
            "closes_at": s.close_after and s.close_after.isoformat(),
            "chat": [{"id": m.id, "who": m.who, "text": m.text, "at": m.created_at.isoformat(),
                      "options": place_options(m.who, m.text)}
                     for m in reversed(rows)],
            "last_change": change and {"id": change.id, "sha": change.sha[:7], "summary": change.summary,
                                       "undone": change.undone},
        }
        await r.set(key, json.dumps(data), ex=get_settings().status_cache_seconds)
    if after:
        data["chat"] = [m for m in data["chat"] if m["id"] > after]
    return ok(data)


# ---------------------------------------------------------------- 7. undo the last change
@router.post("/undo")
async def undo(body: SessionIn, client_id: str = Depends(require_client), db: AsyncSession = Depends(get_db)):
    s = await _selected(db, client_id, body.session_id)
    change = await last_change(db, s)
    if change is None or change.undone:
        raise AppError("nothing_to_undo", "Nothing to undo.", 400)
    job_id = await enqueue(db, s, "undo_change", "undo", change.id)
    return ok({"job_id": job_id})


# ---------------------------------------------------------------- 8. disconnect
@router.post("/disconnect")
async def disconnect(body: SessionIn, client_id: str = Depends(require_client), db: AsyncSession = Depends(get_db)):
    s = await get_session(db, client_id, body.session_id)
    await close_session(db, s, await queue.get_pool())   # forget access + conversation, webhook "closed"
    return ok(None)


# ---------------------------------------------------------------- helpers
def _site(s: AgentSession) -> dict:
    return {"repo": s.repo, "branch": s.branch, "live_url": s.live_url}


async def _connected(db: AsyncSession, client_id: str, session_id: str) -> AgentSession:
    s = await get_session(db, client_id, session_id)
    if s.status not in ("connected", "selected") or not s.token_enc:
        raise AppError("not_connected", "Not connected yet. Finish the login popup first.", 409)
    return s


async def _selected(db: AsyncSession, client_id: str, session_id: str) -> AgentSession:
    s = await _connected(db, client_id, session_id)
    if s.status != "selected" or not s.repo:
        raise AppError("no_website", "Pick your website first.", 409)
    return s

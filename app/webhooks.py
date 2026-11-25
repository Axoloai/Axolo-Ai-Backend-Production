"""Tell the Axolo backend when a client's session changes (webhook).

Events:
  completed  - the agent pushed the Team Portal link for the first time in this session
  updated    - the agent pushed again (moved the link)
  undone     - the client clicked Undo
  closed     - the client disconnected

Delivered by the queue (retries with backoff), so a slow or down Axolo backend never blocks the agent.
Signed with the shared API key so the Axolo backend can trust it:
  X-Axolo-Signature = hex(HMAC-SHA256(API_KEY, f"{X-Axolo-Timestamp}.{raw body}"))
Each event has a unique event_id: the Axolo backend should ignore an event_id it has already handled.
"""
import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime

import httpx

from app.config import get_settings

EVENTS = {"completed", "updated", "undone", "closed"}


def build_event(event: str, *, client_id: str, session_id: str, platform: str, repo: str | None,
                branch: str | None, live_url: str | None, result: dict | None = None) -> dict:
    assert event in EVENTS
    return {
        "event_id": uuid.uuid4().hex,
        "event": event,
        "client_id": client_id,
        "session_id": session_id,
        "platform": platform,
        "site": {"repo": repo, "branch": branch, "live_url": live_url},
        "result": result,
        "at": datetime.now(UTC).isoformat(),
    }


def sign(body: bytes, timestamp: str, secret: str) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


async def deliver(event: dict) -> tuple[bool, str]:
    """POST one event. Returns (delivered, detail). 2xx = delivered."""
    s = get_settings()
    if not s.axolo_webhook_url:
        return True, "no webhook url configured"
    body = json.dumps(event, separators=(",", ":")).encode()
    ts = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "AxoloGitAgent/1.0",
        "X-Axolo-Event": event["event"],
        "X-Axolo-Event-Id": event["event_id"],
        "X-Axolo-Timestamp": ts,
        "X-Axolo-Signature": sign(body, ts, s.api_key.get_secret_value()),
    }
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(s.axolo_webhook_url, content=body, headers=headers)
    except httpx.HTTPError as e:
        return False, type(e).__name__
    return 200 <= r.status_code < 300, f"HTTP {r.status_code}"


async def enqueue_event(pool, event: dict) -> None:
    """Queue the event for delivery (pool = arq pool: the API's, or the worker's ctx['redis'])."""
    await pool.enqueue_job("deliver_webhook", event, _job_id=f"webhook-{event['event_id']}")

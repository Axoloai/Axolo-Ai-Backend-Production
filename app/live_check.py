"""Look at the client's LIVE website like a visitor would, to know when the host has finished deploying.

snapshot(url) opens the page (and its own script files, for sites built with React/Vue/...) and returns every
place the Team Portal link appears, with a little text around it. After a push we compare snapshots:
  - the snapshot changed since before the push  -> the new version is online
  - the link is there (add / move) or gone (undo) -> it is the version we expect
Only public http(s) addresses are opened (never the server's own network).
"""
import asyncio
import ipaddress
import re
import socket
import time
from urllib.parse import urljoin, urlparse

import httpx

from app.agent.prompt import LINK

_MAX_BYTES = 3_000_000          # per file
_MAX_SCRIPTS = 20
_AROUND = 150                   # characters kept on each side of the link
_SCRIPT_SRC = re.compile(r"""<script\b[^>]*\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_HEADERS = {"User-Agent": "AxoloGitAgent/1.0", "Cache-Control": "no-cache", "Pragma": "no-cache"}


def valid_site_url(url: str | None) -> bool:
    p = urlparse(url or "")
    return p.scheme in ("http", "https") and bool(p.hostname)


async def _public(host: str) -> bool:
    """True only if every address of the host is a public internet address."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


async def _get(c: httpx.AsyncClient, url: str) -> tuple[str | None, str]:
    """GET a public page, following up to 5 redirects (each one checked). Returns (text or None, final url)."""
    for _ in range(6):
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname or not await _public(p.hostname):
            return None, url
        async with c.stream("GET", url) as r:
            if r.is_redirect and r.headers.get("location"):
                url = urljoin(url, r.headers["location"])
                continue
            if r.status_code != 200:
                return None, url
            body = bytearray()
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) > _MAX_BYTES:
                    break
            return body.decode(r.encoding or "utf-8", errors="ignore"), url
    return None, url


async def snapshot(url: str, transport: httpx.AsyncBaseTransport | None = None) -> list[str] | None:
    """Every place the Team Portal link appears on the live site (sorted). None if the site can't be opened."""
    if not valid_site_url(url):
        return None
    bust = f"{'&' if '?' in url else '?'}axolo_check={int(time.time() * 1000)}"   # skip the CDN cache
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, headers=_HEADERS, transport=transport) as c:
            html, final = await _get(c, url + bust)
            if html is None:
                return None
            texts = [html]
            host = urlparse(final).hostname
            for src in _SCRIPT_SRC.findall(html)[:_MAX_SCRIPTS]:
                full = urljoin(final, src)
                if urlparse(full).hostname == host:           # the site's own scripts only
                    text, _ = await _get(c, full)
                    if text:
                        texts.append(text)
    except httpx.HTTPError:
        return None
    return sorted(t[max(0, m.start() - _AROUND): m.end() + _AROUND]
                  for t in texts for m in re.finditer(re.escape(LINK), t))


def deployed(now: list[str] | None, before: list[str] | None, expect_link: bool) -> bool:
    """Is the new version online? (changed since before the push AND the link is there / gone as expected)"""
    if now is None:
        return False
    if bool(now) != expect_link:
        return False
    return before is None or now != before

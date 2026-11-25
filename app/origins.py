"""Which company websites may receive the login popup's result.

The browser's postMessage can only target ONE exact origin, never a pattern. So the Axolo backend tells us the
client's exact site in /connect (e.g. "https://acme.axolo.ai"); we check it against FRONTEND_ORIGIN_PATTERNS
(e.g. "https://*.axolo.ai") and the popup reports back to exactly that site - never to anyone else.
"""
import re
from urllib.parse import urlsplit

from app.config import get_settings

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"


def _pattern_to_regex(pattern: str) -> re.Pattern:
    parts = urlsplit(pattern.strip().lower())
    host = parts.hostname or ""
    host_rx = re.escape(host).replace(r"\*", rf"{_LABEL}(?:\.{_LABEL})*")
    port_rx = f":{parts.port}" if parts.port else ""
    return re.compile(rf"^{re.escape(parts.scheme)}://{host_rx}{port_rx}$")


def normalize_origin(origin: str) -> str | None:
    """'https://Acme.Axolo.ai/' -> 'https://acme.axolo.ai'. None if it is not a plain origin."""
    try:
        parts = urlsplit(origin.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.path not in ("", "/") \
            or parts.query or parts.fragment or parts.username or parts.password:
        return None
    return f"{parts.scheme}://{parts.hostname.lower()}" + (f":{port}" if port else "")


def allowed_origin(origin: str | None) -> str | None:
    """The exact origin to send the popup result to, or None if this site is not allowed."""
    s = get_settings()
    if not origin:
        return normalize_origin(s.frontend_origin)
    o = normalize_origin(origin)
    if o is None:
        return None
    if o == normalize_origin(s.frontend_origin):
        return o
    patterns = [p for p in s.frontend_origin_patterns.split(",") if p.strip()]
    return o if any(_pattern_to_regex(p).match(o) for p in patterns) else None

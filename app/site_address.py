"""Where is the client's website online? Candidate addresses, ONLY from the GitHub access (never from what the
client typed elsewhere). A candidate is never shown to anyone until the worker opens it and sees the Team Portal
link there after our push (see watch_deploy)."""
import json
import re
from pathlib import Path
from urllib.parse import urlparse

MAX_CANDIDATES = 6
_MAX_HTML_FILES = 40
_MAX_READ = 300_000
_SKIP_DIRS = {"node_modules", ".git", ".next", "dist", "build", "out", ".cache", "vendor", "coverage"}
# never the website of the client
_SKIP_HOSTS = re.compile(r"(^|\.)(localhost|example\.(com|org|net)|axolo\.ai|github\.com|githubusercontent\.com|"
                         r"w3\.org|schema\.org|googleapis\.com|gstatic\.com|cloudflare\.com|jsdelivr\.net|"
                         r"unpkg\.com|facebook\.com|twitter\.com|x\.com|instagram\.com|linkedin\.com|"
                         r"youtube\.com|google\.com)$", re.IGNORECASE)
_CANONICAL = re.compile(r"""<link\b[^>]*\brel\s*=\s*["']canonical["'][^>]*>""", re.IGNORECASE)
_OG_URL = re.compile(r"""<meta\b[^>]*\bproperty\s*=\s*["']og:url["'][^>]*>""", re.IGNORECASE)
_HREF = re.compile(r"""\bhref\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_CONTENT = re.compile(r"""\bcontent\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_SITEMAP_LINE = re.compile(r"^\s*sitemap\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE)


def origin(url: str | None) -> str | None:
    """'https://www.acme.com/about?x' -> 'https://www.acme.com' (None if it is not a usable public site)."""
    url = (url or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or "." not in host or _SKIP_HOSTS.search(host):
        return None
    port = f":{p.port}" if p.port and p.port not in (80, 443) else ""
    return f"{p.scheme}://{host}{port}"


def merge(*lists: list[str | None]) -> list[str]:
    """Join candidate lists in order of trust, no duplicates, at most MAX_CANDIDATES."""
    out: list[str] = []
    for lst in lists:
        for u in lst or []:
            o = origin(u)
            if o and o not in out:
                out.append(o)
    return out[:MAX_CANDIDATES]


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")[:_MAX_READ]
    except OSError:
        return ""


def _files(root: Path, names: set[str] | None = None, suffix: str | None = None, limit: int = 50) -> list[Path]:
    found: list[Path] = []
    stack = [root]
    while stack and len(found) < limit:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.is_symlink():
                continue
            if e.is_dir():
                if e.name not in _SKIP_DIRS and not e.name.startswith("."):
                    stack.append(e)
            elif (names and e.name in names) or (suffix and e.name.endswith(suffix)):
                found.append(e)
    return found


def from_code(repo_dir: Path) -> list[str]:
    """Website addresses the site writes in its own files: CNAME, package.json homepage,
    <link rel=canonical>, <meta property=og:url>, sitemap.xml, robots.txt."""
    urls: list[str] = []
    for f in _files(repo_dir, names={"CNAME"}, limit=5):
        line = _read(f).strip().splitlines()[:1]
        urls += line
    for f in _files(repo_dir, names={"package.json"}, limit=10):
        try:
            home = json.loads(_read(f)).get("homepage")
        except (ValueError, AttributeError):
            home = None
        if isinstance(home, str):
            urls.append(home)
    for f in _files(repo_dir, suffix=".html", limit=_MAX_HTML_FILES):
        text = _read(f)
        for tag in _CANONICAL.findall(text):
            urls += _HREF.findall(tag)[:1]
        for tag in _OG_URL.findall(text):
            urls += _CONTENT.findall(tag)[:1]
    for f in _files(repo_dir, names={"sitemap.xml"}, limit=5):
        urls += _LOC.findall(_read(f))[:1]
    for f in _files(repo_dir, names={"robots.txt"}, limit=5):
        urls += _SITEMAP_LINE.findall(_read(f))[:1]
    return merge([u for u in urls if u.startswith(("http://", "https://")) or "/" not in u])

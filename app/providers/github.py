"""GitHub: login popup (OAuth), token exchange, repositories, live site link. Async (httpx)."""
import urllib.parse

import httpx

from app.config import get_settings
from app.errors import AppError

API = "https://api.github.com"
USER_AGENT = "AxoloAgent/1.0"
MAX_REPOS = 300


def authorize_url(state: str) -> str:
    s = get_settings()
    return "https://github.com/login/oauth/authorize?" + urllib.parse.urlencode({
        "client_id": s.github_client_id,
        "redirect_uri": s.github_callback_url,
        "scope": "repo",            # read + push the client's repositories
        "state": state,
        "allow_signup": "false",
    })


def _client(token: str | None = None) -> httpx.AsyncClient:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT,
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.AsyncClient(base_url=API, headers=headers, timeout=30)


def _raise(r: httpx.Response) -> None:
    if r.status_code == 401:
        raise AppError("provider_auth", "GitHub access expired or was removed. Please connect again.", 401)
    if r.status_code >= 400:
        raise AppError("provider_error", f"GitHub said HTTP {r.status_code}.", 502)


async def exchange_code(code: str) -> str:
    s = get_settings()
    async with httpx.AsyncClient(timeout=30, headers={"Accept": "application/json", "User-Agent": USER_AGENT}) as c:
        r = await c.post("https://github.com/login/oauth/access_token", data={
            "client_id": s.github_client_id,
            "client_secret": s.github_client_secret.get_secret_value(),
            "code": code,
            "redirect_uri": s.github_callback_url,
        })
    _raise(r)
    out = r.json()
    if "access_token" not in out:
        raise AppError("provider_error", out.get("error_description") or "GitHub returned no token.", 502)
    return out["access_token"]


async def get_user(token: str) -> str:
    async with _client(token) as c:
        r = await c.get("/user")
    _raise(r)
    return r.json()["login"]


async def list_repos(token: str) -> list[dict]:
    """Repositories the client can push to, most recently updated first."""
    repos: list[dict] = []
    url: str | None = "/user/repos?per_page=100&sort=updated&affiliation=owner,collaborator,organization_member"
    async with _client(token) as c:
        while url and len(repos) < MAX_REPOS:
            r = await c.get(url)
            _raise(r)
            for p in r.json():
                if (p.get("permissions") or {}).get("push") and not p.get("archived"):
                    repos.append({"full_name": p["full_name"], "private": p["private"],
                                  "default_branch": p["default_branch"]})
            url = r.links.get("next", {}).get("url")
    return repos[:MAX_REPOS]


async def get_repo(token: str, full_name: str) -> dict | None:
    """The repo, only if the client can push to it."""
    if full_name.count("/") != 1:
        return None
    async with _client(token) as c:
        r = await c.get(f"/repos/{full_name}")
    if r.status_code == 404:
        return None
    _raise(r)
    p = r.json()
    if not (p.get("permissions") or {}).get("push"):
        return None
    return {"full_name": p["full_name"], "private": p["private"], "default_branch": p["default_branch"]}


MAX_BRANCHES = 300


async def list_branches(token: str, full_name: str, default_branch: str) -> list[dict]:
    """The repo's branches, default first: [{"name": "main", "default": true}, ...]."""
    names: list[str] = []
    url: str | None = f"/repos/{full_name}/branches?per_page=100"
    async with _client(token) as c:
        while url and len(names) < MAX_BRANCHES:
            r = await c.get(url)
            if r.status_code == 404:
                return []
            _raise(r)
            names += [b["name"] for b in r.json()]
            url = r.links.get("next", {}).get("url")
    names = names[:MAX_BRANCHES]
    ordered = ([default_branch] if default_branch in names else []) + sorted(n for n in names if n != default_branch)
    return [{"name": n, "default": n == default_branch} for n in ordered]


async def find_live_url(token: str, full_name: str) -> str | None:
    """Vercel / Netlify / Amplify report deployments to GitHub; the Production one has the live link."""
    try:
        async with _client(token) as c:
            for query in ("?environment=Production&per_page=5", "?per_page=5"):
                r = await c.get(f"/repos/{full_name}/deployments{query}")
                if r.status_code != 200:
                    return None
                for d in r.json():
                    sr = await c.get(d["statuses_url"])
                    if sr.status_code != 200:
                        continue
                    for st in sr.json():
                        if st.get("state") == "success" and st.get("environment_url"):
                            return st["environment_url"]
    except httpx.HTTPError:
        return None
    return None


async def find_site_addresses(token: str, full_name: str) -> list[str]:
    """Where the site may be online, from GitHub only: the host's deployment (Vercel / Netlify report it),
    the repo's homepage field and its GitHub Pages address. Only candidates - checked on the live site later."""
    found: list[str] = []
    deploy = await find_live_url(token, full_name)
    if deploy:
        found.append(deploy)
    try:
        async with _client(token) as c:
            r = await c.get(f"/repos/{full_name}")
            if r.status_code == 200 and isinstance(r.json().get("homepage"), str):
                found.append(r.json()["homepage"])
            r = await c.get(f"/repos/{full_name}/pages")
            if r.status_code == 200 and isinstance(r.json().get("html_url"), str):
                found.append(r.json()["html_url"])
    except httpx.HTTPError:
        pass
    return found

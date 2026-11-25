"""Async git + build commands. The client's token is passed as a header on each call only:
it is never written into the repo, its config, or the logs."""
import asyncio
import base64
import os
import shutil
import stat
from pathlib import Path

from app.platforms import Provider

_SAFE_ENV_KEYS = ("PATH", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP", "LANG")


def safe_env(home: Path) -> dict[str, str]:
    """A minimal environment for git / npm: none of OUR secrets (API keys, DB URL) reach client code."""
    env = {k: os.environ[k] for k in _SAFE_ENV_KEYS if k in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home), GIT_TERMINAL_PROMPT="0", CI="1",
               npm_config_update_notifier="false", npm_config_fund="false", npm_config_audit="false")
    return env


async def run(cmd: list[str], cwd: Path, env: dict[str, str], timeout: float,
              secret: str | None = None) -> tuple[int, str]:
    """Run a command; returns (exit code, output). Kills it on timeout. Hides the secret in the output."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"Timed out after {int(timeout)}s."
    text = out.decode("utf-8", errors="replace")
    if secret:
        text = text.replace(secret, "***")
    return proc.returncode or 0, text


def rmtree(path: Path) -> None:
    def onerror(func, p, _exc):  # git marks some files read-only on Windows
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if path.exists():
        shutil.rmtree(path, onerror=onerror)


class GitError(RuntimeError):
    pass


class Git:
    """git for ONE repo copy, authenticated with the client's token per command."""

    def __init__(self, provider: Provider, token: str, repo: str, branch: str, repo_dir: Path, home: Path):
        self.provider, self.token, self.repo, self.branch = provider, token, repo, branch
        self.dir, self.home = repo_dir, home
        self.env = safe_env(home)

    def _auth(self) -> list[str]:
        basic = base64.b64encode(f"{self.provider.git_user}:{self.token}".encode()).decode()
        return ["-c", f"http.extraHeader=Authorization: Basic {basic}"]

    async def __call__(self, *args: str, auth: bool = False, check: bool = True, timeout: float = 300) -> str:
        cmd = ["git", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper="]
        cmd += self._auth() if auth else []
        cmd += list(args)
        cwd = self.dir if self.dir.exists() and args[0] != "clone" else self.dir.parent
        code, out = await run(cmd, cwd, self.env, timeout, secret=self.token)
        if check and code != 0:
            raise GitError(out[-1500:])
        return out

    async def clone(self, depth: int = 30) -> None:
        """Fresh copy of the client's repo (a new copy for every job: stateless workers)."""
        rmtree(self.dir)
        self.dir.parent.mkdir(parents=True, exist_ok=True)
        await self("clone", "--depth", str(depth), "--single-branch", "--branch", self.branch,
                   f"https://{self.provider.host}/{self.repo}.git", str(self.dir), auth=True)

    async def commit(self, message: str, files: list[str]) -> str:
        await self("add", "--", *files)
        await self("-c", "user.name=Axolo Agent", "-c", "user.email=agent@axolo.ai", "commit", "-m", message)
        return (await self("rev-parse", "HEAD")).strip()

    async def push(self) -> None:
        await self("push", "origin", f"HEAD:{self.branch}", auth=True)

    async def has_commit(self, sha: str) -> bool:
        out = await self("cat-file", "-t", sha, check=False)
        return out.strip() == "commit"

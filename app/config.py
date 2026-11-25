"""All settings come from environment variables / .env (never hardcoded)."""
import tempfile
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- service ---
    env: str = "dev"
    # Shared secret with the main backend (it does the client login and calls us server-to-server).
    api_key: SecretStr
    # Public URL of THIS backend (used to build the OAuth callback URL).
    public_base_url: str = "http://localhost:8000"
    # Origin of the website that opens the popup (the popup reports back to it with postMessage).
    # Default, used when /connect does not say which company site the client is on.
    frontend_origin: str = "http://localhost:3000"
    # Company sites allowed to open the popup (comma separated, * = one or more subdomain labels).
    frontend_origin_patterns: str = "http://*.localhost:3000,https://*.axolo.ai"
    # Axolo backend URL we notify when a session is completed / updated / undone / closed (empty = off).
    axolo_webhook_url: str = ""
    webhook_max_tries: int = 8

    # --- PostgreSQL ---
    database_url: str = "postgresql+asyncpg://axolo:axolo@localhost:55432/axolo"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_use_null_pool: bool = False   # True on AWS Lambda (each invocation opens its own connection)
    db_pgbouncer: bool = False       # True behind PgBouncer / RDS Proxy in transaction mode

    # --- Redis (cache, locks, queue) ---
    redis_url: str = "redis://localhost:56379/0"

    # --- security ---
    token_encryption_key: SecretStr   # Fernet key: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    rate_limit_per_minute: int = 120  # per client
    oauth_state_ttl_seconds: int = 600

    # --- Claude ---
    anthropic_api_key: SecretStr
    claude_model: str
    agent_max_turns: int = 60
    claude_max_concurrent: int = 20   # across ALL workers (protects the Claude API rate limit)

    # --- GitHub OAuth app ---
    github_client_id: str = ""
    github_client_secret: SecretStr = SecretStr("")

    # --- worker ---
    work_root: Path = Path(tempfile.gettempdir()) / "axolo-work"
    worker_max_jobs: int = 4            # jobs at once per worker container
    job_timeout_seconds: int = 1200
    build_timeout_seconds: int = 600
    status_cache_seconds: int = 2
    # After the agent publishes, the session closes by itself after this long (Undo works until then).
    auto_close_seconds: int = 600
    # After a push we open the live website every N seconds until the change shows there (max wait).
    deploy_check_seconds: int = 15
    deploy_wait_seconds: int = 900
    # OFF for now (decided 2026-11-26): after a push we never watch the deploy - every platform gets the plain
    # "It's deploying now" message at once. Turn ON (DEPLOY_WATCH_ENABLED=true) to bring back "Done ✓ … live" +
    # View my site for Vercel / Netlify / GitHub (AWS Amplify is never watched).
    deploy_watch_enabled: bool = False

    @property
    def github_callback_url(self) -> str:
        return self.public_base_url.rstrip("/") + "/api/v1/oauth/github/callback"


@lru_cache
def get_settings() -> Settings:
    return Settings()

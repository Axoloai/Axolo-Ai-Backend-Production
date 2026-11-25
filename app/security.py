"""Who is calling (API key from the main backend + client_id), token encryption, rate limit."""
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Header

from app.config import get_settings
from app.errors import AppError
from app.redis import get_redis, rate_key

_CLIENT_ID_MAX = 128


async def require_client(
    x_api_key: str = Header(default=""),
    x_client_id: str = Header(default=""),
) -> str:
    """Every call comes from the main backend (login already done there).
    It proves itself with the shared API key and tells us which client this is."""
    expected = get_settings().api_key.get_secret_value()
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise AppError("unauthorized", "Invalid API key.", 401)
    client_id = x_client_id.strip()
    if not client_id or len(client_id) > _CLIENT_ID_MAX:
        raise AppError("bad_client_id", "X-Client-Id header is missing or too long.", 400)
    await check_rate_limit(client_id)
    return client_id


async def check_rate_limit(client_id: str) -> None:
    """Fixed one-minute window per client, so one busy client cannot slow everyone down."""
    limit = get_settings().rate_limit_per_minute
    key = rate_key(client_id, int(time.time() // 60))
    r = get_redis()
    count = await r.incr(key)
    if count == 1:
        await r.expire(key, 70)
    if count > limit:
        raise AppError("rate_limited", "Too many requests. Please wait a moment.", 429)


def _fernet() -> Fernet:
    return Fernet(get_settings().token_encryption_key.get_secret_value().encode())


def encrypt_token(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def decrypt_token(token_enc: str) -> str:
    try:
        return _fernet().decrypt(token_enc.encode()).decode()
    except InvalidToken as e:
        raise AppError("token_invalid", "Stored access could not be read. Please connect again.", 409) from e

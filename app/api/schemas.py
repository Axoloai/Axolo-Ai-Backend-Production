"""Request bodies the main backend sends."""
from pydantic import BaseModel, Field


class ConnectIn(BaseModel):
    platform: str = Field(min_length=1, max_length=32)
    # The client's exact company site, e.g. "https://acme.axolo.ai" (the popup reports back only to it).
    frontend_origin: str | None = Field(default=None, max_length=255)


class SessionIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)


class SelectIn(SessionIn):
    repo: str = Field(min_length=3, max_length=255)
    # Optional: the branch to change. Not sent -> the repo's default branch.
    branch: str | None = Field(default=None, min_length=1, max_length=255)
    # Accepted for old callers but IGNORED: the website the client typed is never used (see /select).
    live_url: str | None = Field(default=None, min_length=8, max_length=500)


class ChatIn(SessionIn):
    message: str = Field(min_length=1, max_length=2000)

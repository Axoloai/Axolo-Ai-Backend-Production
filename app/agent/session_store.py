"""Keeps the Claude agent's conversation in PostgreSQL (Claude Agent SDK SessionStore).

The next chat message can run on ANY worker: the SDK loads the conversation from here and resumes it.
Keyed by the agent session id (a UUID unique to one client session), so clients never mix.
"""
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AgentTranscript


class PostgresSessionStore:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]):
        self._sm = sessionmaker

    async def append(self, key: dict, entries: list[dict[str, Any]]) -> None:
        if not entries:
            return
        sid, sub = key["session_id"], key.get("subpath", "")
        rows = [{"agent_session_id": sid, "subpath": sub, "entry_uuid": e.get("uuid"), "entry": e} for e in entries]
        stmt = insert(AgentTranscript).values(rows).on_conflict_do_nothing(
            index_elements=["agent_session_id", "subpath", "entry_uuid"],
            index_where=AgentTranscript.entry_uuid.is_not(None))
        async with self._sm() as db:
            await db.execute(stmt)
            await db.commit()

    async def load(self, key: dict) -> list[dict[str, Any]] | None:
        sid, sub = key["session_id"], key.get("subpath", "")
        async with self._sm() as db:
            res = await db.execute(
                select(AgentTranscript.entry)
                .where(AgentTranscript.agent_session_id == sid, AgentTranscript.subpath == sub)
                .order_by(AgentTranscript.id))
            entries = list(res.scalars())
        return entries or None

    async def list_subkeys(self, key: dict) -> list[str]:
        async with self._sm() as db:
            res = await db.execute(
                select(AgentTranscript.subpath).distinct()
                .where(AgentTranscript.agent_session_id == key["session_id"], AgentTranscript.subpath != ""))
            return list(res.scalars())

    async def delete(self, key: dict) -> None:
        q = delete(AgentTranscript).where(AgentTranscript.agent_session_id == key["session_id"])
        if key.get("subpath"):
            q = q.where(AgentTranscript.subpath == key["subpath"])
        async with self._sm() as db:
            await db.execute(q)
            await db.commit()

    async def exists(self, agent_session_id: str) -> bool:
        async with self._sm() as db:
            res = await db.execute(
                select(AgentTranscript.id).where(AgentTranscript.agent_session_id == agent_session_id).limit(1))
            return res.first() is not None

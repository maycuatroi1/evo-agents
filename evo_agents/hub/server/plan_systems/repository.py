"""What a dispatch of a plan to a system reads and writes of the tables: the handle of the system's entry agent, the
run a message woke it with, the trace that run opened, and the plan that trace takes."""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import system_context, tables


class DispatchStore:
    async def handle(self, conn: AsyncConnection, profile_id: int) -> str | None:
        ap = tables.agent_profiles
        return (await conn.execute(select(ap.c.handle).where(ap.c.id == profile_id))).scalar_one_or_none()

    async def mention(self, conn: AsyncConnection, message_id: int, profile_id: int | None):
        """The mention of agent ``profile_id`` by message ``message_id``: the run it took and the hub's refusal."""
        m = tables.chat_mentions
        query = select(m.c.run_id, m.c.refusal).where(m.c.message_id == message_id, m.c.profile_id == profile_id)
        return (await conn.execute(query)).first()

    async def invoked_trace(self, conn: AsyncConnection, run_id: int):
        """The trace run ``run_id`` was invoked in, with its system."""
        s, t = tables.system_spans, tables.system_traces
        query = (
            select(t.c.id, t.c.system_id)
            .join_from(s, t, t.c.id == s.c.trace_id)
            .where(s.c.run_id == run_id, s.c.name == system_context.INVOKE)
        )
        return (await conn.execute(query)).first()

    async def take_plan(self, conn: AsyncConnection, trace_id: int, plan_id: str, plan_revision: int) -> int:
        """Make trace ``trace_id`` run revision ``plan_revision`` of plan ``plan_id``; the system revision it runs."""
        t = tables.system_traces
        values = {"plan_id": plan_id, "plan_revision": plan_revision}
        query = update(t).values(**values).where(t.c.id == trace_id).returning(t.c.system_revision)
        return (await conn.execute(query)).scalar_one()


STORE = DispatchStore()

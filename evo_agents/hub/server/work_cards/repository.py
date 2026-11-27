"""The columns of system_traces a work card reads and writes (``card_message_id``, ``card_state``, the work's fields
and its mark of handled, 0235 and 0270), the traces a run is in and the role a member holds on a trace's project."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import system_context, tables


class CardStore:
    async def set_card(self, conn: AsyncConnection, trace_id: int, message_id: int, state: str) -> None:
        """Name ``message_id`` the card's message of trace ``trace_id``, showing what ``state`` keys."""
        t = tables.system_traces
        await conn.execute(update(t).values(card_message_id=message_id, card_state=state).where(t.c.id == trace_id))

    async def card(self, conn: AsyncConnection, trace_id: int):
        """The card's message of trace ``trace_id`` (``card_message_id``, ``card_state`` and its ``chat_id``); None
        for a trace with no card."""
        t, m = tables.system_traces, tables.chat_messages
        query = (
            select(t.c.card_message_id, t.c.card_state, m.c.chat_id)
            .join(m, m.c.id == t.c.card_message_id)
            .where(t.c.id == trace_id)
        )
        return (await conn.execute(query)).one_or_none()

    async def set_state(self, conn: AsyncConnection, trace_id: int, state: str) -> None:
        t = tables.system_traces
        await conn.execute(update(t).values(card_state=state).where(t.c.id == trace_id))

    async def carded_traces(self, conn: AsyncConnection, run_id: int) -> list[int]:
        """The traces with a card that run ``run_id`` is a span of, or an Orchestrator's run on a thread of."""
        s, o, t = tables.system_spans, tables.orchestrator_threads, tables.system_traces
        spans = select(s.c.trace_id).where(s.c.run_id == run_id, s.c.name == system_context.INVOKE)
        threads = select(o.c.trace_id).where(o.c.orchestrator_run_id == run_id, o.c.trace_id.is_not(None))
        query = select(t.c.id).where(t.c.id.in_(spans.union(threads)), t.c.card_message_id.is_not(None))
        return list((await conn.execute(query)).scalars())

    async def decision_run(self, conn: AsyncConnection, decision_id: int) -> int | None:
        d = tables.decisions
        return (await conn.execute(select(d.c.run_id).where(d.c.id == decision_id))).scalar_one_or_none()

    async def set_work(self, conn: AsyncConnection, trace_id: int, values: dict) -> None:
        """Write ``values``, columns of system_traces, on trace ``trace_id``."""
        t = tables.system_traces
        await conn.execute(update(t).values(**values).where(t.c.id == trace_id))

    async def project_role(self, conn: AsyncConnection, project_id: int, user_id: int) -> tuple[str, str | None]:
        """(the name of project ``project_id``, the role member ``user_id`` holds on it, None for no grant)."""
        g, p = tables.grants, tables.projects
        query = (
            select(p.c.name, g.c.role)
            .select_from(p.outerjoin(g, (g.c.project_id == p.c.id) & (g.c.user_id == user_id)))
            .where(p.c.id == project_id)
        )
        project, role = (await conn.execute(query)).one()
        return project, role

    async def mark_handled(self, conn: AsyncConnection, trace_id: int, user_id: int) -> None:
        t = tables.system_traces
        await conn.execute(update(t).values(handled_by=user_id, handled_at=func.now()).where(t.c.id == trace_id))


STORE = CardStore()

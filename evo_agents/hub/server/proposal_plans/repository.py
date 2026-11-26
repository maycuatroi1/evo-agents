"""The rows of curator_changes an accepted proposal makes or plans again."""

from __future__ import annotations

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


class ChangeStore:
    async def record(self, conn: AsyncConnection, values: dict) -> int:
        """A new change of ``values``; its id."""
        c = tables.curator_changes
        return (await conn.execute(insert(c).values(**values).returning(c.c.id))).scalar_one()

    async def change_of(self, conn: AsyncConnection, proposal_id: int):
        """The change of proposal ``proposal_id``, its row locked; None when it has none."""
        c = tables.curator_changes
        query = select(c.c.id, c.c.state, c.c.plan_id, c.c.branch, c.c.pr_number, c.c.reason)
        return (await conn.execute(query.where(c.c.proposal_id == proposal_id).with_for_update())).one_or_none()

    async def planned(self, conn: AsyncConnection, change_id: int, made: dict) -> None:
        """Change ``change_id`` planned with ``made`` (its plan, repo, branch and forge), its reason cleared."""
        c = tables.curator_changes
        values = {**made, "state": "planned", "reason": None, "updated_at": func.now()}
        await conn.execute(update(c).values(**values).where(c.c.id == change_id))


STORE = ChangeStore()

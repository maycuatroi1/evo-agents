"""The rows of plan_run_choices (0238), one a member and project, and the project's default agent a choice falls
back to."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.plan_run_choice.models import FIELDS
from evo_agents.hub.server.profile_store import Profile, profile_of, profile_select


class ChoiceStore:
    async def default_profile(self, conn: AsyncConnection, project_id: int) -> Profile | None:
        ap = tables.agent_profiles
        query = profile_select().where(ap.c.project_id == project_id, ap.c.is_default)
        row = (await conn.execute(query)).one_or_none()
        return None if row is None else profile_of(row)

    async def row(self, conn: AsyncConnection, user_id: int, project_id: int):
        """The member's choice in the project, with its ``updated_at``; None when they saved none."""
        c = tables.plan_run_choices
        query = select(*(c.c[name] for name in FIELDS), c.c.updated_at)
        return (await conn.execute(query.where(c.c.user_id == user_id, c.c.project_id == project_id))).one_or_none()

    async def keep(self, conn: AsyncConnection, user_id: int, project_id: int, values: dict) -> None:
        """Keep ``values`` as the member's choice in the project, in place of the one before."""
        c = tables.plan_run_choices
        statement = pg_insert(c).values(user_id=user_id, project_id=project_id, **values)
        statement = statement.on_conflict_do_update(
            constraint="plan_run_choices_pkey", set_={**values, "updated_at": func.now()}
        )
        await conn.execute(statement)


STORE = ChoiceStore()

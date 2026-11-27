"""The proposals of @builder of kind ``goal`` (``proposals``): where one applies, how it is checked, applied and
rolled back, as its ``RecordKind`` (``records``).

A proposal names a goal of the project (``target.goal``, its code or alias) or none, for a new one whose code the hub
picks when it is proposed and keeps in the target. Its version is the goal's revision, 0 for a new one. Rolling back a
new goal closes it (dropped).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.builder.records import RecordKind
from evo_agents.hub import goals
from evo_agents.hub.server.goals import service as project_goals
from evo_agents.hub.server.projects import ProjectAccess


class GoalKind(RecordKind):
    noun = "goal"
    model = goals
    routes = project_goals
    write_model = project_goals.ProjectGoalWrite
    dropped_note = "closed"

    def reopens(self, sets: dict) -> bool:
        return sets.get("status", goals.CLOSED) != goals.CLOSED

    def dropped(self, row: Any, access: ProjectAccess, proposal_id: int, applied: int) -> BaseModel:
        return project_goals.closing(row, "dropped", None, applied)

    async def prepare(self, conn: AsyncConnection, access: ProjectAccess) -> None:
        await project_goals.adopt_charter_goals(conn, access.project_id, access.name)


KIND = GoalKind()

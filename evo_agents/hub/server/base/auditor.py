"""The audit of a write in a project: who wrote it (the actor and the token of the scope's principal, or none for
what the hub does by itself) and in which project, so a service names only the action and the target."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server import audit
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import Principal


@dataclass(frozen=True)
class Auditor:
    conn: AsyncConnection
    project_id: int
    user: Principal | None = None

    @classmethod
    def of(cls, conn: AsyncConnection, user: Principal | None, access: ProjectAccess) -> Auditor:
        return cls(conn, access.project_id, user)

    async def record(self, action: str, target: str) -> None:
        user = self.user
        await audit.record(
            self.conn,
            actor_id=user.user_id if user is not None else None,
            token_id=user.token_id if user is not None else None,
            action=action,
            target=target,
            project_id=self.project_id,
        )

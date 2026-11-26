"""A caller's scope in one project, as a FastAPI dependency: ``ProjectScope(role, noun)`` opens the request's
transaction (``UnitOfWork``), finds the project through ``project_access`` (404 without a grant, as for every route
of a project) and checks the role the routes it guards need, then yields a ``Scope``: the connection, the principal,
their access and the request. The transaction commits once the handler returns, before the response is sent
(``scope="function"`` of ``Depends``), and rolls back when it raises.

A reader needs a grant on the project (403 for a hub admin without one). A writer or an admin needs that role, and is
never the agent of a run: @builder proposes the write, which a member of the role approves. Each refusal names what
the routes guard (``Noun``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.access import has_role
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Forbidden
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser, Principal

Role = Literal["reader", "writer", "admin"]
APPROVER = {"writer": "a writer", "admin": "an admin"}  # who approves what @builder proposes, by the role it needs


@dataclass(frozen=True)
class Noun:
    """How a refusal names what the routes guard: ``one`` (a goal) and ``all`` (the goals)."""

    one: str
    all: str


@dataclass(frozen=True)
class Scope:
    conn: AsyncConnection
    user: Principal
    access: ProjectAccess
    request: Request

    @property
    def auditor(self) -> Auditor:
        return Auditor.of(self.conn, self.user, self.access)

    @property
    def jobs(self):
        """The app's queue of jobs (procrastinate)."""
        return self.request.app.state.jobs


class ProjectScope:
    def __init__(self, role: Role, noun: Noun):
        self.role, self.noun = role, noun

    def refusal(self, user: Principal, access: ProjectAccess) -> str | None:
        """Why ``user`` may not do what the role guards in the project; None when they may."""
        if self.role == "reader":
            return (
                None
                if access.role is not None
                else f"reading {self.noun.all} of project {access.name} needs a grant on it"
            )
        if user.acts_for_run is not None:
            approver = APPROVER[self.role]
            return f"the agent of a run never writes {self.noun.one}: @builder proposes one, which {approver} approves"
        if has_role(access.role, self.role):
            return None
        held = access.role or "no grant"
        return f"writing {self.noun.one} of project {access.name} needs the {self.role} role on it; {user.login} holds {held}"

    async def check(self, conn: AsyncConnection, user: Principal, project: str) -> ProjectAccess:
        """``user``'s access to the project, once they hold the role: 404 without a grant (``project_access``), 403
        without the role."""
        access = await project_access(conn, user, project)
        refused = self.refusal(user, access)
        if refused is not None:
            raise Forbidden(refused)
        return access

    async def __call__(self, request: Request, project: ProjectName, user: CurrentUser) -> AsyncIterator[Scope]:
        async with UnitOfWork(request.app.state.engine) as conn:
            yield Scope(conn, user, await self.check(conn, user, project), request)

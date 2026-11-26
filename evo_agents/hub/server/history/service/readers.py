"""Who reads the history of a project, and what of it: a member with a grant (``READERS``, 403 without one), through a
sink (``through``); the owner of a history run, whose facets count (``Reader``); and a caller reading many runs at
once (``RunReader``), as ``runs.service.views.readable_run`` decides it for one."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server.base.errors import NotFound
from evo_agents.hub.server.base.scope import Noun, ProjectScope
from evo_agents.hub.server.history import repository
from evo_agents.hub.server.plans.service import read_sink as through
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.runs.service.views import agent_reads, sees_unplanned, visible_plans
from evo_agents.hub.server.security import MACHINE, Principal

READERS = ProjectScope("reader", Noun("the Curator", "the Curator"))  # 403: "reading the Curator of project P needs..."


async def history_access(conn: AsyncConnection, user: Principal, project: str, sink: str | None) -> ProjectAccess:
    """The caller's access to ``project`` when it reads the Curator's history of it: a grant, and the project's default
    label visible through ``sink`` (the project's hub sink when None); 404 otherwise. The routes and the MCP tool
    history_clusters read the clusters through it."""
    return cleared(await READERS.check(conn, user, project), project, sink)


def cleared(access: ProjectAccess, project: str, sink: str | None) -> ProjectAccess:
    """``access``, once the project's default label is visible through ``sink``; 404 otherwise."""
    if not access.visible(access.rules.default_label, through(access, sink)):  # pyright: ignore[reportArgumentType]
        raise NotFound(f"project {project} has no history of the Curator you may read through this sink")
    return access


async def owner_access(conn: AsyncConnection, user_id: int, project_id: int) -> ProjectAccess | None:
    """The access of member ``user_id`` to project ``project_id``, as a request of theirs would get it; None without
    one."""
    login, name = await repository.member_and_project(conn, user_id, project_id)
    member = Principal(user_id=user_id, login=login, admin=False, token_id=0, kind=MACHINE, token_hash="")
    try:
        return await project_access(conn, member, name)
    except HTTPException:
        return None


@dataclass(frozen=True)
class Reader:
    """Whose facets count: an access, the hub sink it reads through and the plans whose runs it may read."""

    access: ProjectAccess
    plans: frozenset

    @property
    def through(self) -> str | None:
        return self.access.rules.hub_sink.sink if self.access.rules.hub_sink else None

    def reads(self, row) -> bool:
        if row.kind == "session":
            return self.through is not None and self.access.visible(row.label, self.through)
        return row.run_kind == "review" or row.plan_id in self.plans


async def reader_of(conn: AsyncConnection, access: ProjectAccess) -> Reader:
    return Reader(access, frozenset(await visible_plans(conn, access, None)))


class RunReader:
    """Whether the caller reads a run: as ``readable_run`` decides it, for many runs at once."""

    def __init__(self, user: Principal, plans: list[str], unplanned: bool):
        self.user, self.plans, self.unplanned = user, frozenset(plans), unplanned

    def reads(self, kind: str | None, plan_id: str | None, dispatched_by: int | None) -> bool:
        if not agent_reads(self.user, kind):
            return False
        if kind == "local":
            return dispatched_by == self.user.user_id
        if plan_id is None:
            return self.unplanned
        return plan_id in self.plans

    def clause(self):
        """The same rule in SQL, on the runs table."""
        user = self.user
        mine, replays = agent_reads(user, "local"), agent_reads(user, "replay")
        return repository.runs_clause(user.user_id, mine, self.unplanned, self.plans, replays)


async def run_reader(conn: AsyncConnection, user: Principal, access: ProjectAccess, sink: str | None) -> RunReader:
    return RunReader(user, await visible_plans(conn, access, sink), sees_unplanned(access, sink))

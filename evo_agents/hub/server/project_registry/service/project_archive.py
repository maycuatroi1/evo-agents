"""Archiving a project and making it live again (``evo_agents.hub.server.project_archive``): who may, the runs that
stop it, the change with its audit row, and ``refuse_archived``, which every new run and kg push goes through."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.server import audit
from evo_agents.hub.server.autonomy.service import hold as autonomy_hold
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.project_registry.models.projects import Project
from evo_agents.hub.server.project_registry.repository import project_archive as repository
from evo_agents.hub.server.project_registry.service.access import project_access
from evo_agents.hub.server.project_registry.service.projects import project_view
from evo_agents.hub.server.security import Principal, holds

MAX_NAMED_RUNS = 20  # the active runs a refusal to archive names; it counts the rest


class Archived(autonomy_hold.Stopped):
    """409: the project is archived, and what was asked of it starts nothing there."""

    def __init__(self, detail: str) -> None:
        HTTPException.__init__(self, 409, detail)


def refusal(name: str, refused: str) -> str:
    return (
        f"project {name} is archived, so {refused}; an admin of the project unarchives it with `evo-agents hub "
        f"project unarchive {name}`"
    )


async def archived_name(conn: AsyncConnection, project_id: int) -> str | None:
    """The name of project ``project_id`` when it is archived, else None; its row held FOR KEY SHARE until the
    transaction ends, so it cannot be archived meanwhile."""
    row = await repository.archived_row(conn, project_id)
    return row.name if row is not None and row.archived_at is not None else None


async def refuse_archived(conn: AsyncConnection, project_id: int, refused: str = "it takes no new run") -> None:
    """``Archived`` when project ``project_id`` is archived; ``refused`` says what it does not take."""
    name = await archived_name(conn, project_id)
    if name is not None:
        raise Archived(refusal(name, f"{refused}; nothing was queued"))


async def admin_of(conn: AsyncConnection, user: Principal, project: str, doing: str) -> int:
    """The id of ``project``, its row locked FOR UPDATE, when ``user`` is an admin of it or a hub admin; 404 for a
    project they do not see, 403 otherwise."""
    access = await project_access(conn, user, project)
    if user.acts_for_run is not None:
        raise HTTPException(403, f"{doing} project {project} is for an admin of it, never for the agent of a run")
    if not user.admin and not holds(access.role, "admin"):
        raise HTTPException(403, f"{doing} project {project} needs the admin role on it, or a hub admin")
    await repository.lock(conn, access.project_id)
    return access.project_id


def busy(project: str, active: list[int]) -> str:
    named = ", ".join(f"#{run_id}" for run_id in active[:MAX_NAMED_RUNS])
    more = f" and {len(active) - MAX_NAMED_RUNS} more" if len(active) > MAX_NAMED_RUNS else ""
    return (
        f"project {project} has {len(active)} active run(s): {named}{more}. Cancel them first with `evo-agents hub "
        "run cancel RUN`; nothing was archived"
    )


async def set_archived(conn: AsyncConnection, user: Principal, project: str, *, archive: bool) -> bool:
    """Archive or unarchive ``project`` for ``user`` in ``conn``'s transaction; whether it changed."""
    doing = "archiving" if archive else "unarchiving"
    project_id = await admin_of(conn, user, project, doing)
    archived_at = await repository.archived_at(conn, project_id)
    if (archived_at is not None) == archive:
        return False
    if archive:
        active = await repository.active_runs(conn, project_id)
        if active:
            raise HTTPException(409, busy(project, active))
    await repository.set_archived(conn, project_id, user.user_id if archive else None)
    action = audit.PROJECT_ARCHIVE if archive else audit.PROJECT_UNARCHIVE
    await audit.record(
        conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=project, project_id=project_id
    )
    return True


async def archive(engine: AsyncEngine, user: Principal, project: str, *, archive: bool) -> tuple[bool, Project]:
    """Archive or unarchive ``project`` for ``user``: whether it changed, and the project as ``user`` sees it."""
    async with UnitOfWork(engine) as conn:
        changed = await set_archived(conn, user, project, archive=archive)
        found = await project_view(conn, user, project)
    return changed, found

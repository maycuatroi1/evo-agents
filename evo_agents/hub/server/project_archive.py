"""An archived project: one its people no longer work on, which the hub keeps whole but no longer runs anything for.

POST /v1/projects/{project}/archive and POST /v1/projects/{project}/unarchive (``evo-agents hub project archive`` and
``unarchive``) are for an admin of the project or a hub admin, never for the agent of a run (403). Archiving refuses
(409, naming them) while a run of the project other than a local one is active (``runs.ACTIVE_STATES``): those are
cancelled first, so nothing is left running in an archived project. It sets ``projects.archived_at`` and
``archived_by`` under the project's row lock; unarchiving clears both. Archiving an archived project, or unarchiving a
live one, changes nothing and writes no audit row; a change is audited as project.archive or project.unarchive, its
target the project. Both answer the project as GET /v1/projects/{project} shows it.

What an archived project refuses or leaves out, until it is unarchived:

- a new run of any kind: ``refuse_archived`` in ``runs.service.dispatch.insert_run``, which every dispatch, chat run,
  night shift, Loop and release goes through, raises ``Archived``, a 409 that is an ``autonomy_hold.Stopped``, so the
  places that answer such a refusal with words (a mention in a chat, the Loop) do so for an archived project too; the
  local run of a Claude Code session (``local_runs``) is refused the same way;
- a kg push (POST /v1/kg/{project}/runs) and a build a member asks for (POST /v1/kg/{project}/builds), 409; every
  other build (a plugin's change, a stalled build, a graph of an older schema) is skipped by ``kg_build.queue_build``;
- the Curator's night shift (``curator_shift.due_schedules``), the Loop's ticks (``loop_jobs``) and the plugins' jobs
  (``plugin_jobs``), which leave it out.

Everything the project holds reads as before. GET /v1/projects lists the live projects alone unless ``archived`` is
true; GET /v1/projects/{project} shows one either way, with ``archived_at`` and ``archived_by``.

``refuse_archived`` takes the project's row FOR KEY SHARE before it reads the state, and archiving takes it FOR UPDATE,
so a run inserted while the project is being archived is either seen by the archive (409) or refused by the check.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit, autonomy_hold
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import Project, project_access, project_view
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409)}
MAX_NAMED_RUNS = 20  # the active runs a refusal to archive names; it counts the rest

router = APIRouter(prefix="/v1/projects", tags=["projects"], responses={401: {"model": ErrorBody}})


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
    p = tables.projects
    held = select(p.c.name, p.c.archived_at).where(p.c.id == project_id).with_for_update(read=True, key_share=True)
    row = (await conn.execute(held)).one_or_none()
    return row.name if row is not None and row.archived_at is not None else None


async def refuse_archived(conn: AsyncConnection, project_id: int, refused: str = "it takes no new run") -> None:
    """``Archived`` when project ``project_id`` is archived; ``refused`` says what it does not take."""
    name = await archived_name(conn, project_id)
    if name is not None:
        raise Archived(refusal(name, f"{refused}; nothing was queued"))


async def _admin_of(conn: AsyncConnection, user: Principal, project: str, doing: str) -> int:
    """The id of ``project``, its row locked FOR UPDATE, when ``user`` is an admin of it or a hub admin; 404 for a
    project they do not see, 403 otherwise."""
    access = await project_access(conn, user, project)
    if user.acts_for_run is not None:
        raise HTTPException(403, f"{doing} project {project} is for an admin of it, never for the agent of a run")
    if not user.admin and not has_role(access.role, "admin"):
        raise HTTPException(403, f"{doing} project {project} needs the admin role on it, or a hub admin")
    p = tables.projects
    await conn.execute(select(p.c.id).where(p.c.id == access.project_id).with_for_update())
    return access.project_id


async def _active_runs(conn: AsyncConnection, project_id: int) -> list[int]:
    r = tables.runs
    query = select(r.c.id).where(r.c.project_id == project_id, r.c.state.in_(runs.ACTIVE_STATES), r.c.kind != "local")
    return list(await conn.scalars(query.order_by(r.c.id)))


def _busy(project: str, active: list[int]) -> str:
    named = ", ".join(f"#{run_id}" for run_id in active[:MAX_NAMED_RUNS])
    more = f" and {len(active) - MAX_NAMED_RUNS} more" if len(active) > MAX_NAMED_RUNS else ""
    return (
        f"project {project} has {len(active)} active run(s): {named}{more}. Cancel them first with `evo-agents hub "
        "run cancel RUN`; nothing was archived"
    )


async def _set(conn: AsyncConnection, user: Principal, project: str, *, archive: bool) -> bool:
    """Archive or unarchive ``project`` for ``user`` in ``conn``'s transaction; whether it changed."""
    doing = "archiving" if archive else "unarchiving"
    project_id = await _admin_of(conn, user, project, doing)
    p = tables.projects
    archived_at = await conn.scalar(select(p.c.archived_at).where(p.c.id == project_id))
    if (archived_at is not None) == archive:
        return False
    if archive:
        active = await _active_runs(conn, project_id)
        if active:
            raise HTTPException(409, _busy(project, active))
        values = {"archived_at": func.now(), "archived_by": user.user_id}
    else:
        values = {"archived_at": None, "archived_by": None}
    await conn.execute(update(p).where(p.c.id == project_id).values(**values))
    action = audit.PROJECT_ARCHIVE if archive else audit.PROJECT_UNARCHIVE
    await audit.record(
        conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=project, project_id=project_id
    )
    return True


@router.post("/{project}/archive", response_model=Project, responses=REFUSALS)
async def archive(request: Request, project: ProjectName, user: CurrentUser) -> Project:
    """Archive the project: it takes no new run, kg push or build, and nothing the hub schedules runs for it."""
    async with request.app.state.engine.begin() as conn:
        changed = await _set(conn, user, project, archive=True)
        found = await project_view(conn, user, project)
    log.info("project archived", extra={"project": project, "login": user.login, "changed": changed})
    return found


@router.post("/{project}/unarchive", response_model=Project, responses=REFUSALS)
async def unarchive(request: Request, project: ProjectName, user: CurrentUser) -> Project:
    """Make the archived project live again: its night shift, Loop and plugins' jobs run again as they are set."""
    async with request.app.state.engine.begin() as conn:
        changed = await _set(conn, user, project, archive=False)
        found = await project_view(conn, user, project)
    log.info("project unarchived", extra={"project": project, "login": user.login, "changed": changed})
    return found

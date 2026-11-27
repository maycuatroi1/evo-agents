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
- the Curator's night shift (``curator.repository.shift.due_schedules``), the Loop's ticks (``loop_jobs``) and the
  plugins' jobs (``plugin_jobs``), which leave it out.

Everything the project holds reads as before. GET /v1/projects lists the live projects alone unless ``archived`` is
true; GET /v1/projects/{project} shows one either way, with ``archived_at`` and ``archived_by``.

``refuse_archived`` takes the project's row FOR KEY SHARE before it reads the state, and archiving takes it FOR UPDATE,
so a run inserted while the project is being archived is either seen by the archive (409) or refused by the check.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.project_registry.models.projects import Project
from evo_agents.hub.server.project_registry.service import project_archive as service
from evo_agents.hub.server.project_registry.service.project_archive import MAX_NAMED_RUNS as MAX_NAMED_RUNS
from evo_agents.hub.server.project_registry.service.project_archive import Archived as Archived
from evo_agents.hub.server.project_registry.service.project_archive import archived_name as archived_name
from evo_agents.hub.server.project_registry.service.project_archive import refusal as refusal
from evo_agents.hub.server.project_registry.service.project_archive import refuse_archived as refuse_archived
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409)}

router = APIRouter(prefix="/v1/projects", tags=["projects"], responses={401: {"model": ErrorBody}})


@router.post("/{project}/archive", response_model=Project, responses=REFUSALS)
async def archive(request: Request, project: ProjectName, user: CurrentUser) -> Project:
    """Archive the project: it takes no new run, kg push or build, and nothing the hub schedules runs for it."""
    changed, found = await service.archive(request.app.state.engine, user, project, archive=True)
    log.info("project archived", extra={"project": project, "login": user.login, "changed": changed})
    return found


@router.post("/{project}/unarchive", response_model=Project, responses=REFUSALS)
async def unarchive(request: Request, project: ProjectName, user: CurrentUser) -> Project:
    """Make the archived project live again: its night shift, Loop and plugins' jobs run again as they are set."""
    changed, found = await service.archive(request.app.state.engine, user, project, archive=False)
    log.info("project unarchived", extra={"project": project, "login": user.login, "changed": changed})
    return found

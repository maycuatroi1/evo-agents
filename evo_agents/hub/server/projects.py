"""Projects: registering a harness's project, the projects a caller sees, and a caller's access to one of them.

PUT /v1/projects/{project} takes what ``evo-agents hub project register`` read in a harness: the ladder (levels and
locations) and the sinks of its knowledge.yaml, the default label of its memories and plans (the harness
source's), its repos, and where the harness and the repos sit relative to a workspace. Registering a new project
needs a hub admin, or a member holding an unused right to register one, which an accepted invite link gives
(``evo_agents.hub.server.invites``): in the same transaction the project is created, the member gets the admin role at
its highest level, and the right is marked used. Registering it again, to bring the hub up to date with its harness,
needs a hub admin or the admin role on the project. The same body twice changes nothing and writes no audit row. A
change replaces the sinks and repos in the same transaction and adds one audit row naming the project, never what it
holds; a change that drops a level some grant reaches is refused until the grant is changed. A new project gets its
default agent profile (``profile_store.ensure_default``), whose handle is the project's name; a name that is the
handle of another project's agent is refused (409), since @<name> would name both.

GET /v1/projects lists the projects the caller holds a grant on, all of them for a hub admin, with the caller's
role and max level, the archived ones (``evo_agents.hub.server.project_archive``) only with ``archived`` true;
GET /v1/projects/{project} is one of them, archived or not. A project that is not registered and one the caller cannot
see get the same 404.

``project_access`` is where the routes for memories, plans, skills and the knowledge graph start: the project's
rules (``evo_agents.hub.access``) and the caller's grant, with the read and write rules applied through them. A hub
admin without a grant manages a project but reads and pushes nothing in it. The agent of a run, on the hub's /mcp
with its worker's token (``Principal.scope``), sees the run's project alone, every other one answering as a project
that is not registered, and there uses its owner's grant capped by the scope (``scoped_grant``): the lower role and
the lower level of the two.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Query, Request

from evo_agents.hub.access import ProjectRules as ProjectRules
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.paging import PAGED, WHOLE, Paging
from evo_agents.hub.server.project_registry.models.projects import PRINTABLE as PRINTABLE
from evo_agents.hub.server.project_registry.models.projects import REFUSALS, Project, Registered, Registration
from evo_agents.hub.server.project_registry.models.projects import SINK_KINDS as SINK_KINDS
from evo_agents.hub.server.project_registry.models.projects import Clearance as Clearance
from evo_agents.hub.server.project_registry.models.projects import Harness as Harness
from evo_agents.hub.server.project_registry.models.projects import LabelIn as LabelIn
from evo_agents.hub.server.project_registry.models.projects import Name as Name
from evo_agents.hub.server.project_registry.models.projects import PathText as PathText
from evo_agents.hub.server.project_registry.models.projects import ProjectAccess as ProjectAccess
from evo_agents.hub.server.project_registry.models.projects import Repo as Repo
from evo_agents.hub.server.project_registry.models.projects import Sink as Sink
from evo_agents.hub.server.project_registry.models.projects import not_found as not_found
from evo_agents.hub.server.project_registry.service import projects as service
from evo_agents.hub.server.project_registry.service.access import access_of as access_of
from evo_agents.hub.server.project_registry.service.access import project_access as project_access
from evo_agents.hub.server.project_registry.service.access import scoped_grant as scoped_grant
from evo_agents.hub.server.project_registry.service.projects import create_project as create_project
from evo_agents.hub.server.project_registry.service.projects import project_view as project_view
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["projects"], responses={401: {"model": ErrorBody}})


@router.get("", response_model=list[Project], responses=PAGED)
async def list_projects(
    request: Request,
    user: CurrentUser,
    page: Paging = WHOLE,
    archived: Annotated[bool, Query(description="list the archived projects too")] = False,
) -> list[Project]:
    return await service.list_projects(request.app.state.engine, user, page, archived)


@router.get("/{project}", response_model=Project, responses={404: {"model": ErrorBody}})
async def show(request: Request, project: ProjectName, user: CurrentUser) -> Project:
    return await service.show(request.app.state.engine, user, project)


@router.put("/{project}", response_model=Registered, responses=REFUSALS)
async def register(request: Request, body: Registration, project: ProjectName, user: CurrentUser) -> Registered:
    """Register ``project`` from its harness, or bring the hub's copy up to date with it."""
    done = await service.register(request.app.state.engine, user, project, body)
    outcome = "registered" if done.created else "updated" if done.changed else "unchanged"
    log.info("project registration", extra={"project": project, "outcome": outcome, "login": user.login})
    return done

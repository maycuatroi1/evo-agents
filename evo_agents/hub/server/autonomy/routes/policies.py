"""The routes of the autonomy policy of a project (plan gitlab-merge-queue, criterion a2; the keys in
``evo_agents.hub.autonomy``). Table autonomy_policies, schema 0079.

- GET /v1/projects/{project}/policy: the policy now, for a member with a grant on the project: the keys its newest
  revision sets (``policy``) and every key at that value or its default (``effective``), each repo of the project
  included. Revision 0 is a project that never had one: every repo off, as the hub was before the policy.
- GET .../policy/revisions, newest first, each with the keys whose value it changed; GET .../revisions/{revision},
  one of them whole; GET .../policy/compare?from=N&to=M, the keys whose value differs between two revisions.
- PUT .../policy: the next revision, for an admin of the project. The body is the whole policy, as the admin writes
  it; a body equal to the newest revision writes nothing (``changed`` false). ``if_revision`` other than the revision
  now is 409. The agent of a run never writes it: @builder proposes a change (proposal kind ``policy``,
  ``evo_agents.core_plugins.builder.proposals``), which an admin approves, and the hub writes it through
  ``autonomy.service.policies.write``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request

from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.autonomy.models.policies import (
    MAX_REVISION,
    AutonomyPolicy,
    PolicyComparison,
    PolicyRevision,
    PolicyWrite,
    PolicyWritten,
)
from evo_agents.hub.server.autonomy.service import guards as route_autonomy
from evo_agents.hub.server.autonomy.service import policies
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import CurrentUser

REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404)}
WRITE_REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
RevisionNumber = Annotated[int, Path(ge=1, le=MAX_REVISION)]

router = APIRouter(
    prefix="/v1/projects", tags=["policy"], dependencies=[route_autonomy.K12], responses={401: {"model": ErrorBody}}
)


@router.get("/{project}/policy", response_model=AutonomyPolicy, responses=REFUSALS)
async def show_policy(request: Request, project: ProjectName, user: CurrentUser) -> AutonomyPolicy:
    """The project's policy now: what its newest revision sets, and every key as it reads."""
    async with request.app.state.engine.begin() as conn:
        return await policies.view_at(conn, await policies.reader(conn, user, project))


@router.put("/{project}/policy", response_model=PolicyWritten, responses=WRITE_REFUSALS)
async def write_policy(request: Request, project: ProjectName, body: PolicyWrite, user: CurrentUser) -> PolicyWritten:
    """Write the project's policy whole as its next revision; an admin of the project alone may."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        policies.refuse_writer(user, access)
        revision, changed = await policies.write(
            conn, user, access, body.policy, note=body.note, if_revision=body.if_revision
        )
        shown = await policies.view_at(conn, access, revision)
    return PolicyWritten(**shown.model_dump(), changed=changed)


@router.get("/{project}/policy/revisions", response_model=list[PolicyRevision], responses=REFUSALS)
async def policy_revisions(request: Request, project: ProjectName, user: CurrentUser) -> list[PolicyRevision]:
    """Every revision of the project's policy, the newest first, each with the keys whose value it changed."""
    async with request.app.state.engine.begin() as conn:
        return await policies.revisions(conn, await policies.reader(conn, user, project))


@router.get("/{project}/policy/revisions/{revision}", response_model=AutonomyPolicy, responses=REFUSALS)
async def show_policy_revision(
    request: Request, project: ProjectName, revision: RevisionNumber, user: CurrentUser
) -> AutonomyPolicy:
    """One revision of the project's policy whole."""
    async with request.app.state.engine.begin() as conn:
        return await policies.view_at(conn, await policies.reader(conn, user, project), revision)


@router.get("/{project}/policy/compare", response_model=PolicyComparison, responses=REFUSALS)
async def compare_policy(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    from_revision: Annotated[
        int, Query(alias="from", ge=0, le=MAX_REVISION, description="the older revision; 0 for no policy")
    ],
    to_revision: Annotated[
        int | None, Query(alias="to", ge=0, le=MAX_REVISION, description="the newer revision; the newest when left out")
    ] = None,
) -> PolicyComparison:
    """The keys whose value differs between two revisions of the project's policy, each as it reads now."""
    async with request.app.state.engine.begin() as conn:
        return await policies.compare(conn, await policies.reader(conn, user, project), from_revision, to_revision)

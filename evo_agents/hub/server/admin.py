"""Hub administration, for the logins listed in EVO_HUB_ADMINS only: users, grants, what needs an admin, row counts,
and the retention of built graphs. (The admins of a self-managed project give grants on it through
``evo_agents.hub.server.project_admin``.)

GET /v1/admin/overview answers what an admin may have to act on, in one read: members and how many were seen lately,
tokens about to expire or long unused, the grants of each project, the bytes the blob store holds and the objects
waiting to leave it, graph builds that failed lately, offline workers, and the audit rows of the last day. Each count
names its window, so the web says it without knowing the hub's settings, and the token counts use the same conditions
as GET /v1/admin/tokens?state=expiring and ?state=unused, which list them. GET /v1/admin/stats counts the rows of every
table, for diagnostics.

A grant gives a login a role on a project (reader, writer or admin) and the highest level of that project's label
ladder it may see. The ladder is the project's own, as ``evo_agents.schema.policy.Policy`` reads it from the levels
registered with the project, so a max level outside it is refused. A login that has not signed in yet can be
granted: its users row waits for the first sign-in, which claims it. Granting and revoking add an audit row
naming the project, the login and the role, never anything the grant gives access to. Revoking a grant, or lowering it
to reader, gives back the credential leases still out of every run the member dispatched in the project
(``credentials.end_member_leases``), whose worker can no longer ask for them.

POST /v1/admin/kg/prune runs the retention of built graphs now (``evo_agents.hub.kg_prune``), as the worker does every
hour: for one project or every one, keeping the artifacts of the ``keep`` newest graphs of each (default
EVO_HUB_KG_KEEP_ARTIFACTS). With ``dry_run`` it answers what it would delete and changes nothing.

POST /v1/admin/kg/scrub reads a page of a project's built graphs and source blobs and answers those holding a secret,
never a value, and POST /v1/admin/kg/scrub/purge deletes the old ones of them (``evo_agents.hub.server.kg_scrub``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from evo_agents.hub.blobs import BlobStoreUnavailable
from evo_agents.hub.server import kg_scrub
from evo_agents.hub.server.autonomy.service import guards as route_autonomy
from evo_agents.hub.server.base.models import PROJECT_NAME, Login, ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.hub_admin.models.admin import ACTIVE_DAYS as ACTIVE_DAYS
from evo_agents.hub.server.hub_admin.models.admin import AUDIT_HOURS as AUDIT_HOURS
from evo_agents.hub.server.hub_admin.models.admin import FAILED_BUILD_DAYS as FAILED_BUILD_DAYS
from evo_agents.hub.server.hub_admin.models.admin import MISSING as MISSING
from evo_agents.hub.server.hub_admin.models.admin import SHA256 as SHA256
from evo_agents.hub.server.hub_admin.models.admin import TOKEN_EXPIRING_DAYS as TOKEN_EXPIRING_DAYS
from evo_agents.hub.server.hub_admin.models.admin import TOKEN_UNUSED_DAYS as TOKEN_UNUSED_DAYS
from evo_agents.hub.server.hub_admin.models.admin import AdminOverview as AdminOverview
from evo_agents.hub.server.hub_admin.models.admin import AuditCounts as AuditCounts
from evo_agents.hub.server.hub_admin.models.admin import FailedBuildCounts as FailedBuildCounts
from evo_agents.hub.server.hub_admin.models.admin import Grant as Grant
from evo_agents.hub.server.hub_admin.models.admin import GrantRequest as GrantRequest
from evo_agents.hub.server.hub_admin.models.admin import KgPruned as KgPruned
from evo_agents.hub.server.hub_admin.models.admin import MemberCounts as MemberCounts
from evo_agents.hub.server.hub_admin.models.admin import ProjectFailedBuilds as ProjectFailedBuilds
from evo_agents.hub.server.hub_admin.models.admin import ProjectGrantCounts as ProjectGrantCounts
from evo_agents.hub.server.hub_admin.models.admin import ProjectPruned as ProjectPruned
from evo_agents.hub.server.hub_admin.models.admin import PruneRequest as PruneRequest
from evo_agents.hub.server.hub_admin.models.admin import StorageCounts as StorageCounts
from evo_agents.hub.server.hub_admin.models.admin import TokenCounts as TokenCounts
from evo_agents.hub.server.hub_admin.models.admin import UserGrant as UserGrant
from evo_agents.hub.server.hub_admin.models.admin import UserRow as UserRow
from evo_agents.hub.server.hub_admin.models.admin import WorkerCounts as WorkerCounts
from evo_agents.hub.server.hub_admin.repository.admin import TOKEN_EXPIRING as TOKEN_EXPIRING
from evo_agents.hub.server.hub_admin.repository.admin import TOKEN_LIVE as TOKEN_LIVE
from evo_agents.hub.server.hub_admin.repository.admin import TOKEN_UNUSED as TOKEN_UNUSED
from evo_agents.hub.server.hub_admin.repository.admin import upsert_grant as upsert_grant
from evo_agents.hub.server.hub_admin.service import admin as service
from evo_agents.hub.server.hub_admin.service.admin import give_grant as give_grant
from evo_agents.hub.server.hub_admin.service.admin import project_ladder as project_ladder
from evo_agents.hub.server.hub_admin.service.admin import take_grant as take_grant
from evo_agents.hub.server.paging import PAGED, WHOLE, Paging
from evo_agents.hub.server.security import AdminUser, admin

router = APIRouter(
    prefix="/v1/admin", tags=["admin"], dependencies=[Depends(admin)], responses={401: {"model": ErrorBody}}
)


class ScrubRequest(BaseModel):
    project: str = Field(pattern=PROJECT_NAME)
    after: str | None = Field(None, pattern=SHA256, description="next of the page before; left out for the first page")
    limit: int = Field(
        kg_scrub.DEFAULT_LIMIT, ge=1, le=kg_scrub.MAX_LIMIT, description="objects this page reads at most"
    )


class ScrubPurgeRequest(BaseModel):
    project: str = Field(pattern=PROJECT_NAME)
    sha256: list[Annotated[str, Field(pattern=SHA256)]] = Field(
        min_length=1, max_length=kg_scrub.MAX_PURGE, description="hashes a scan found old"
    )


@router.get("/users", response_model=list[UserRow], responses={403: {"model": ErrorBody}, **PAGED})
async def users(request: Request, page: Paging = WHOLE) -> list[UserRow]:
    return await service.users(request.app.state.engine, request.app.state.config, page)


async def revoke_taken(request: Request, run_ids: list[int]) -> None:
    """Revoke at GitHub the tokens of the leases a grant taken back gave back (``service.take_back_leases``); the
    route of a self-managed project's admin gives the leases back as well."""
    await service.revoke_taken(request.app.state, run_ids)


@router.put(
    "/projects/{project}/grants/{login}",
    response_model=Grant,
    responses={**MISSING, 422: {"model": ErrorBody}},
    dependencies=[route_autonomy.K12],
)
async def grant(request: Request, body: GrantRequest, project: ProjectName, login: Login, user: AdminUser) -> Grant:
    """Give ``login`` a role on ``project``, or change the one it has."""
    given, taken = await service.grant(request.app.state.engine, user, project, login, body)
    await revoke_taken(request, taken)
    return given


@router.delete(
    "/projects/{project}/grants/{login}",
    status_code=204,
    response_class=Response,
    responses=MISSING,
    dependencies=[route_autonomy.K12],
)
async def revoke(request: Request, project: ProjectName, login: Login, user: AdminUser) -> Response:
    """Take away the role ``login`` has on ``project``."""
    taken = await service.revoke(request.app.state.engine, user, project, login)
    await revoke_taken(request, taken)
    return Response(status_code=204)


@router.get("/stats", response_model=dict[str, int], responses={403: {"model": ErrorBody}})
async def stats(request: Request) -> dict[str, int]:
    """The number of rows of every hub table, by table name; tables added by later revisions show up by themselves."""
    return await service.stats(request.app.state.engine)


@router.get("/overview", response_model=AdminOverview, responses={403: {"model": ErrorBody}})
async def overview(request: Request) -> AdminOverview:
    """What may need an admin: members, tokens to rotate, grants by project, storage, failed graph builds, offline
    workers and the last day of the audit trail."""
    return await service.overview(request.app.state.engine)


@router.post("/kg/prune", response_model=KgPruned, responses={**MISSING, 503: {"model": ErrorBody}})
async def prune_kg(request: Request, body: PruneRequest, user: AdminUser) -> KgPruned:
    """Delete the artifacts of graphs older than each project's ``keep`` newest; the builds keep their records."""
    from evo_agents.hub.kg_prune import UnknownProject, prune

    state = request.app.state
    if state.blobs is None:
        missing = ", ".join(state.config.blob_store_missing())
        raise HTTPException(503, f"the blob store is not configured on this hub: set {missing}")
    keep = body.keep or state.config.kg_keep_artifacts
    try:
        report = await prune(
            state.engine,
            state.blobs,
            keep,
            body.project,
            dry_run=body.dry_run,
            graph_files=body.graph_files,
            actor_id=user.user_id,
            token_id=user.token_id,
        )
    except UnknownProject as exc:
        raise HTTPException(404, f"{exc}: run `evo-agents hub project register`") from None
    except BlobStoreUnavailable:
        raise HTTPException(
            503,
            "the blob store did not answer: the builds were marked and their artifacts wait to be deleted by the next "
            "prune",
        ) from None
    return KgPruned(**report.to_json())


@router.post("/kg/scrub", response_model=kg_scrub.ScrubPage, responses={**MISSING, 503: {"model": ErrorBody}})
async def scrub_kg(request: Request, body: ScrubRequest) -> kg_scrub.ScrubPage:
    """Read a page of a project's built graphs and source blobs and say which hold a secret; changes nothing."""
    return await kg_scrub.scan(request.app.state, body.project, body.after, body.limit)


@router.post("/kg/scrub/purge", response_model=kg_scrub.ScrubPurged, responses={**MISSING, 503: {"model": ErrorBody}})
async def purge_kg_secrets(request: Request, body: ScrubPurgeRequest, user: AdminUser) -> kg_scrub.ScrubPurged:
    """Delete the old graphs and source blobs of ``sha256`` that hold a secret; the others stay, with the reason."""
    return await kg_scrub.purge(request.app.state, body.project, body.sha256, user)

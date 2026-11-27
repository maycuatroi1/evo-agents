"""Skills: versions of skill directories whose bundles live in the blob store, for ``hub skills sync`` to write into
the skills directories of every runtime.

A skill is global (a hub admin publishes it) or belongs to a project (the members of the project read it; publishing
follows the write rule of ``evo_agents.hub.access``: the writer role, and a hub sink that clears the project's default
label). A global skill has an audience: ``everyone`` signed in reads it, or hub ``admins`` alone (a project skill is
always ``everyone``, its project's grants deciding). Whoever is not a hub admin never sees an ``admins`` skill: the
list leaves it out, and asking for it or its bundle is a 404, the same as for a skill that does not exist. The agent
of a run reads what the run's owner reads: the admins skills when the owner is a hub admin. Publishing a new global
skill sets its audience, ``everyone`` by default; publishing a version with an audience changes it, and PUT
/v1/skills/global/{name}/audience changes it alone (both for a hub admin). Every change adds one audit row,
``skill:global/<name> audience=<was>-><is>``. Asking about a project skill without a grant on the project is a
403, the same whether the project exists or not; a hub admin without a grant reads nothing there either, and the
agent of a run (``Principal.scope``) reads the global skills and those of the run's project alone. Names are unique in
their scope ignoring case, since a name is a directory name on every machine.

A version records the bundle's SHA-256, size and key (blobs/sha256/<sha256>), the name and description of its
SKILL.md, who published it and when, and optionally the repo and commit it was built from. The bytes stay in the
blob store. Publishing happens in four calls: POST /v1/blobs/uploads for the bundle (kind skill-bundle, held by the
project, or by the hub itself for a global skill; over 10 MiB is refused there before any URL is issued), the PUT to
the presigned URL, POST /v1/blobs/commit, then POST .../versions here. That last call creates a version only for a
bundle the holder has committed, so the hub read the bytes back and hashed them, and only after reading the bundle
again from the blob store and checking it as ``evo_agents.hub.skills.read_bundle`` does (a well-formed tar.gz whose
SKILL.md names this skill); a bundle not committed, or another SHA-256 or size, is a 422 that creates nothing. The
same bundle as the latest version changes nothing. Every new version adds one audit row naming the skill,
``skill:global/<name>`` or ``skill:project/<project>/<name>``, never its content.

GET /v1/skills lists the skills the caller sees with their latest version; GET .../{name} is one skill with every
version; GET .../{name}/bundle hands out a presigned GET of one version's bundle, after the read check, together with
the SHA-256 the client checks the bytes against. The URL makes the blob store answer as a download named
``<name>-v<version>.tar.gz``, so a browser sent to it saves the file (the web navigates there and never fetches it,
so the bucket needs no CORS). The read check comes before the blob store is asked for: without a grant the answer is
403 whether the store is configured or not. Routes exist twice: under /v1/skills/global/ and under
/v1/skills/projects/{project}/.

The daemon of a worker pool node syncs the global skills with its worker token, which reads nothing under /v1/skills:
GET /v1/worker/skills lists the global skills its owner reads (the admins ones too when the owner is a hub admin),
and GET /v1/worker/skills/global/{name}/bundle hands out a bundle of one as above. These read skills alone, never
anything of a run, and answer 403 to a worker that is not a live pool node.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Request

from evo_agents.hub.server.base.models import PROJECT_NAME, ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.paging import PAGED, WHOLE, Paging
from evo_agents.hub.server.security import AdminUser, CurrentUser, Principal
from evo_agents.hub.server.skill_bundles.models.skills import ADMINS as ADMINS
from evo_agents.hub.server.skill_bundles.models.skills import AUDIENCE as AUDIENCE
from evo_agents.hub.server.skill_bundles.models.skills import AUDIENCE_TEXT as AUDIENCE_TEXT
from evo_agents.hub.server.skill_bundles.models.skills import AUDIENCES as AUDIENCES
from evo_agents.hub.server.skill_bundles.models.skills import EVERYONE as EVERYONE
from evo_agents.hub.server.skill_bundles.models.skills import GLOBAL_PLACE as GLOBAL_PLACE
from evo_agents.hub.server.skill_bundles.models.skills import MAX_SOURCE_REPO as MAX_SOURCE_REPO
from evo_agents.hub.server.skill_bundles.models.skills import PUBLISH as PUBLISH
from evo_agents.hub.server.skill_bundles.models.skills import SCOPES, SKILL_NAME, AudienceChange
from evo_agents.hub.server.skill_bundles.models.skills import UNAVAILABLE as UNAVAILABLE
from evo_agents.hub.server.skill_bundles.models.skills import BundleTicket as BundleTicket
from evo_agents.hub.server.skill_bundles.models.skills import Place as Place
from evo_agents.hub.server.skill_bundles.models.skills import Published as Published
from evo_agents.hub.server.skill_bundles.models.skills import PublishRequest as PublishRequest
from evo_agents.hub.server.skill_bundles.models.skills import Skill as Skill
from evo_agents.hub.server.skill_bundles.models.skills import SkillAudience as SkillAudience
from evo_agents.hub.server.skill_bundles.models.skills import SkillHistory as SkillHistory
from evo_agents.hub.server.skill_bundles.models.skills import Version as Version
from evo_agents.hub.server.skill_bundles.repository.skills import latest_skills as latest_skills
from evo_agents.hub.server.skill_bundles.service import skills as service

log = logging.getLogger(__name__)

SkillName = Annotated[str, Path(pattern=SKILL_NAME, description="the skill's name, its directory in every runtime")]
VersionNumber = Annotated[int | None, Query(ge=1, le=2**31 - 1, description="default: the latest")]
READ_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}
PUBLISH_REFUSALS = {code: {"model": ErrorBody} for code in (403, 422, 503)}
BUNDLE_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 503)}
AUDIENCE_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}

router = APIRouter(prefix="/v1/skills", tags=["skills"], responses={401: {"model": ErrorBody}})
worker_router = APIRouter(prefix="/v1/worker/skills", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


_writable = service.writable  # the place a caller publishes into, as tests/hub/test_mcp.py asks for it


def audiences(request: Request, user: Principal) -> tuple[str, ...]:
    """The audiences of the global skills ``user`` reads (``service.audiences``), under the hub's configuration."""
    return service.audiences(request.app.state.config, user)


@router.get("", response_model=list[Skill], responses={403: {"model": ErrorBody}, **PAGED})
async def list_skills(
    request: Request,
    user: CurrentUser,
    scope: Annotated[Literal[SCOPES] | None, Query(description="default: both")] = None,  # pyright: ignore[reportInvalidTypeForm]
    project: Annotated[str | None, Query(pattern=PROJECT_NAME, description="only this project's skills")] = None,
    page: Paging = WHOLE,
) -> list[Skill]:
    """The skills the caller sees, each with its latest version: the global ones of an audience the caller is in and
    those of every project the caller has a grant on."""
    state = request.app.state
    return await service.list_skills(state.engine, state.config, user, scope, project, page)


async def _history(request: Request, user: Principal, project: str | None, name: str) -> SkillHistory:
    return await service.history(request.app.state.engine, request.app.state.config, user, project, name)


@router.get("/global/{name}", response_model=SkillHistory, responses=READ_REFUSALS)
async def global_skill(request: Request, name: SkillName, user: CurrentUser) -> SkillHistory:
    """A global skill and every version of it."""
    return await _history(request, user, None, name)


@router.get("/projects/{project}/{name}", response_model=SkillHistory, responses=READ_REFUSALS)
async def project_skill(request: Request, project: ProjectName, name: SkillName, user: CurrentUser) -> SkillHistory:
    """A skill of a project and every version of it."""
    return await _history(request, user, project, name)


# Publishing


async def _publish(request: Request, user: Principal, project: str | None, name: str, body: PublishRequest):
    state = request.app.state
    return await service.publish(state.engine, state.blobs, state.config, user, project, name, body)


@router.post("/global/{name}/versions", response_model=Published, responses=PUBLISH_REFUSALS)
async def publish_global(request: Request, name: SkillName, body: PublishRequest, user: CurrentUser) -> Published:
    """A new version of a global skill from a committed bundle (a hub admin only)."""
    return await _publish(request, user, None, name, body)


@router.post("/projects/{project}/{name}/versions", response_model=Published, responses=PUBLISH_REFUSALS)
async def publish_project(
    request: Request, project: ProjectName, name: SkillName, body: PublishRequest, user: CurrentUser
) -> Published:
    """A new version of a skill of a project from a bundle the project committed."""
    return await _publish(request, user, project, name, body)


@router.put("/global/{name}/audience", response_model=SkillAudience, responses=AUDIENCE_REFUSALS)
async def set_audience(request: Request, name: SkillName, body: AudienceChange, user: AdminUser) -> SkillAudience:
    """Who reads a global skill: every signed-in member, or hub admins alone (a hub admin only). A change adds one
    audit row; the audience it has already changes nothing."""
    return await service.set_audience(request.app.state.engine, user, name, body.audience)


# Downloading


async def _bundle(request: Request, user: Principal, project: str | None, name: str, version: int | None):
    state = request.app.state
    return await service.bundle(state.engine, state.blobs, state.config, user, project, name, version)


@router.get("/global/{name}/bundle", response_model=BundleTicket, responses=BUNDLE_REFUSALS)
async def global_bundle(request: Request, name: SkillName, user: CurrentUser, version: VersionNumber = None):
    """A short-lived presigned GET of a global skill's bundle, with the SHA-256 to check it against."""
    return await _bundle(request, user, None, name, version)


@router.get("/projects/{project}/{name}/bundle", response_model=BundleTicket, responses=BUNDLE_REFUSALS)
async def project_bundle(
    request: Request, project: ProjectName, name: SkillName, user: CurrentUser, version: VersionNumber = None
):
    """A short-lived presigned GET of a project skill's bundle, for its members only (403 for anyone else)."""
    return await _bundle(request, user, project, name, version)


@worker_router.get("", response_model=list[Skill], responses={403: {"model": ErrorBody}, **PAGED})
async def worker_skills(request: Request, user: CurrentUser, page: Paging = WHOLE) -> list[Skill]:
    """The global skills the owner of the pool node whose token made the request reads, each with its latest
    version: what ``hub skills sync`` writes on a machine without a project."""
    return await service.worker_skills(request.app.state.engine, request.app.state.config, user, page)


@worker_router.get("/global/{name}/bundle", response_model=BundleTicket, responses=BUNDLE_REFUSALS)
async def worker_bundle(request: Request, name: SkillName, user: CurrentUser, version: VersionNumber = None):
    """A short-lived presigned GET of a global skill's bundle for a pool node, as GET /v1/skills/global/{name}/bundle
    answers its owner."""
    state = request.app.state
    return await service.worker_bundle(state.engine, state.blobs, state.config, user, name, version)

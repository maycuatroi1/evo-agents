"""The routes of the agent profiles of a project: GET and POST /v1/projects/{p}/agents, GET, PATCH and DELETE
.../agents/{handle}, GET .../agents/{handle}/versions, PUT .../agents/{handle}/account-pool, and PUT, DELETE and GET
.../agents/{handle}/avatar; ``evo_agents.hub.server.agents.service.profiles`` says the rules."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from evo_agents.hub.server.agents.models.profiles import (
    AVATAR_REFUSALS,
    AVATAR_TYPES,
    READ_REFUSALS,
    WRITE_REFUSALS,
    AccountPoolChoice,
    Agent,
    AgentCreate,
    AgentEdit,
    AgentVersion,
    AvatarUpload,
    Handle,
)
from evo_agents.hub.server.agents.service import profiles as service
from evo_agents.hub.server.autonomy.service import guards as route_autonomy
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/projects", tags=["agents"], dependencies=[route_autonomy.K12], responses={401: {"model": ErrorBody}}
)


@router.get("/{project}/agents", response_model=list[Agent], responses=READ_REFUSALS)
async def list_agents(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    deleted: Annotated[bool, Query(description="include the deleted ones")] = False,
) -> list[Agent]:
    """The agent profiles of the project, the default first, then by handle."""
    return await service.list_agents(request.app.state.engine, user, project, deleted)


@router.get("/{project}/agents/{handle}", response_model=Agent, responses=READ_REFUSALS)
async def show_agent(request: Request, project: ProjectName, handle: Handle, user: CurrentUser) -> Agent:
    """One agent profile of the project, deleted or not."""
    return await service.show(request.app.state.engine, user, project, handle)


@router.get("/{project}/agents/{handle}/versions", response_model=list[AgentVersion], responses=READ_REFUSALS)
async def agent_versions(
    request: Request, project: ProjectName, handle: Handle, user: CurrentUser
) -> list[AgentVersion]:
    """Every version of the profile, the latest first: the rules a run of that version had."""
    return await service.versions(request.app.state.engine, user, project, handle)


@router.post("/{project}/agents", status_code=201, response_model=Agent, responses=WRITE_REFUSALS)
async def create_agent(request: Request, project: ProjectName, body: AgentCreate, user: CurrentUser) -> Agent:
    """Create an agent profile of the project, at version 1."""
    created = await service.create(request.app.state.engine, user, project, body, request.app.state.hub_version)
    log.info("agent created", extra={"project": project, "handle": body.handle, "login": user.login})
    return created


@router.patch("/{project}/agents/{handle}", response_model=Agent, responses=WRITE_REFUSALS)
async def edit_agent(
    request: Request, project: ProjectName, handle: Handle, body: AgentEdit, user: CurrentUser
) -> Agent:
    """Change an agent profile: the fields given, as its next version; nothing changes for values it has already."""
    view, changes = await service.edit(
        request.app.state.engine, user, project, handle, body, request.app.state.hub_version
    )
    log.info("agent edited", extra={"project": project, "handle": handle, "changed": sorted(changes)})
    return view


@router.put("/{project}/agents/{handle}/account-pool", response_model=Agent, responses=WRITE_REFUSALS)
async def set_account_pool(
    request: Request, project: ProjectName, handle: Handle, body: AccountPoolChoice, user: CurrentUser
) -> Agent:
    """Attach an account pool of the project to the agent, or detach it (an admin of the project or of the hub): the
    agent has one, and a pool may serve many agents. In pool mode its runs lease the accounts of that pool alone."""
    view = await service.set_account_pool(request.app.state.engine, user, project, handle, body)
    log.info("agent account pool changed", extra={"project": project, "handle": handle})
    return view


@router.delete("/{project}/agents/{handle}", response_model=Agent, responses=WRITE_REFUSALS)
async def delete_agent(request: Request, project: ProjectName, handle: Handle, user: CurrentUser) -> Agent:
    """Soft-delete an agent profile: it takes no new run; its handle stays taken and it reads as before."""
    view, retired = await service.delete(request.app.state.engine, user, project, handle)
    log.info("agent deleted", extra={"project": project, "handle": handle, "login": user.login})
    if retired:
        log.info("agent accounts deleted with it", extra={"project": project, "handle": handle, "accounts": retired})
    return view


@router.put("/{project}/agents/{handle}/avatar", response_model=Agent, responses=AVATAR_REFUSALS)
async def put_avatar(
    request: Request, project: ProjectName, handle: Handle, body: AvatarUpload, user: CurrentUser
) -> Agent:
    """Set the profile's avatar, a PNG, JPEG or WebP image, as its next version."""
    # the refusals of the caller before the bytes are stored
    await service.may_change_avatar(request.app.state.engine, user, project)
    data, sha256 = service.image(body)
    view = await service.set_avatar(request, project, handle, user, sha256, data)
    log.info("agent avatar set", extra={"project": project, "handle": handle, "bytes": len(data)})
    return view


@router.delete("/{project}/agents/{handle}/avatar", response_model=Agent, responses=WRITE_REFUSALS)
async def delete_avatar(request: Request, project: ProjectName, handle: Handle, user: CurrentUser) -> Agent:
    """Drop the profile's avatar, as its next version: the web draws its initial on its colour again."""
    return await service.set_avatar(request, project, handle, user, None)


@router.get(
    "/{project}/agents/{handle}/avatar",
    response_class=Response,
    responses={200: {"content": {kind: {} for kind in AVATAR_TYPES}}, **READ_REFUSALS, 503: {"model": ErrorBody}},
)
async def get_avatar(request: Request, project: ProjectName, handle: Handle, user: CurrentUser) -> Response:
    """The profile's avatar image; 404 when it has none."""
    data, kind, sha256 = await service.avatar(request, project, handle, user)
    headers = {
        "Cache-Control": "private, max-age=86400",
        "ETag": f'"{sha256}"',
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'",
    }
    return Response(content=data, media_type=kind, headers=headers)

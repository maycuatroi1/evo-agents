"""What every member reads of the accounts of an agent (``docs/credentials.md``, section "Accounts of an agent"):
GET /v1/projects/{p}/agents/{handle}/accounts and GET .../accounts/{name}, the accounts of one agent, and GET
/v1/agent-accounts, the accounts of the agents of every project the caller holds a grant on.

Each account reads as ``agent_accounts.AgentAccount``: its name, variable, provider and category
(``account_providers``), workers, weight, priority, pause, cooldown, end, state, and the figures a member's own account
has, computed by the same functions (``account_views.agent_account_figures``): last usage, runs and their cost over 24
hours and 7 days (null for a service account), the runs holding it. Never a value: no query here reads one.

Reading needs a grant on the project, any role, as reading the agent's profile does: someone without one gets the 404
of ``project_access``, a hub admin without one a 403. An agent the project has not is 404; the default profile and a
deleted one have no live account, so an empty list. GET /v1/agent-accounts lists, for each project the caller holds a
grant on (and a run's scope reaches), the live accounts of its agents, each with its project and agent (handle, name,
colour, avatar), by project, handle, variable and name; ``project`` and ``handle`` narrow it. GET /v1/accounts keeps
its meaning: the caller's own accounts alone (``accounts``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import profiles, tables
from evo_agents.hub.server.account_views import agent_account_figures
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.agent_accounts import AccountName, AgentAccount, Handle, account_rows, view_of
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import CurrentUser

REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}

router = APIRouter(prefix="/v1", tags=["agents"], responses={401: {"model": ErrorBody}})


class ListedAgentAccount(AgentAccount):
    """An account of an agent, with the project and the agent it belongs to."""

    project: str
    handle: str = Field(description="the agent's handle, @ left out")
    agent_name: str = Field(description="the agent's display name")
    color: str = Field(description="the agent's colour, on which the web draws its initial")
    icon_url: str | None = Field(description="GET of the agent's avatar on this hub; null: draw the initial")


def _icon_url(project: str, handle: str, avatar_sha256: str | None) -> str | None:
    """As ``agent_profiles.icon_url`` writes it."""
    if avatar_sha256 is None:
        return None
    return f"/v1/projects/{project}/agents/{handle}/avatar?v={avatar_sha256[:16]}"


async def _views(conn: AsyncConnection, profile_ids: list[int], name: str | None = None) -> dict[int, list]:
    """The accounts of the profiles, as (profile id, AgentAccount fields) by profile id, in the order of
    ``account_rows``."""
    if not profile_ids:
        return {}
    rows = (await conn.execute(account_rows(profile_ids, name))).all()
    figures = await agent_account_figures(conn, profile_ids, [row.id for row in rows])
    found: dict[int, list] = {}
    for row in rows:
        found.setdefault(row.profile_id, []).append(view_of(row, figures.get(row.id)))
    return found


async def _profile_id(conn: AsyncConnection, user, project: str, handle: str) -> int:
    access = await project_access(conn, user, project)
    if access.role is None:
        raise HTTPException(403, f"reading the agents of project {access.name} needs a grant on it")
    ap = tables.agent_profiles
    found = select(ap.c.id).where(ap.c.project_id == access.project_id, ap.c.handle == handle)
    profile_id = (await conn.execute(found)).scalar_one_or_none()
    if profile_id is None:
        raise HTTPException(
            404, f"project {project} has no agent @{handle}: see `evo-agents hub agent list --project {project}`"
        )
    return profile_id


@router.get("/projects/{project}/agents/{handle}/accounts", response_model=list[AgentAccount], responses=REFUSALS)
async def list_agent_accounts(
    request: Request, project: ProjectName, handle: Handle, user: CurrentUser, response: Response
) -> list[AgentAccount]:
    """The live accounts of agent ``handle``, by variable and name, with their figures; never a value."""
    async with request.app.state.engine.begin() as conn:
        profile_id = await _profile_id(conn, user, project, handle)
        found = await _views(conn, [profile_id])
    response.headers.update(NO_STORE)
    return [AgentAccount(**view) for view in found.get(profile_id, [])]


@router.get("/projects/{project}/agents/{handle}/accounts/{name}", response_model=AgentAccount, responses=REFUSALS)
async def get_agent_account(
    request: Request, project: ProjectName, handle: Handle, name: AccountName, user: CurrentUser, response: Response
) -> AgentAccount:
    """The account ``name`` of agent ``handle`` with its figures; 404 when the agent has no live account of that name."""
    async with request.app.state.engine.begin() as conn:
        profile_id = await _profile_id(conn, user, project, handle)
        found = await _views(conn, [profile_id], name)
    if not found:
        raise HTTPException(404, f"@{handle} has no account named {name}")
    response.headers.update(NO_STORE)
    return AgentAccount(**found[profile_id][0])


@router.get("/agent-accounts", response_model=list[ListedAgentAccount])
async def list_all_agent_accounts(
    request: Request,
    user: CurrentUser,
    response: Response,
    project: Annotated[str | None, Query(description="only the agents of this project")] = None,
    handle: Annotated[
        str | None, Query(pattern=profiles.HANDLE_PATTERN, description="only the agent of this handle")
    ] = None,
) -> list[ListedAgentAccount]:
    """The live accounts of the agents of every project the caller holds a grant on, by project, handle, variable and
    name; a hub admin's grants alone count, as for reading a profile."""
    ap, projects, grants = tables.agent_profiles, tables.projects, tables.grants
    agents = (
        select(ap.c.id, ap.c.handle, ap.c.name, ap.c.color, ap.c.avatar_sha256, projects.c.name.label("project"))
        .join_from(ap, projects, projects.c.id == ap.c.project_id)
        .join(grants, (grants.c.project_id == projects.c.id) & (grants.c.user_id == user.user_id))
        .where(ap.c.deleted_at.is_(None), ap.c.is_default.is_(False))
        .order_by(projects.c.name, ap.c.handle)
    )
    if project is not None:
        agents = agents.where(projects.c.name == project)
    if handle is not None:
        agents = agents.where(ap.c.handle == handle)
    async with request.app.state.engine.begin() as conn:
        rows = [row for row in (await conn.execute(agents)).all() if user.reaches(row.project)]
        found = await _views(conn, [row.id for row in rows])
    response.headers.update(NO_STORE)
    return [
        ListedAgentAccount(
            **view,
            project=row.project,
            handle=row.handle,
            agent_name=row.name,
            color=row.color,
            icon_url=_icon_url(row.project, row.handle, row.avatar_sha256),
        )
        for row in rows
        for view in found.get(row.id, [])
    ]

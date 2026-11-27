"""What every member reads of the accounts of an agent (``docs/credentials.md``, section "Accounts of an agent"):
GET /v1/projects/{p}/agents/{handle}/accounts and GET .../accounts/{name}, the accounts of one agent, and GET
/v1/agent-accounts, the accounts of the agents of every project the caller holds a grant on.

Each account reads as ``agent_accounts.models.accounts.AgentAccount``: its name, variable, provider and category
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

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server.account_views import agent_account_figures
from evo_agents.hub.server.agent_accounts.models.views import icon_url
from evo_agents.hub.server.agent_accounts.repository import accounts as repository
from evo_agents.hub.server.agent_accounts.service.accounts import view_of
from evo_agents.hub.server.agent_accounts.service.shapes import AgentAccount, ListedAgentAccount
from evo_agents.hub.server.base.errors import Forbidden, NotFound
from evo_agents.hub.server.credentials.repository.secrets import bound_names
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import Principal


async def _views(conn: AsyncConnection, profile_ids: list[int], name: str | None = None) -> dict[int, list]:
    """The accounts of the profiles, as (profile id, AgentAccount fields) by profile id, in the order of
    ``account_rows``."""
    if not profile_ids:
        return {}
    rows = await repository.accounts_of(conn, bound_names, profile_ids, name)
    figures = await agent_account_figures(conn, profile_ids, [row.id for row in rows])
    found: dict[int, list] = {}
    for row in rows:
        found.setdefault(row.profile_id, []).append(view_of(row, figures.get(row.id)))
    return found


async def _profile_id(conn: AsyncConnection, user: Principal, project: str, handle: str) -> int:
    access = await project_access(conn, user, project)
    if access.role is None:
        raise Forbidden(f"reading the agents of project {access.name} needs a grant on it")
    profile_id = await repository.profile_id_of(conn, access.project_id, handle)
    if profile_id is None:
        raise NotFound(f"project {project} has no agent @{handle}: see `evo-agents hub agent list --project {project}`")
    return profile_id


async def list_accounts(conn: AsyncConnection, user: Principal, project: str, handle: str) -> list[AgentAccount]:
    """The live accounts of agent ``handle``, by variable and name, with their figures."""
    profile_id = await _profile_id(conn, user, project, handle)
    found = await _views(conn, [profile_id])
    return [AgentAccount(**view) for view in found.get(profile_id, [])]


async def show(conn: AsyncConnection, user: Principal, project: str, handle: str, name: str) -> AgentAccount:
    """The account ``name`` of agent ``handle`` with its figures; 404 when the agent has no live account of that
    name."""
    profile_id = await _profile_id(conn, user, project, handle)
    found = await _views(conn, [profile_id], name)
    if not found:
        raise NotFound(f"@{handle} has no account named {name}")
    return AgentAccount(**found[profile_id][0])


async def list_all(
    conn: AsyncConnection, user: Principal, project: str | None, handle: str | None
) -> list[ListedAgentAccount]:
    """The live accounts of the agents of every project the caller holds a grant on and reaches, by project, handle,
    variable and name."""
    rows = [
        row for row in await repository.listed_agents(conn, user.user_id, project, handle) if user.reaches(row.project)
    ]
    found = await _views(conn, [row.id for row in rows])
    return [
        ListedAgentAccount(
            **view,
            project=row.project,
            handle=row.handle,
            agent_name=row.name,
            color=row.color,
            icon_url=icon_url(row.project, row.handle, row.avatar_sha256),
        )
        for row in rows
        for view in found.get(row.id, [])
    ]

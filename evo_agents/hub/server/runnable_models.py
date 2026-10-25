"""The models of provider accounts a member may run in a project: GET /v1/projects/{project}/runnable-models, what the
dispatch form offers (``evo_agents.hub.gateway_runs``, ``docs/credentials.md``, section "Provider accounts").

Each model is named as a run asks for it, ``<prefix>/<model>``, with the runtimes that run it through the gateway and
the accounts that serve it: the caller's own provider accounts bound to the project and those shared with them for it
that a run of theirs may use now (``gateway_accounts.usable_accounts``, on any worker: live, not paused, not past
their end, a share that lends now). A model of none of them is not listed; a dispatch of one is 422, saying why.
Never a key: no query here reads one.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import gateway_runs, tables
from evo_agents.hub.credentials import PROVIDER_KIND
from evo_agents.hub.server import route_autonomy
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.gateway_accounts import Asker, usable_accounts
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import CurrentUser, Principal

router = APIRouter(
    prefix="/v1/projects",
    tags=["provider-accounts"],
    dependencies=[route_autonomy.K8],
    responses={401: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)


class RunnableModel(BaseModel):
    model: str = Field(description="as a run asks for it: <prefix>/<model>")
    prefix: str = Field(description="the catalog's alias of its provider, or evo-<id> for an endpoint of a member")
    runtimes: list[str] = Field(description="the runtimes that run it through the model gateway; any picks one")
    accounts: list[str] = Field(description="the names of the provider accounts that serve it, by name")


def _candidates(user: Principal, project_id: int):
    """The provider accounts that may serve the caller in the project: their own bound to it, and those shared for it."""
    s, p, b = tables.secrets, tables.provider_accounts, tables.secret_bindings
    cs, csp = tables.credential_shares, tables.credential_share_projects
    bound = select(b.c.secret_id).where(b.c.project_id == project_id)
    shared = (
        select(cs.c.secret_id)
        .join_from(cs, csp, csp.c.share_id == cs.c.id)
        .where(csp.c.project_id == project_id, cs.c.revoked_at.is_(None))
    )
    mine = (s.c.owner_id == user.user_id) & s.c.id.in_(bound)
    return (
        select(s.c.id, p.c.provider, p.c.models)
        .join_from(s, p, p.c.secret_id == s.c.id)
        .where(s.c.kind == PROVIDER_KIND, s.c.deleted_at.is_(None), or_(mine, s.c.id.in_(shared)))
    )


async def runnable(conn: AsyncConnection, user: Principal, project_id: int) -> list[RunnableModel]:
    """The models the caller may run in project ``project_id``, by name, as the module says."""
    rows = (await conn.execute(_candidates(user, project_id))).all()
    models = {row.id: [str(model) for model in row.models or []] for row in rows}
    prefixes = {row.id: gateway_runs.prefix_of(row.provider, row.id) for row in rows}
    asker = Asker(id=0, owner_id=user.user_id, principal=user.user_id, project_id=project_id)
    found: dict[str, RunnableModel] = {}
    for prefix in sorted({prefix for prefix in prefixes.values() if prefix is not None}):
        usable = await usable_accounts(conn, asker, None, prefix)
        for account in usable.accounts:
            for model in models.get(account.secret_id, []):
                name = gateway_runs.model_name(prefix, model)
                entry = found.setdefault(
                    name,
                    RunnableModel(model=name, prefix=prefix, runtimes=[*gateway_runs.GATEWAY_RUNTIMES], accounts=[]),
                )
                if account.name not in entry.accounts:
                    entry.accounts.append(account.name)
    return [found[name] for name in sorted(found)]


@router.get("/{project}/runnable-models", response_model=list[RunnableModel])
async def list_runnable_models(
    request: Request, project: ProjectName, user: CurrentUser, response: Response
) -> list[RunnableModel]:
    """The models of provider accounts the caller may run in ``project``, through the model gateway."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        found = await runnable(conn, user, access.project_id)
    response.headers.update(NO_STORE)
    return found

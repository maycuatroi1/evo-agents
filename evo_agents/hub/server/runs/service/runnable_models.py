"""The models of provider accounts a member may run in a project (``evo_agents.hub.server.runnable_models``)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import gateway_runs
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.gateway.models.accounts import Asker
from evo_agents.hub.server.gateway.service.accounts import usable_accounts
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.runs.models.runnable_models import RunnableModel
from evo_agents.hub.server.runs.repository import runnable_models as repository
from evo_agents.hub.server.security import Principal


async def runnable(conn: AsyncConnection, user: Principal, project_id: int) -> list[RunnableModel]:
    """The models the caller may run in project ``project_id``, by name, as ``runnable_models`` says."""
    rows = await repository.candidates(conn, user.user_id, project_id)
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


async def list_runnable(engine: AsyncEngine, user: Principal, project: str) -> list[RunnableModel]:
    """The models the caller may run in ``project``, in one transaction; 404 for a project they may not read."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        return await runnable(conn, user, access.project_id)

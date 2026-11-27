"""The owner's view of a run's leases (``evo_agents.hub.server.run_leases``): who may see them, and the name and kind
each lease shows."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub.credentials import HUB_KIND, KG_RUN, RUN_ACCESS, agent_lease_name, github_repo
from evo_agents.hub.gateway_runs import GATEWAY
from evo_agents.hub.server import kg_run_access
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.runs.models.run_leases import RunLease
from evo_agents.hub.server.runs.repository import run_leases as repository
from evo_agents.hub.server.runs.service.views import readable_run
from evo_agents.hub.server.security import Principal


def _token_name(target: str) -> str:
    """The name a GitHub token's lease was handed out with, github-app:<owner>, from the origins it answered for."""
    origins = target.split()
    found = github_repo(origins[0]) if origins else None
    return f"github-app:{found[0]}" if found else "github-app"


def _lease_name(row, owner: str, project: str) -> str:
    if row.provider == GATEWAY:
        return row.secret or f"{GATEWAY}:{row.target}"
    if row.provider == RUN_ACCESS and (row.token_host or "").startswith(f"{KG_RUN}:"):
        return kg_run_access.lease_name(project)
    if row.provider == RUN_ACCESS:
        return f"{RUN_ACCESS}:{owner}"
    if row.provider != "secret":
        return _token_name(row.target)
    return row.secret if row.agent is None else agent_lease_name(row.agent, row.secret)


def _lease_kind(row) -> str:
    if row.provider == GATEWAY:
        return GATEWAY
    if row.provider == RUN_ACCESS:
        return HUB_KIND
    return row.secret_kind if row.provider == "secret" else "git"


def _lease(row, owner: str, project: str) -> RunLease:
    return RunLease(
        id=row.id,
        name=_lease_name(row, owner, project),
        provider=row.provider,
        kind=_lease_kind(row),
        target=row.target,
        worker=row.worker,
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        profile=row.profile,
        profile_id=row.profile_id,
        profile_version=row.profile_version,
    )


async def run_credentials(
    engine: AsyncEngine, user: Principal, project: str, run_id: int, sink: str | None
) -> list[RunLease]:
    """Every lease run ``run_id`` of ``project`` got, given back or not, without a value: 404 for a run ``user`` may
    not read, 403 for one another member dispatched."""
    async with UnitOfWork(engine) as conn:
        await readable_run(conn, user, project, run_id, sink)
        owner_id, owner = await repository.run_owner(conn, run_id)
        if owner_id != user.user_id:
            raise HTTPException(403, f"only {owner}, who dispatched run {run_id}, sees the credentials it got")
        rows = await repository.run_leases(conn, run_id)
    return [_lease(row, owner, project) for row in rows]

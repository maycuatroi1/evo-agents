"""The routes of account pools, ``/v1/projects/{p}/account-pools``, which read and write them through
``evo_agents.hub.server.accounts.service.pools``; that module says what a pool is."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response

from evo_agents.hub.server.accounts.models.pools import (
    REFUSALS,
    AccountAdd,
    AccountPool,
    PoolName,
    PoolPolicy,
    PoolWrite,
)
from evo_agents.hub.server.accounts.service import pools as service
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["account pools"], responses={401: {"model": ErrorBody}})


@router.get("/{project}/account-pools", response_model=list[AccountPool], responses=REFUSALS)
async def list_account_pools(request: Request, project: ProjectName, user: CurrentUser) -> list[AccountPool]:
    """The account pools of the project, by name, each with its accounts and the agents that use it (an admin of the
    project or of the hub)."""
    return await service.list_pools(request.app.state.engine, user, project)


@router.post("/{project}/account-pools", response_model=AccountPool, status_code=201, responses=REFUSALS)
async def create_account_pool(
    request: Request, project: ProjectName, body: PoolWrite, user: CurrentUser
) -> AccountPool:
    """Make an empty account pool named ``name`` (an admin of the project or of the hub); 409 when the project has one
    of that name."""
    view, pool_id = await service.create(request.app.state.engine, user, project, body)
    log.info("account pool made", extra={"project": project, "pool_id": pool_id})
    return view


@router.get("/{project}/account-pools/{pool}", response_model=AccountPool, responses=REFUSALS)
async def get_account_pool(request: Request, project: ProjectName, pool: PoolName, user: CurrentUser) -> AccountPool:
    return await service.read(request.app.state.engine, user, project, pool)


@router.patch("/{project}/account-pools/{pool}", response_model=AccountPool, responses=REFUSALS)
async def set_pool_policy(
    request: Request, project: ProjectName, pool: PoolName, body: PoolPolicy, user: CurrentUser
) -> AccountPool:
    """Set the pool's strategy and the thresholds of quota-pacing (an admin of the project or of the hub); a field left
    out stays. The agents of the pool pick their accounts by it from their next choice on."""
    return await service.set_policy(request.app.state.engine, user, project, pool, body)


@router.delete("/{project}/account-pools/{pool}", status_code=204, responses=REFUSALS)
async def delete_account_pool(request: Request, project: ProjectName, pool: PoolName, user: CurrentUser) -> Response:
    """Delete a pool and its list of accounts (the accounts stay); 409 while an agent uses it."""
    await service.delete(request.app.state.engine, user, project, pool)
    return Response(status_code=204)


@router.put("/{project}/account-pools/{pool}/accounts", response_model=AccountPool, responses=REFUSALS)
async def add_account(
    request: Request, project: ProjectName, pool: PoolName, body: AccountAdd, user: CurrentUser
) -> AccountPool:
    """Add an account to the pool: an env account (a Claude account among them) or a provider account of yours, or of an
    agent of the project. 409 when the pool holds an account of that name already."""
    return await service.add(request.app.state.engine, user, project, pool, body)


@router.delete("/{project}/account-pools/{pool}/accounts/{secret_id}", response_model=AccountPool, responses=REFUSALS)
async def remove_account(
    request: Request, project: ProjectName, pool: PoolName, secret_id: int, user: CurrentUser
) -> AccountPool:
    """Take an account out of the pool; the account stays where it is. The runs that hold it keep it."""
    return await service.remove(request.app.state.engine, user, project, pool, secret_id)

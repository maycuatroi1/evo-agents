"""The routes of worker pools, ``/v1/worker-pools``: a member's pools and the pools of the agents they administer,
their settings and their join tokens (``service.pools``, ``service.pool_tokens``)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Path, Request, Response

from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser
from evo_agents.hub.server.workers.models.pools import (
    POOL_PATH_NAME,
    IssuedJoinToken,
    JoinToken,
    JoinTokenRequest,
    Pool,
    PoolRequest,
    PoolUpdate,
)
from evo_agents.hub.server.workers.service import pool_tokens, pools

REFUSALS: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    409: {"model": ErrorBody},
}

router = APIRouter(prefix="/v1/worker-pools", tags=["worker pools"], responses={401: {"model": ErrorBody}})

PoolName = Path(pattern=POOL_PATH_NAME, description="the pool's name")


@router.get("", response_model=list[Pool])
async def list_pools(request: Request, user: CurrentUser) -> list[Pool]:
    """The caller's pools, and the pools of the agents whose project the caller administers (every agent's for a hub
    admin), by name, each with its nodes that are not revoked."""
    return await pools.listed(request.app.state.engine, user)


@router.get("/{name}", response_model=Pool, responses={404: {"model": ErrorBody}})
async def show_pool(request: Request, user: CurrentUser, name: str = PoolName) -> Pool:
    """One of the caller's pools with its nodes that are not revoked."""
    return await pools.read_pool(request.app.state.engine, user, name)


@router.post("", status_code=201, response_model=Pool, responses={**REFUSALS, 422: {"model": ErrorBody}})
async def create_pool(request: Request, body: PoolRequest, user: CurrentUser) -> Pool:
    """A new pool of the caller's, without nodes until a machine joins with one of its join tokens."""
    return await pools.create(request.app.state.engine, user, body)


@router.patch("/{name}", response_model=Pool, responses={**REFUSALS, 422: {"model": ErrorBody}})
async def update_pool(request: Request, body: PoolUpdate, user: CurrentUser, name: str = PoolName) -> Pool:
    """Change the pool's settings; its nodes take them, and their daemons learn them from their next heartbeat."""
    return await pools.update(request.app.state.engine, user, name, body)


@router.delete("/{name}", status_code=204, responses=REFUSALS)
async def delete_pool(request: Request, user: CurrentUser, name: str = PoolName) -> Response:
    """Delete the caller's pool once it has no live node, with its join tokens; its revoked nodes stay revoked, in no
    pool. The pool of an agent goes with its agent alone."""
    pool_id = await pools.delete(request.app.state.engine, user, name)
    pools.log.info("worker pool deleted", extra={"pool_id": pool_id})
    return Response(status_code=204)


@router.post(
    "/{name}/tokens", status_code=201, response_model=IssuedJoinToken, responses={**REFUSALS, 422: {"model": ErrorBody}}
)
async def create_join_token(
    request: Request, body: JoinTokenRequest, user: CurrentUser, response: Response, name: str = PoolName
) -> IssuedJoinToken:
    """A join token of the pool, which machines join it with (`evo-agents worker join --token`); shown once."""
    issued = await pool_tokens.create(request.app.state.engine, user, name, body)
    response.headers.update(NO_STORE)
    return issued


@router.get("/{name}/tokens", response_model=list[JoinToken], responses={404: {"model": ErrorBody}})
async def list_join_tokens(request: Request, user: CurrentUser, name: str = PoolName) -> list[JoinToken]:
    """The join tokens of the pool, newest first, without the tokens themselves."""
    return await pool_tokens.listed(request.app.state.engine, user, name)


@router.post("/{name}/tokens/{token_id}/revoke", response_model=JoinToken, responses=REFUSALS)
async def revoke_join_token(
    request: Request, user: CurrentUser, name: str = PoolName, token_id: int = Path(ge=1, le=2**63 - 1)
) -> JoinToken:
    """Revoke a join token of the pool: no machine joins with it any more; the nodes that joined with it stay."""
    return await pool_tokens.revoke(request.app.state.engine, user, name, token_id)

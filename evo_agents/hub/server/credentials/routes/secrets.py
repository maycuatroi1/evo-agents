"""The routes of the owner's secrets, PUT, PATCH, GET and DELETE under /v1/secrets (``service.secrets``)."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.autonomy.service import guards as route_autonomy
from evo_agents.hub.server.credentials.models.secrets import (
    Secret,
    SecretName,
    SecretRebind,
    SecretWrite,
    SecretWritten,
)
from evo_agents.hub.server.credentials.service import secrets
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(
    prefix="/v1/secrets", tags=["secrets"], dependencies=[route_autonomy.K8], responses={401: {"model": ErrorBody}}
)

REFUSALS = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}


@router.get("", response_model=list[Secret])
async def list_secrets(request: Request, user: CurrentUser, response: Response) -> list[Secret]:
    """The caller's own secrets, by name, without their values; a hub admin's own too."""
    async with request.app.state.engine.begin() as conn:
        found = await secrets.owned_secrets(conn, user.user_id)
    response.headers.update(NO_STORE)
    return found


@router.put(
    "/{name}",
    response_model=SecretWritten,
    responses={**REFUSALS, 409: {"model": ErrorBody}, 422: {"model": ErrorBody}, 503: {"model": ErrorBody}},
)
async def put_secret(
    request: Request, name: SecretName, body: SecretWrite, user: CurrentUser, response: Response
) -> SecretWritten:
    """Create the caller's secret ``name``, or replace it whole; the value is sealed and never shown again."""
    sealer = secrets.sealer_of(request)
    write = secrets.checked_write(sealer, user, name, body)
    async with request.app.state.engine.begin() as conn:
        written = await secrets.put(conn, sealer, user, name, body, write)
    response.headers.update(NO_STORE)
    return written


@router.patch(
    "/{name}",
    response_model=Secret,
    responses={**REFUSALS, 409: {"model": ErrorBody}, 422: {"model": ErrorBody}},
)
async def rebind_secret(
    request: Request, name: SecretName, body: SecretRebind, user: CurrentUser, response: Response
) -> Secret:
    """Bind the caller's secret ``name`` to other workers or pools, or to any worker; its value and the rest stay."""
    workers, pools = secrets.rebinding(body)
    async with request.app.state.engine.begin() as conn:
        secret = await secrets.rebind(conn, user, name, workers, pools)
    response.headers.update(NO_STORE)
    return secret


@router.delete("/{name}", status_code=204, response_class=Response, responses=REFUSALS)  # pyright: ignore[reportArgumentType]
async def delete_secret(request: Request, name: SecretName, user: CurrentUser) -> Response:
    """Delete the caller's secret ``name``: its value and bindings go, and its leases are revoked."""
    async with request.app.state.engine.begin() as conn:
        await secrets.delete(conn, user, name)
    return Response(status_code=204)

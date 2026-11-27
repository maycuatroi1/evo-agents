"""The routes of the shares of secrets and accounts, GET and POST /v1/secrets/{name}/shares and DELETE
/v1/shares/{id} (``service.shares``)."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.credentials.models.shares import SecretName, ShareId, ShareWrite
from evo_agents.hub.server.credentials.service import shares
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser
from evo_agents.hub.server.share_views import CredentialShare

router = APIRouter(prefix="/v1", tags=["shares"], responses={401: {"model": ErrorBody}})

REFUSALS = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}
WRITE_REFUSALS = {**REFUSALS, 409: {"model": ErrorBody}, 422: {"model": ErrorBody}}


@router.get("/secrets/{name}/shares", response_model=list[CredentialShare], responses=REFUSALS)  # pyright: ignore[reportArgumentType]
async def list_shares(
    request: Request, name: SecretName, user: CurrentUser, response: Response
) -> list[CredentialShare]:
    """The shares of the caller's secret or account ``name``, revoked ones too, newest first."""
    async with request.app.state.engine.begin() as conn:
        found = await shares.listed(conn, user, name)
    response.headers.update(NO_STORE)
    return found


@router.post("/secrets/{name}/shares", status_code=201, response_model=CredentialShare, responses=WRITE_REFUSALS)  # pyright: ignore[reportArgumentType]
async def create_share(
    request: Request, name: SecretName, body: ShareWrite, user: CurrentUser, response: Response
) -> CredentialShare:
    """Share the caller's secret or account ``name``; a personal subscription token needs ``terms_ack``."""
    shares.checked(body)
    async with request.app.state.engine.begin() as conn:
        share = await shares.create(conn, user, name, body)
    response.headers.update(NO_STORE)
    return share


@router.delete("/shares/{share_id}", response_model=CredentialShare, responses={**REFUSALS, 409: {"model": ErrorBody}})
async def revoke_share(request: Request, share_id: ShareId, user: CurrentUser, response: Response) -> CredentialShare:
    """Revoke a share: its secret's owner may, and a hub admin may revoke any."""
    async with request.app.state.engine.begin() as conn:
        share = await shares.revoke(conn, user, share_id)
    response.headers.update(NO_STORE)
    return share

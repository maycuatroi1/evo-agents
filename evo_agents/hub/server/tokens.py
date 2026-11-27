"""The caller's own tokens: list them, revoke one.

The list shows live tokens (neither revoked nor expired) unless ``all`` is set, each with its kind, host, last use
and expiry, and marks the one the request came with. Revoking takes effect on the next request made with that
token, from any process, and adds an audit row. Another user's token is answered as unknown. Revoking the token of a
run that runs as its owner (kind run, ``run_access``) also stops the run running as them. Revoking a worker's
token revokes the worker too, in the same transaction and as POST /v1/workers/{id}/revoke does: its runs are
released and the name is free again, with a worker.revoke audit row besides the token.revoke one.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Request, Response

from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.identity.models.tokens import MAX_ID as MAX_ID
from evo_agents.hub.server.identity.models.tokens import TokenRow as TokenRow
from evo_agents.hub.server.identity.service import tokens as service
from evo_agents.hub.server.security import WEB, CurrentUser, delete_session_cookie

router = APIRouter(prefix="/v1/tokens", tags=["tokens"], responses={401: {"model": ErrorBody}})


@router.get("", response_model=list[TokenRow])
async def list_tokens(
    request: Request,
    user: CurrentUser,
    include_inactive: Annotated[bool, Query(alias="all", description="also list revoked and expired tokens")] = False,
) -> list[TokenRow]:
    return await service.list_tokens(request.app.state.engine, user, include_inactive)


@router.delete("/{token_id}", status_code=204, response_class=Response, responses={404: {"model": ErrorBody}})
async def revoke(request: Request, user: CurrentUser, token_id: Annotated[int, Path(ge=1, le=MAX_ID)]) -> Response:
    """Revoke one of the caller's tokens; a worker token's worker is revoked with it."""
    await service.revoke(request.app.state.engine, request.app.state, user, token_id)
    response = Response(status_code=204)
    if token_id == user.token_id and user.kind == WEB:
        delete_session_cookie(response)
    return response

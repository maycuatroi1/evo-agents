"""The caller's own tokens: list them, revoke one.

The list shows live tokens (neither revoked nor expired) unless ``all`` is set, each with its kind, host, last use
and expiry, and marks the one the request came with. Revoking takes effect on the next request made with that
token, from any process, and adds an audit row. Another user's token is answered as unknown.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel

from evo_agents.hub.server import audit
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import WEB, CurrentUser, delete_session_cookie, revoke_token

MAX_ID = 2**63 - 1  # bigint

router = APIRouter(prefix="/v1/tokens", tags=["tokens"], responses={401: {"model": ErrorBody}})


class TokenRow(BaseModel):
    id: int
    kind: Literal["machine", "web", "worker"]
    host: str | None
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime
    revoked_at: datetime | None
    state: Literal["active", "revoked", "expired"]
    current: bool  # the token this request came with


LIST = """
SELECT id, kind, host, created_at, last_used_at, expires_at, revoked_at,
       CASE WHEN revoked_at IS NOT NULL THEN 'revoked' WHEN expires_at <= now() THEN 'expired' ELSE 'active' END
  FROM tokens
 WHERE user_id = %s AND (%s OR (revoked_at IS NULL AND expires_at > now()))
 ORDER BY created_at DESC, id DESC
"""
FIELDS = ("id", "kind", "host", "created_at", "last_used_at", "expires_at", "revoked_at", "state")


@router.get("", response_model=list[TokenRow])
async def list_tokens(
    request: Request,
    user: CurrentUser,
    include_inactive: Annotated[bool, Query(alias="all", description="also list revoked and expired tokens")] = False,
) -> list[TokenRow]:
    async with request.app.state.pool.connection() as conn:
        rows = await (await conn.execute(LIST, (user.user_id, include_inactive))).fetchall()
    return [TokenRow(**dict(zip(FIELDS, row, strict=True)), current=row[0] == user.token_id) for row in rows]


@router.delete("/{token_id}", status_code=204, response_class=Response, responses={404: {"model": ErrorBody}})
async def revoke(request: Request, user: CurrentUser, token_id: Annotated[int, Path(ge=1, le=MAX_ID)]) -> Response:
    async with request.app.state.pool.connection() as conn:
        if not await revoke_token(conn, token_id, user.user_id):
            raise HTTPException(404, f"you have no unrevoked token {token_id}: see `evo-agents hub token list`")
        target = audit.token_target(token_id)
        action = audit.TOKEN_REVOKE
        await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target)
    response = Response(status_code=204)
    if token_id == user.token_id and user.kind == WEB:
        delete_session_cookie(response)
    return response

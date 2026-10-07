"""The caller's own tokens: list them, revoke one.

The list shows live tokens (neither revoked nor expired) unless ``all`` is set, each with its kind, host, last use
and expiry, and marks the one the request came with. Revoking takes effect on the next request made with that
token, from any process, and adds an audit row. Another user's token is answered as unknown. Revoking a worker's
token revokes the worker too, in the same transaction and as POST /v1/workers/{id}/revoke does: its runs are
released and the name is free again, with a worker.revoke audit row besides the token.revoke one.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import case, func, select

from evo_agents.hub import tables
from evo_agents.hub.server import audit, workers
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import WEB, CurrentUser, delete_session_cookie, revoke_token

log = logging.getLogger(__name__)

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


def _listed(user_id: int, include_inactive: bool):
    """The tokens of ``user_id``, newest first; only the live ones unless ``include_inactive``."""
    tokens = tables.tokens
    state = case(
        (tokens.c.revoked_at.is_not(None), "revoked"),
        (tokens.c.expires_at <= func.now(), "expired"),
        else_="active",
    )
    query = select(
        tokens.c.id,
        tokens.c.kind,
        tokens.c.host,
        tokens.c.created_at,
        tokens.c.last_used_at,
        tokens.c.expires_at,
        tokens.c.revoked_at,
        state.label("state"),
    ).where(tokens.c.user_id == user_id)
    if not include_inactive:
        query = query.where(tokens.c.revoked_at.is_(None), tokens.c.expires_at > func.now())
    return query.order_by(tokens.c.created_at.desc(), tokens.c.id.desc())


@router.get("", response_model=list[TokenRow])
async def list_tokens(
    request: Request,
    user: CurrentUser,
    include_inactive: Annotated[bool, Query(alias="all", description="also list revoked and expired tokens")] = False,
) -> list[TokenRow]:
    async with request.app.state.engine.begin() as conn:
        rows = (await conn.execute(_listed(user.user_id, include_inactive))).all()
    return [TokenRow(**row._mapping, current=row.id == user.token_id) for row in rows]


@router.delete("/{token_id}", status_code=204, response_class=Response, responses={404: {"model": ErrorBody}})
async def revoke(request: Request, user: CurrentUser, token_id: Annotated[int, Path(ge=1, le=MAX_ID)]) -> Response:
    """Revoke one of the caller's tokens; a worker token's worker is revoked with it."""
    async with request.app.state.engine.begin() as conn:
        worker = await workers.lock_worker_of_token(conn, token_id, user.user_id)  # before the token's row
        if not await revoke_token(conn, token_id, user.user_id):
            raise HTTPException(404, f"you have no unrevoked token {token_id}: see `evo-agents hub token list`")
        target = audit.token_target(token_id)
        action = audit.TOKEN_REVOKE
        await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target)
        released = await workers.end_worker(conn, user, *worker, token_id) if worker else 0
    if worker:
        log.info("worker revoked", extra={"worker_id": worker[0], "by": user.login, "runs_released": released})
        await workers.revoke_leased_tokens(request.app.state, worker[0])
    response = Response(status_code=204)
    if token_id == user.token_id and user.kind == WEB:
        delete_session_cookie(response)
    return response

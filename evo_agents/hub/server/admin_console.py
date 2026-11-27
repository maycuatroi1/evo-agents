"""What the admin area of the web reads and changes beyond ``admin``: the audit trail, every user's tokens, and
revoking any one of them. Hub admins only, like every route under /v1/admin; a member gets 403 from the API
whatever the web shows.

GET /v1/admin/audit filters the trail by actor (a login), action, project and a half-open time range
[since, until), newest first. GET /v1/admin/tokens lists the tokens and web sessions of every user, live ones
unless ``state`` says otherwise: ``expiring`` lists the live ones that expire within TOKEN_EXPIRING_DAYS and
``unused`` those not revoked and unused for TOKEN_UNUSED_DAYS, the tokens GET /v1/admin/overview counts. Both page
by cursor: a page holds at most ``limit`` rows, and ``next_cursor``, passed back as ``cursor`` with the same
filters, gives the rows right after it, so pages never overlap or skip a row even while new rows arrive. A cursor is
the sort key of the last row shown (time and id) in base64url; it names no content.

DELETE /v1/admin/tokens/{id} revokes one token of anyone. The next request made with it gets 401, from any
process. It answers 404 for an id the hub never issued and 409 for a token revoked already, and adds an audit row
naming the token and its owner, never the token itself. An admin revoking the web session they are using is
signed out, as with DELETE /v1/tokens/{id}. A worker's token takes its worker with it, as DELETE /v1/tokens/{id}
does: the worker is revoked in the same transaction, its runs released, with a worker.revoke audit row.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from pydantic import AwareDatetime

from evo_agents.hub.server.base.models import LOGIN_NAME, PROJECT_NAME
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.hub_admin.models.admin import TOKEN_EXPIRING_DAYS, TOKEN_UNUSED_DAYS
from evo_agents.hub.server.hub_admin.models.admin_console import ACTION_NAME as ACTION_NAME
from evo_agents.hub.server.hub_admin.models.admin_console import CURSOR_HELP as CURSOR_HELP
from evo_agents.hub.server.hub_admin.models.admin_console import DEFAULT_LIMIT as DEFAULT_LIMIT
from evo_agents.hub.server.hub_admin.models.admin_console import INVALID as INVALID
from evo_agents.hub.server.hub_admin.models.admin_console import MAX_ID as MAX_ID
from evo_agents.hub.server.hub_admin.models.admin_console import MAX_LIMIT as MAX_LIMIT
from evo_agents.hub.server.hub_admin.models.admin_console import AdminToken as AdminToken
from evo_agents.hub.server.hub_admin.models.admin_console import AuditPage as AuditPage
from evo_agents.hub.server.hub_admin.models.admin_console import AuditRow as AuditRow
from evo_agents.hub.server.hub_admin.models.admin_console import TokenFilter as TokenFilter
from evo_agents.hub.server.hub_admin.models.admin_console import TokenPage as TokenPage
from evo_agents.hub.server.hub_admin.models.admin_console import TokenState as TokenState
from evo_agents.hub.server.hub_admin.repository.admin_console import TOKEN_FILTERS as TOKEN_FILTERS
from evo_agents.hub.server.hub_admin.service import admin_console as service
from evo_agents.hub.server.hub_admin.service.admin_console import decode_cursor as decode_cursor
from evo_agents.hub.server.hub_admin.service.admin_console import encode_cursor as encode_cursor
from evo_agents.hub.server.security import WEB, AdminUser, admin, delete_session_cookie
from evo_agents.hub.server.workers.service import workers

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/admin",
    tags=["admin"],
    dependencies=[Depends(admin)],
    responses={401: {"model": ErrorBody}, 403: {"model": ErrorBody}},
)

Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT, description="rows per page")]
CursorParam = Annotated[str | None, Query(min_length=1, max_length=200, description=CURSOR_HELP)]


@router.get("/audit", response_model=AuditPage, responses=INVALID)
async def audit_trail(
    request: Request,
    actor: Annotated[str | None, Query(pattern=LOGIN_NAME, description="a login, any case")] = None,
    action: Annotated[str | None, Query(pattern=ACTION_NAME, description="an action, such as grant.put")] = None,
    project: Annotated[str | None, Query(pattern=PROJECT_NAME)] = None,
    since: Annotated[AwareDatetime | None, Query(description="rows at or after this time")] = None,
    until: Annotated[AwareDatetime | None, Query(description="rows before this time")] = None,
    cursor: CursorParam = None,
    limit: Limit = DEFAULT_LIMIT,
) -> AuditPage:
    """The audit trail, newest first, filtered and paged by cursor."""
    return await service.audit_page(request.app.state.engine, actor, action, project, since, until, cursor, limit)


@router.get("/audit/actions", response_model=list[str])
async def audit_actions(request: Request) -> list[str]:
    """Every action the trail holds at least one row of, in alphabetical order, for the action filter."""
    return await service.audit_actions(request.app.state.engine)


@router.get("/tokens", response_model=TokenPage, responses=INVALID)
async def all_tokens(
    request: Request,
    user: AdminUser,
    login: Annotated[str | None, Query(pattern=LOGIN_NAME, description="one user's tokens, any case")] = None,
    kind: Literal["machine", "web", "worker", "run"] | None = None,
    state: Annotated[
        TokenFilter,
        Query(
            description=f"live tokens unless set; expiring: live ones expiring within {TOKEN_EXPIRING_DAYS} days; "
            f"unused: not revoked and unused for {TOKEN_UNUSED_DAYS} days"
        ),
    ] = "active",
    cursor: CursorParam = None,
    limit: Limit = DEFAULT_LIMIT,
) -> TokenPage:
    """The tokens and web sessions of every user, newest first, paged by cursor."""
    engine = request.app.state.engine
    return await service.token_page(engine, user, login, kind, state, cursor, limit)


@router.delete(
    "/tokens/{token_id}",
    status_code=204,
    response_class=Response,
    responses={404: {"model": ErrorBody}, 409: {"model": ErrorBody}},
)
async def revoke_any_token(
    request: Request, user: AdminUser, token_id: Annotated[int, Path(ge=1, le=MAX_ID)]
) -> Response:
    """Revoke a token or web session of any user; a worker token's worker is revoked with it."""
    worker, released = await service.revoke_any_token(request.app.state.engine, user, token_id)
    if worker:
        log.info("worker revoked", extra={"worker_id": worker[0], "by": user.login, "runs_released": released})
        await workers.revoke_leased_tokens(request.app.state, worker[0])
    response = Response(status_code=204)
    if token_id == user.token_id and user.kind == WEB:
        delete_session_cookie(response)
    return response

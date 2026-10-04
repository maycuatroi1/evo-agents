"""What the admin area of the web reads and changes beyond ``admin``: the audit trail, every user's tokens, and
revoking any one of them. Hub admins only, like every route under /v1/admin; a member gets 403 from the API
whatever the web shows.

GET /v1/admin/audit filters the trail by actor (a login), action, project and a half-open time range
[since, until), newest first. GET /v1/admin/tokens lists the tokens and web sessions of every user, live ones
unless ``state`` says otherwise. Both pages by cursor: a page holds at most ``limit`` rows, and ``next_cursor``,
passed back as ``cursor`` with the same filters, gives the rows right after it, so pages never overlap or skip a
row even while new rows arrive. A cursor is the sort key of the last row shown (time and id) in base64url; it
names no content.

DELETE /v1/admin/tokens/{id} revokes one token of anyone. The next request made with it gets 401, from any
process. It answers 404 for an id the hub never issued and 409 for a token revoked already, and adds an audit row
naming the token and its owner, never the token itself. An admin revoking the web session they are using is
signed out, as with DELETE /v1/tokens/{id}.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from pydantic import AwareDatetime, BaseModel, Field

from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import LOGIN_NAME, PROJECT_NAME
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import WEB, AdminUser, admin, delete_session_cookie

MAX_ID = 2**63 - 1  # bigint
MAX_LIMIT = 200
DEFAULT_LIMIT = 50
ACTION_NAME = r"^[a-z][a-z0-9_.-]{0,99}$"  # as audit's CHECK accepts it
CURSOR_HELP = "next_cursor of the previous page, with the same filters"
INVALID = {422: {"model": ErrorBody}}

router = APIRouter(
    prefix="/v1/admin",
    tags=["admin"],
    dependencies=[Depends(admin)],
    responses={401: {"model": ErrorBody}, 403: {"model": ErrorBody}},
)

Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT, description="rows per page")]
CursorParam = Annotated[str | None, Query(min_length=1, max_length=200, description=CURSOR_HELP)]


def encode_cursor(at: datetime, row_id: int) -> str:
    return base64.urlsafe_b64encode(f"{at.isoformat()}|{row_id}".encode()).rstrip(b"=").decode()


def decode_cursor(value: str) -> tuple[datetime, int]:
    """The sort key a cursor holds; 422 for anything ``encode_cursor`` did not write."""
    try:
        text = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        at_text, _, id_text = text.partition("|")
        at, row_id = datetime.fromisoformat(at_text), int(id_text)
    except (ValueError, UnicodeError, binascii.Error):
        at, row_id = None, 0
    if at is None or at.tzinfo is None or not 1 <= row_id <= MAX_ID:
        raise HTTPException(422, "cursor is not one this hub handed out: start again without it")
    return at, row_id


def _check_range(since: datetime | None, until: datetime | None) -> None:
    if since is not None and until is not None and since >= until:
        raise HTTPException(422, "since must be earlier than until")


class AuditRow(BaseModel):
    id: int
    at: datetime
    actor: str | None = Field(description="the login that acted; null for an action of the hub itself")
    token_id: int | None = Field(description="the token or web session the actor used")
    action: str
    target: str = Field(description="what the action named: a project, a login, a token id; never content")
    project: str | None = Field(description="the project the action happened in; null for one outside any project")


class AuditPage(BaseModel):
    items: list[AuditRow]
    next_cursor: str | None = Field(description="pass as cursor for the rows after these; null on the last page")


AUDIT = """
SELECT a.id, a.at, u.login, a.token_id, a.action, a.target, p.name
  FROM audit a LEFT JOIN users u ON u.id = a.actor_id LEFT JOIN projects p ON p.id = a.project_id
 WHERE {where}
 ORDER BY a.at DESC, a.id DESC
 LIMIT %(limit)s
"""
AUDIT_FILTERS = {
    "actor": "a.actor_id = (SELECT id FROM users WHERE lower(login) = lower(%(actor)s))",
    "action": "a.action = %(action)s",
    "project": "a.project_id = (SELECT id FROM projects WHERE name = %(project)s)",
    "since": "a.at >= %(since)s",
    "until": "a.at < %(until)s",
    "after": "(a.at, a.id) < (%(after_at)s, %(after_id)s)",
}
# Every distinct action without reading the whole table: one index probe per action (audit_action_idx, schema 0007).
ACTIONS = """
WITH RECURSIVE found(action) AS (
    (SELECT action FROM audit ORDER BY action LIMIT 1)
    UNION ALL
    SELECT (SELECT a.action FROM audit a WHERE a.action > found.action ORDER BY a.action LIMIT 1)
      FROM found WHERE found.action IS NOT NULL
)
SELECT action FROM found WHERE action IS NOT NULL
"""


def _where(filters: dict[str, str], params: dict) -> str:
    return " AND ".join(condition for name, condition in filters.items() if params.get(name) is not None) or "TRUE"


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
    _check_range(since, until)
    after_at, after_id = decode_cursor(cursor) if cursor else (None, None)
    params = {"actor": actor, "action": action, "project": project, "since": since, "until": until}
    params |= {"after": after_at, "after_at": after_at, "after_id": after_id, "limit": limit + 1}
    async with request.app.state.pool.connection() as conn:
        rows = await (await conn.execute(AUDIT.format(where=_where(AUDIT_FILTERS, params)), params)).fetchall()
    items = [
        AuditRow(id=row_id, at=at, actor=login, token_id=token_id, action=name, target=target, project=project_name)
        for row_id, at, login, token_id, name, target, project_name in rows[:limit]
    ]
    more = len(rows) > limit
    return AuditPage(items=items, next_cursor=encode_cursor(items[-1].at, items[-1].id) if more else None)


@router.get("/audit/actions", response_model=list[str])
async def audit_actions(request: Request) -> list[str]:
    """Every action the trail holds at least one row of, in alphabetical order, for the action filter."""
    async with request.app.state.pool.connection() as conn:
        return [row[0] for row in await (await conn.execute(ACTIONS)).fetchall()]


TokenState = Literal["active", "revoked", "expired"]


class AdminToken(BaseModel):
    id: int
    login: str = Field(description="whose token it is")
    kind: Literal["machine", "web"]
    host: str | None = Field(description="the machine a machine token was issued to; null for a web session")
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime
    revoked_at: datetime | None
    state: TokenState
    current: bool = Field(description="the token this request came with")


class TokenPage(BaseModel):
    items: list[AdminToken]
    next_cursor: str | None = Field(description="pass as cursor for the tokens after these; null on the last page")


STATE = "CASE WHEN t.revoked_at IS NOT NULL THEN 'revoked' WHEN t.expires_at <= now() THEN 'expired' ELSE 'active' END"
TOKENS = f"""
SELECT t.id, u.login, t.kind, t.host, t.created_at, t.last_used_at, t.expires_at, t.revoked_at, {STATE}
  FROM tokens t JOIN users u ON u.id = t.user_id
 WHERE {{where}}
 ORDER BY t.created_at DESC, t.id DESC
 LIMIT %(limit)s
"""
TOKEN_FILTERS = {
    "login": "lower(u.login) = lower(%(login)s)",
    "kind": "t.kind = %(kind)s",
    "active": "t.revoked_at IS NULL AND t.expires_at > now()",
    "revoked": "t.revoked_at IS NOT NULL",
    "expired": "t.revoked_at IS NULL AND t.expires_at <= now()",
    "after": "(t.created_at, t.id) < (%(after_at)s, %(after_id)s)",
}
REVOKE = """
UPDATE tokens t SET revoked_at = now() FROM users u
 WHERE t.id = %s AND u.id = t.user_id AND t.revoked_at IS NULL
RETURNING u.login
"""


@router.get("/tokens", response_model=TokenPage, responses=INVALID)
async def all_tokens(
    request: Request,
    user: AdminUser,
    login: Annotated[str | None, Query(pattern=LOGIN_NAME, description="one user's tokens, any case")] = None,
    kind: Literal["machine", "web"] | None = None,
    state: Annotated[TokenState | Literal["any"], Query(description="live tokens unless set")] = "active",
    cursor: CursorParam = None,
    limit: Limit = DEFAULT_LIMIT,
) -> TokenPage:
    """The tokens and web sessions of every user, newest first, paged by cursor."""
    after_at, after_id = decode_cursor(cursor) if cursor else (None, None)
    params = {"login": login, "kind": kind, state: True, "after": after_at, "after_at": after_at, "after_id": after_id}
    params["limit"] = limit + 1
    async with request.app.state.pool.connection() as conn:
        rows = await (await conn.execute(TOKENS.format(where=_where(TOKEN_FILTERS, params)), params)).fetchall()
    fields = ("id", "login", "kind", "host", "created_at", "last_used_at", "expires_at", "revoked_at", "state")
    items = [AdminToken(**dict(zip(fields, row, strict=True)), current=row[0] == user.token_id) for row in rows[:limit]]
    more = len(rows) > limit
    return TokenPage(items=items, next_cursor=encode_cursor(items[-1].created_at, items[-1].id) if more else None)


@router.delete(
    "/tokens/{token_id}",
    status_code=204,
    response_class=Response,
    responses={404: {"model": ErrorBody}, 409: {"model": ErrorBody}},
)
async def revoke_any_token(
    request: Request, user: AdminUser, token_id: Annotated[int, Path(ge=1, le=MAX_ID)]
) -> Response:
    """Revoke a token or web session of any user."""
    async with request.app.state.pool.connection() as conn:
        row = await (await conn.execute(REVOKE, (token_id,))).fetchone()
        if row is None:
            found = await (await conn.execute("SELECT revoked_at FROM tokens WHERE id = %s", (token_id,))).fetchone()
            if found is None:
                raise HTTPException(404, f"this hub never issued token {token_id}")
            raise HTTPException(409, f"token {token_id} was revoked already, at {found[0].isoformat()}")
        target = f"{audit.token_target(token_id)} login={row[0]}"
        action = audit.TOKEN_REVOKE
        await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target)
    response = Response(status_code=204)
    if token_id == user.token_id and user.kind == WEB:
        delete_session_cookie(response)
    return response

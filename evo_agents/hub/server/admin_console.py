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

import base64
import binascii
import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import and_, case, func, literal, select, tuple_, update

from evo_agents.hub import tables
from evo_agents.hub.server import audit, workers
from evo_agents.hub.server.admin import (
    LOGIN_NAME,
    PROJECT_NAME,
    TOKEN_EXPIRING,
    TOKEN_EXPIRING_DAYS,
    TOKEN_LIVE,
    TOKEN_UNUSED,
    TOKEN_UNUSED_DAYS,
)
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import WEB, AdminUser, admin, delete_session_cookie

log = logging.getLogger(__name__)

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


def _key(after: tuple[datetime, int], at, row_id):
    """The sort key of a cursor, bound with the types of its columns (an id may need all of bigint)."""
    return tuple_(literal(after[0], at.type), literal(after[1], row_id.type))


def _trail(actor, action, project, since, until, after: tuple[datetime, int] | None, limit: int):
    """The audit rows the filters leave, newest first, at most ``limit``; only those after the sort key ``after``."""
    trail, users, projects = tables.audit, tables.users, tables.projects
    query = select(
        trail.c.id,
        trail.c.at,
        users.c.login.label("actor"),
        trail.c.token_id,
        trail.c.action,
        trail.c.target,
        projects.c.name.label("project"),
    ).select_from(
        trail.outerjoin(users, users.c.id == trail.c.actor_id).outerjoin(projects, projects.c.id == trail.c.project_id)
    )
    if actor is not None:
        named = users.alias("named_user")  # the actor's row, apart from the users joined for every row's login
        actor_id = select(named.c.id).where(func.lower(named.c.login) == func.lower(actor)).scalar_subquery()
        query = query.where(trail.c.actor_id == actor_id)
    if action is not None:
        query = query.where(trail.c.action == action)
    if project is not None:
        named = projects.alias("named_project")
        project_id = select(named.c.id).where(named.c.name == project).scalar_subquery()
        query = query.where(trail.c.project_id == project_id)
    if since is not None:
        query = query.where(trail.c.at >= since)
    if until is not None:
        query = query.where(trail.c.at < until)
    if after is not None:
        query = query.where(tuple_(trail.c.at, trail.c.id) < _key(after, trail.c.at, trail.c.id))
    return query.order_by(trail.c.at.desc(), trail.c.id.desc()).limit(limit)


def _actions():
    """Every distinct action without reading the whole table: one index probe per action (audit_action_idx, schema
    0007). A recursive CTE starts from the first action and each step takes the next one after it."""
    trail = tables.audit
    first = select(trail.c.action).order_by(trail.c.action).limit(1)
    found = first.cte("found", recursive=True)
    later = trail.alias("a")
    following = (
        select(later.c.action)
        .where(later.c.action > found.c.action)
        .order_by(later.c.action)
        .limit(1)
        .scalar_subquery()
    )
    found = found.union_all(select(following).where(found.c.action.is_not(None)))
    return select(found.c.action).where(found.c.action.is_not(None))


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
    after = decode_cursor(cursor) if cursor else None
    async with request.app.state.engine.begin() as conn:
        rows = (await conn.execute(_trail(actor, action, project, since, until, after, limit + 1))).all()
    items = [AuditRow(**row._mapping) for row in rows[:limit]]
    more = len(rows) > limit
    return AuditPage(items=items, next_cursor=encode_cursor(items[-1].at, items[-1].id) if more else None)


@router.get("/audit/actions", response_model=list[str])
async def audit_actions(request: Request) -> list[str]:
    """Every action the trail holds at least one row of, in alphabetical order, for the action filter."""
    async with request.app.state.engine.begin() as conn:
        return list((await conn.execute(_actions())).scalars())


TokenState = Literal["active", "revoked", "expired"]
# What ``state`` of GET /v1/admin/tokens may ask for besides a token's own state, and ``any``.
TokenFilter = Literal["active", "revoked", "expired", "expiring", "unused", "any"]


class AdminToken(BaseModel):
    id: int
    login: str = Field(description="whose token it is")
    kind: Literal["machine", "web", "worker"]
    host: str | None = Field(
        description="the machine a machine token or worker token was issued to; null for a web session"
    )
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime
    revoked_at: datetime | None
    state: TokenState
    current: bool = Field(description="the token this request came with")


class TokenPage(BaseModel):
    items: list[AdminToken]
    next_cursor: str | None = Field(description="pass as cursor for the tokens after these; null on the last page")


_tokens = tables.tokens
# What each ``state`` of GET /v1/admin/tokens keeps; ``any`` keeps every token.
TOKEN_FILTERS = {
    "active": TOKEN_LIVE,
    "revoked": _tokens.c.revoked_at.is_not(None),
    "expired": and_(_tokens.c.revoked_at.is_(None), _tokens.c.expires_at <= func.now()),
    "expiring": TOKEN_EXPIRING,
    "unused": TOKEN_UNUSED,
}


def _listed(login, kind, state: str, after: tuple[datetime, int] | None, limit: int):
    """The tokens of every user the filters leave, newest first, at most ``limit``; only those after ``after``."""
    tokens, users = tables.tokens, tables.users
    token_state = case(
        (tokens.c.revoked_at.is_not(None), "revoked"),
        (tokens.c.expires_at <= func.now(), "expired"),
        else_="active",
    )
    query = select(
        tokens.c.id,
        users.c.login,
        tokens.c.kind,
        tokens.c.host,
        tokens.c.created_at,
        tokens.c.last_used_at,
        tokens.c.expires_at,
        tokens.c.revoked_at,
        token_state.label("state"),
    ).join_from(tokens, users, users.c.id == tokens.c.user_id)
    if login is not None:
        query = query.where(func.lower(users.c.login) == func.lower(login))
    if kind is not None:
        query = query.where(tokens.c.kind == kind)
    if state in TOKEN_FILTERS:
        query = query.where(TOKEN_FILTERS[state])
    if after is not None:
        query = query.where(tuple_(tokens.c.created_at, tokens.c.id) < _key(after, tokens.c.created_at, tokens.c.id))
    return query.order_by(tokens.c.created_at.desc(), tokens.c.id.desc()).limit(limit)


@router.get("/tokens", response_model=TokenPage, responses=INVALID)
async def all_tokens(
    request: Request,
    user: AdminUser,
    login: Annotated[str | None, Query(pattern=LOGIN_NAME, description="one user's tokens, any case")] = None,
    kind: Literal["machine", "web", "worker"] | None = None,
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
    after = decode_cursor(cursor) if cursor else None
    async with request.app.state.engine.begin() as conn:
        rows = (await conn.execute(_listed(login, kind, state, after, limit + 1))).all()
    items = [AdminToken(**row._mapping, current=row.id == user.token_id) for row in rows[:limit]]
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
    """Revoke a token or web session of any user; a worker token's worker is revoked with it."""
    async with request.app.state.engine.begin() as conn:
        worker = await workers.lock_worker_of_token(conn, token_id)  # before the token's row
        tokens, users = tables.tokens, tables.users
        revoked = await conn.execute(
            update(tokens)
            .values(revoked_at=func.now())
            .where(tokens.c.id == token_id, users.c.id == tokens.c.user_id, tokens.c.revoked_at.is_(None))
            .returning(users.c.login)
        )
        owner = revoked.scalar()
        if owner is None:
            found = await conn.execute(select(tokens.c.revoked_at).where(tokens.c.id == token_id))
            row = found.first()
            if row is None:
                raise HTTPException(404, f"this hub never issued token {token_id}")
            raise HTTPException(409, f"token {token_id} was revoked already, at {row.revoked_at.isoformat()}")
        target = f"{audit.token_target(token_id)} login={owner}"
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

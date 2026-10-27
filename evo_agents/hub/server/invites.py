"""Invite links: a hub admin makes a one-time link that gives a grant on a project, or the right to register one new
project, and a signed-in member accepts it.

POST /v1/admin/invites makes a link of kind ``join``, naming a project, a role and a max level of the project's ladder
as ``PUT /v1/admin/projects/{project}/grants/{login}`` takes them, or of kind ``register``, naming none. It lasts
``days``, DEFAULT_DAYS unless asked, MAX_DAYS at most, and ``for_login`` binds it to the one GitHub login that may
accept it, so a bound link can be posted where anyone reads it. Its token is ``evn_`` and 32 random bytes in base64url;
the hub keeps the token's hex SHA-256 (``security.hash_token``) and answers the token and the link, EVO_HUB_PUBLIC_URL
+ PAGE + the token, in that response alone. GET /v1/admin/invites lists the links, newest first, each with its status:
open, accepted, revoked or expired. POST /v1/admin/invites/{invite_id}/revoke revokes an open link, or the right an
accepted register link gave that no registration has used yet; any other link is 409. Those three routes are a hub
admin's alone: anyone else gets 403.

POST /v1/invites/preview is public: the web's page of a link reads, from the token alone, what the link gives, so a
visitor who has not signed in yet sees who invites them and to what. It names the admin who made the link, its kind,
status and expiry, and whether it is bound to a login, never which one; the project, role and max level only to a
visitor the link is for while it is open (anyone for an unbound link, the bound login alone, signed in, for a bound
one), and to the member who accepted it. It reads the session cookie when there is one, to say who is signed in, and
writes nothing.

POST /v1/invites/accept takes the token from a signed-in member, never from the agent of a run. The link's row is
locked first, so of two accepts at once one gives what the link gives and the other finds it accepted. A token the hub
does not know is 404; a link accepted, revoked or expired is 410, and one bound to another login 403, each saying why
and giving nothing. A join link gives its grant as a hub admin's grant does, except that it never lowers one the
member holds: the role and the max level are the higher of the held and the invited. A register link records the
right, which the member's next registration of a new project spends (``evo_agents.hub.server.projects``): in that
transaction the project is created, the member gets the admin role at the project's highest level
(``spend_register_right``), and the right is marked used. The right does not expire with the link; a hub admin revokes
it.

Making, accepting and revoking a link each write one audit row naming the link, "invite:<id> kind=<kind>", with the
project, role and max level of a join link and the login it is bound to, never the token; a join link's rows are in
its project.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import case, func, insert, select, update
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.access import ROLES
from evo_agents.hub.config import HubConfig
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import LOGIN_NAME, PROJECT_NAME, project_ladder, upsert_grant
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.paging import PAGED, WHOLE, Paging
from evo_agents.hub.server.security import (
    SESSION_COOKIE,
    WEB,
    AdminUser,
    CurrentUser,
    Principal,
    admin,
    authenticate,
    hash_token,
)

log = logging.getLogger(__name__)

PREFIX = "evn_"
TOKEN_BYTES = 32  # 256 random bits
DEFAULT_DAYS = 7
MAX_DAYS = 30
PAGE = "/invite/"  # the web's page of a link: EVO_HUB_PUBLIC_URL + PAGE + its token
JOIN, REGISTER = "join", "register"
OPEN, ACCEPTED, REVOKED, EXPIRED = "open", "accepted", "revoked", "expired"
ASK_AGAIN = "ask a hub admin for a new link; nothing was granted"

InviteId = Annotated[int, Path(ge=1, le=2**63 - 1)]
Role = Literal["reader", "writer", "admin"]
Status = Literal["open", "accepted", "revoked", "expired"]

router = APIRouter(
    prefix="/v1/admin/invites",
    tags=["admin"],
    dependencies=[Depends(admin)],
    responses={401: {"model": ErrorBody}, 403: {"model": ErrorBody}},
)
accept_router = APIRouter(prefix="/v1/invites", tags=["invites"], responses={401: {"model": ErrorBody}})


class InviteRequest(BaseModel):
    kind: Literal["join", "register"] = Field(
        description="join: a grant on an existing project; register: the right to register one new project"
    )
    project: str | None = Field(None, pattern=PROJECT_NAME, description="join: the project the link grants on")
    role: Role | None = Field(None, description="join: the role it gives")
    max_level: str | None = Field(
        None, min_length=1, max_length=100, description="join: the highest level of the project's ladder it reaches"
    )
    for_login: str | None = Field(
        None,
        pattern=LOGIN_NAME,
        description="the one GitHub login that may accept the link; anyone signed in if left out",
    )
    days: int = Field(DEFAULT_DAYS, ge=1, le=MAX_DAYS, description="how long the link stays open")

    @model_validator(mode="after")
    def _fields_of_the_kind(self) -> InviteRequest:
        named = [self.project, self.role, self.max_level]
        if self.kind == JOIN and None in named:
            raise ValueError("a join link names a project, a role and a max level")
        if self.kind == REGISTER and any(value is not None for value in named):
            raise ValueError("a register link names no project, role or max level")
        return self


class InviteRow(BaseModel):
    id: int
    kind: Literal["join", "register"]
    project: str | None = Field(
        description="join: the project the link grants on; register: the project its right registered, once used"
    )
    role: Role | None = Field(description="join: the role it gives")
    max_level: str | None = Field(description="join: the highest level it reaches")
    for_login: str | None = Field(description="the one login that may accept it; null for anyone signed in")
    created_by: str = Field(description="the hub admin who made it")
    created_at: datetime
    expires_at: datetime = Field(description="after this the link can no longer be accepted")
    status: Status = Field(description="revoked, else accepted, else expired once expires_at is past, else open")
    accepted_by: str | None
    accepted_at: datetime | None
    used_at: datetime | None = Field(description="register: when its right registered the project")


class InviteCreated(InviteRow):
    token: str = Field(description="evn_...: shown this once, the hub keeps only its SHA-256")
    url: str | None = Field(description="the link to hand out, EVO_HUB_PUBLIC_URL + /invite/ + token; null without one")


class InviteAccept(BaseModel):
    token: str = Field(min_length=1, max_length=200, description="the token of the link, evn_...")


class InvitePreview(BaseModel):
    kind: Literal["join", "register"]
    invited_by: str = Field(description="the hub admin who made the link")
    status: Status
    expires_at: datetime
    bound: bool = Field(description="whether one GitHub login alone may accept the link; which one is never shown")
    viewer: str | None = Field(description="the login signed in on the web that asks; null for a visitor")
    for_viewer: bool = Field(description="whether the viewer may accept the link: signed in, and it unbound or theirs")
    accepted_by_viewer: bool = Field(description="whether the viewer accepted the link")
    project: str | None = Field(
        description="join: the project; register: the project its right registered, once used. Null unless the link "
        "is open and for the viewer (unbound, anyone), or the viewer accepted it"
    )
    role: Role | None = Field(description="join: the role it gives, shown as the project is")
    max_level: str | None = Field(description="join: the highest level it reaches, shown as the project is")


def _rows():
    """Every link as InviteRow reads it, its status computed at the time of the query."""
    invites, projects = tables.invites, tables.projects
    creator, accepter = tables.users.alias("creator"), tables.users.alias("accepter")
    status = case(
        (invites.c.revoked_at.is_not(None), REVOKED),
        (invites.c.accepted_at.is_not(None), ACCEPTED),
        (invites.c.expires_at <= func.now(), EXPIRED),
        else_=OPEN,
    )
    return select(
        invites.c.id,
        invites.c.kind,
        projects.c.name.label("project"),
        invites.c.role,
        invites.c.max_level,
        invites.c.for_login,
        creator.c.login.label("created_by"),
        invites.c.created_at,
        invites.c.expires_at,
        status.label("status"),
        accepter.c.login.label("accepted_by"),
        invites.c.accepted_at,
        invites.c.used_at,
    ).select_from(
        invites.join(creator, creator.c.id == invites.c.created_by)
        .outerjoin(projects, projects.c.id == invites.c.project_id)
        .outerjoin(accepter, accepter.c.id == invites.c.accepted_by)
    )


ROWS = _rows()


async def _one(conn: AsyncConnection, invite_id: int) -> InviteRow:
    row = (await conn.execute(ROWS.where(tables.invites.c.id == invite_id))).one()
    return InviteRow(**row._asdict())


def _target(invite: InviteRow) -> str:
    """The audit target of a link: what it gives, never its token."""
    parts = [f"invite:{invite.id}", f"kind={invite.kind}"]
    if invite.kind == JOIN:
        parts += [f"project={invite.project}", f"role={invite.role}", f"max_level={invite.max_level}"]
    if invite.for_login:
        parts.append(f"for={invite.for_login}")
    return " ".join(parts)


def _url(config: HubConfig, token: str) -> str | None:
    return f"{config.public_url}{PAGE}{token}" if config.public_url else None


@router.post(
    "",
    status_code=201,
    response_model=InviteCreated,
    responses={404: {"model": ErrorBody}, 422: {"model": ErrorBody}},
)
async def create(request: Request, response: Response, body: InviteRequest, user: AdminUser) -> InviteCreated:
    """Make a one-time invite link; the answer holds its token, which the hub shows this once."""
    invites = tables.invites
    token = PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
    async with request.app.state.engine.begin() as conn:
        project_id = None
        if body.kind == JOIN:
            project_id, ladder = await project_ladder(conn, body.project)
            if body.max_level not in ladder:
                levels = ", ".join(ladder)
                raise HTTPException(
                    422, f"max_level must be a level of project {body.project} ({levels}), not {body.max_level}"
                )
        made = await conn.execute(
            insert(invites)
            .values(
                kind=body.kind,
                project_id=project_id,
                role=body.role,
                max_level=body.max_level,
                for_login=body.for_login,
                token_hash=hash_token(token),
                created_by=user.user_id,
                expires_at=func.now() + timedelta(days=body.days),
            )
            .returning(invites.c.id)
        )
        invite = await _one(conn, made.scalar_one())
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.INVITE_CREATE,
            target=_target(invite),
            project_id=project_id,
        )
    response.headers.update(NO_STORE)
    return InviteCreated(**invite.model_dump(), token=token, url=_url(request.app.state.config, token))


@router.get("", response_model=list[InviteRow], responses=PAGED)
async def list_invites(request: Request, page: Paging = WHOLE) -> list[InviteRow]:
    """Every invite link, newest first, with its status; never a token."""
    async with request.app.state.engine.begin() as conn:
        rows = await page.rows(conn, ROWS.order_by(tables.invites.c.id.desc()))
    return [InviteRow(**row._asdict()) for row in rows]


def _status(row: Row) -> str:
    if row.revoked_at is not None:
        return REVOKED
    if row.accepted_at is not None:
        return ACCEPTED
    return EXPIRED if row.expires_at <= row.now else OPEN


async def _locked(conn: AsyncConnection, *where) -> Row | None:
    """The link ``where`` finds, its row locked until the transaction ends, with the database's time as ``now``."""
    invites = tables.invites
    found = await conn.execute(select(invites, func.now().label("now")).where(*where).with_for_update(of=invites))
    return found.first()


@router.post(
    "/{invite_id}/revoke", response_model=InviteRow, responses={404: {"model": ErrorBody}, 409: {"model": ErrorBody}}
)
async def revoke(request: Request, invite_id: InviteId, user: AdminUser) -> InviteRow:
    """Revoke an open link, or the unused right an accepted register link gave."""
    invites = tables.invites
    async with request.app.state.engine.begin() as conn:
        row = await _locked(conn, invites.c.id == invite_id)
        if row is None:
            raise HTTPException(404, f"there is no invite link {invite_id}")
        status = _status(row)
        unused_right = row.kind == REGISTER and status == ACCEPTED and row.used_at is None
        if status != OPEN and not unused_right:
            hint = " (`evo-agents hub admin revoke` takes back the grant it gave)" if status == ACCEPTED else ""
            raise HTTPException(409, f"invite link {invite_id} is {status}: there is nothing to revoke{hint}")
        await conn.execute(
            update(invites).where(invites.c.id == invite_id).values(revoked_at=func.now(), revoked_by=user.user_id)
        )
        invite = await _one(conn, invite_id)
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.INVITE_REVOKE,
            target=_target(invite),
            project_id=row.project_id,
        )
    return invite


def _refuse_closed(row: Row | None, user: Principal) -> None:
    """Raise unless ``row`` is a link ``user`` may accept now."""
    if row is None:
        raise HTTPException(404, f"this invite link is not known to the hub: {ASK_AGAIN}")
    status = _status(row)
    if status == ACCEPTED:
        raise HTTPException(410, f"this invite link was accepted already, and a link works once: {ASK_AGAIN}")
    if status == REVOKED:
        raise HTTPException(410, f"this invite link was revoked: {ASK_AGAIN}")
    if status == EXPIRED:
        raise HTTPException(410, f"this invite link expired at {row.expires_at:%Y-%m-%d %H:%M} UTC: {ASK_AGAIN}")
    if row.for_login is not None and row.for_login.lower() != user.login.lower():
        raise HTTPException(
            403, "this invite link is for another GitHub login: sign in as that login to accept it; nothing was granted"
        )


async def _viewer(request: Request) -> Principal | None:
    """Who is signed in on the web that sends ``request``: the live session of its cookie, or None."""
    session = request.cookies.get(SESSION_COOKIE)
    if not session:
        return None
    return await authenticate(request.app.state.engine, session, WEB, request.app.state.config)


@accept_router.post("/preview", response_model=InvitePreview, responses={404: {"model": ErrorBody}})
async def preview(request: Request, response: Response, body: InviteAccept) -> InvitePreview:
    """What an invite link gives, for its page: public, the token being the credential; never the login it is bound
    to, and its project and role only to someone it is for."""
    viewer = await _viewer(request)
    async with request.app.state.engine.begin() as conn:
        row = (await conn.execute(ROWS.where(tables.invites.c.token_hash == hash_token(body.token)))).first()
    response.headers.update(NO_STORE)
    if row is None:
        raise HTTPException(404, f"this invite link is not known to the hub: {ASK_AGAIN}")
    invite = InviteRow(**row._asdict())
    login = viewer.login.lower() if viewer is not None else None
    for_viewer = login is not None and (invite.for_login is None or invite.for_login.lower() == login)
    accepted_by_viewer = login is not None and (invite.accepted_by or "").lower() == login
    shown = accepted_by_viewer or (invite.status == OPEN and (invite.for_login is None or for_viewer))
    return InvitePreview(
        kind=invite.kind,
        invited_by=invite.created_by,
        status=invite.status,
        expires_at=invite.expires_at,
        bound=invite.for_login is not None,
        viewer=viewer.login if viewer is not None else None,
        for_viewer=for_viewer,
        accepted_by_viewer=accepted_by_viewer,
        project=invite.project if shown else None,
        role=invite.role if shown else None,
        max_level=invite.max_level if shown else None,
    )


def _higher(ladder: list[str], *levels: str) -> str:
    return max(levels, key=lambda level: ladder.index(level) if level in ladder else -1)


async def _give_grant(conn: AsyncConnection, row: Row, user: Principal) -> None:
    """The grant a join link gives, never lowering the one ``user`` holds on its project."""
    projects, grants = tables.projects, tables.grants
    name = (await conn.execute(select(projects.c.name).where(projects.c.id == row.project_id))).scalar_one()
    _, ladder = await project_ladder(conn, name)
    if row.max_level not in ladder:
        raise HTTPException(
            409, f"the max level of this invite link, {row.max_level}, is no longer a level of {name}: {ASK_AGAIN}"
        )
    held = (
        await conn.execute(
            select(grants.c.role, grants.c.max_level)
            .where(grants.c.user_id == user.user_id, grants.c.project_id == row.project_id)
            .with_for_update()
        )
    ).first()
    role, level = row.role, row.max_level
    if held is not None:
        role, level = max(role, held.role, key=ROLES.index), _higher(ladder, level, held.max_level)
        if (role, level) == (held.role, held.max_level):
            return
    await conn.execute(upsert_grant(user.user_id, row.project_id, role, level, row.created_by))


@accept_router.post(
    "/accept",
    response_model=InviteRow,
    responses={
        403: {"model": ErrorBody},
        404: {"model": ErrorBody},
        409: {"model": ErrorBody},
        410: {"model": ErrorBody, "description": "the link was accepted, revoked or has expired"},
    },
)
async def accept(request: Request, body: InviteAccept, user: CurrentUser) -> InviteRow:
    """Accept an invite link: a join link gives its grant, a register link the right to register one project."""
    if user.acts_for_run is not None:
        raise HTTPException(403, "the agent of a run accepts no invite link")
    invites = tables.invites
    async with request.app.state.engine.begin() as conn:
        row = await _locked(conn, invites.c.token_hash == hash_token(body.token))
        _refuse_closed(row, user)
        if row.kind == JOIN:
            await _give_grant(conn, row, user)
        await conn.execute(
            update(invites).where(invites.c.id == row.id).values(accepted_at=func.now(), accepted_by=user.user_id)
        )
        invite = await _one(conn, row.id)
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.INVITE_ACCEPT,
            target=_target(invite),
            project_id=row.project_id,
        )
    log.info("invite link accepted", extra={"invite": row.id, "kind": row.kind, "login": user.login})
    return invite


async def register_right(conn: AsyncConnection, user: Principal) -> Row | None:
    """The oldest right to register a project that ``user`` holds and has not used, its row locked; None without one.
    The agent of a run holds none."""
    if user.acts_for_run is not None:
        return None
    invites = tables.invites
    found = await conn.execute(
        select(invites.c.id, invites.c.created_by)
        .where(
            invites.c.kind == REGISTER,
            invites.c.accepted_by == user.user_id,
            invites.c.used_at.is_(None),
            invites.c.revoked_at.is_(None),
        )
        .order_by(invites.c.id)
        .limit(1)
        .with_for_update()
    )
    return found.first()


async def spend_register_right(conn: AsyncConnection, right: Row, user: Principal, project_id: int, top: str) -> None:
    """Mark ``right`` used by the new project ``project_id``, and make ``user`` its admin at level ``top``."""
    invites = tables.invites
    await conn.execute(
        update(invites).where(invites.c.id == right.id).values(used_at=func.now(), project_id=project_id)
    )
    await conn.execute(upsert_grant(user.user_id, project_id, "admin", top, right.created_by))

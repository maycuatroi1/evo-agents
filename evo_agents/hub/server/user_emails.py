"""A member's emails: list them, add one, remove one (``evo-agents hub email list|add|remove``).

A member manages their own addresses; a hub admin manages anyone's, naming the member with ``user``, and anyone else
naming another member gets 403. An address belongs to one member on the hub, whatever its case: adding one another
member holds is a 409, adding one the member holds already answers that row with 200 instead of 201. A member holds
at most ``MAX_EMAILS``. GitHub's noreply address of a login maps to that login without a row, so one naming another
login is refused, and the member's own adds nothing but is taken. The hub never checks that the member reads an
address: sign-in asks GitHub for ``read:user`` only. An address travels in the body, never in a path, so no access
line holds it; removing names the row's id. Every change adds an audit row that names the row and the member, never
the address.

``logins_of_emails`` names the member of each of a few addresses for the hub's own use, through
``evo_agents.hub.user_emails.login_of_email``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import LOGIN_NAME
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.hub.user_emails import MAX_EMAILS, email_key, email_problem, login_of_email, noreply_login

MAX_ID = 2**63 - 1  # bigint
MAX_BODY_CHARS = 1000  # an address with spaces around it still fits; email_problem holds the real bound

router = APIRouter(prefix="/v1/emails", tags=["emails"], responses={401: {"model": ErrorBody}})

Member = Annotated[
    str | None,
    Query(alias="user", pattern=LOGIN_NAME, description="the member, any case; another's needs a hub admin"),
]


class UserEmail(BaseModel):
    id: int
    email: str  # as it was written
    login: str  # the member it belongs to
    created_at: datetime


class EmailAdd(BaseModel):
    email: str = Field(min_length=1, max_length=MAX_BODY_CHARS, description="one address; case does not matter")


def _listed():
    e, users = tables.user_emails, tables.users
    return select(e.c.id, e.c.email, users.c.login, e.c.created_at, e.c.user_id).join_from(
        e, users, users.c.id == e.c.user_id
    )


async def _member(conn: AsyncConnection, caller: Principal, login: str | None, *, lock: bool = False):
    """(id, login) of the member ``login`` names, the caller's own without it; 403 for another's unless the caller
    is a hub admin, 404 for a login the hub has no row of. ``lock`` holds the member's row until the transaction
    ends, so two adds count the member's addresses one after the other."""
    own = login is None or login.lower() == caller.login.lower()
    if not own and not caller.admin:
        raise HTTPException(403, "only a hub admin lists, adds or removes the emails of another member")
    users = tables.users
    named = users.c.id == caller.user_id if own else func.lower(users.c.login) == login.lower()
    query = select(users.c.id, users.c.login).where(named)
    found = (await conn.execute(query.with_for_update(key_share=True) if lock else query)).one_or_none()
    if found is None:
        raise HTTPException(404, f"no member {login} on this hub: they sign in once, or an admin grants them a role")
    return found.id, found.login


def _row(found) -> UserEmail:
    return UserEmail(id=found.id, email=found.email, login=found.login, created_at=found.created_at)


async def _audit(conn: AsyncConnection, caller: Principal, action: str, email_id: int, login: str) -> None:
    target = f"email:{email_id} user={login}"
    await audit.record(conn, actor_id=caller.user_id, token_id=caller.token_id, action=action, target=target)


@router.get("", response_model=list[UserEmail], responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}})
async def list_emails(request: Request, user: CurrentUser, login: Member = None) -> list[UserEmail]:
    """The emails of the caller, or of the member ``user`` names, in the order of their addresses."""
    e = tables.user_emails
    async with request.app.state.engine.begin() as conn:
        member_id, _ = await _member(conn, user, login)
        found = await conn.execute(_listed().where(e.c.user_id == member_id).order_by(func.lower(e.c.email), e.c.id))
        return [_row(row) for row in found]


def _refuse_address(email: str, login: str) -> None:
    problem = email_problem(email)
    if problem:
        raise HTTPException(422, problem)
    named = noreply_login(email)
    if named is not None and named.lower() != login.lower():
        raise HTTPException(422, f"{email} is GitHub's noreply address of {named}: it maps to {named} without a row")


@router.post(
    "",
    response_model=UserEmail,
    status_code=201,
    responses={200: {"model": UserEmail}, **{code: {"model": ErrorBody} for code in (403, 404, 409, 422)}},
)
async def add_email(
    request: Request, response: Response, body: EmailAdd, user: CurrentUser, login: Member = None
) -> UserEmail:
    """Give the caller, or the member ``user`` names, the address ``email``: 201 with the new row, 200 with the row
    they had, 409 when another member holds it."""
    e, email = tables.user_emails, body.email.strip()
    async with request.app.state.engine.begin() as conn:
        member_id, member = await _member(conn, user, login, lock=True)
        _refuse_address(email, member)
        held = (await conn.execute(_listed().where(func.lower(e.c.email) == func.lower(email)))).one_or_none()
        if held is not None and held.user_id == member_id:
            response.status_code = 200
            return _row(held)
        if held is not None:
            whose = f" ({held.login}'s)" if user.admin else ""
            raise HTTPException(409, f"{email} is the email of another member on this hub{whose}")
        count = select(func.count()).select_from(e).where(e.c.user_id == member_id)
        if (await conn.execute(count)).scalar_one() >= MAX_EMAILS:
            raise HTTPException(422, f"{member} holds {MAX_EMAILS} emails, the most a member does: remove one first")
        added = pg_insert(e).values(user_id=member_id, email=email).on_conflict_do_nothing()
        new = (await conn.execute(added.returning(e.c.id, e.c.email, e.c.created_at))).one_or_none()
        if new is None:  # another member took it since the check
            raise HTTPException(409, f"{email} is the email of another member on this hub")
        await _audit(conn, user, audit.EMAIL_ADD, new.id, member)
    return UserEmail(id=new.id, email=new.email, login=member, created_at=new.created_at)


@router.delete(
    "/{email_id}",
    response_model=UserEmail,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def remove_email(
    request: Request, user: CurrentUser, email_id: Annotated[int, Path(ge=1, le=MAX_ID)]
) -> UserEmail:
    """Remove the email ``email_id`` of the caller, or of anyone for a hub admin; answers the row it removed."""
    e = tables.user_emails
    async with request.app.state.engine.begin() as conn:
        found = (await conn.execute(_listed().where(e.c.id == email_id).with_for_update(of=e))).one_or_none()
        if found is None:
            raise HTTPException(404, f"no email {email_id} on this hub: see `evo-agents hub email list`")
        if found.user_id != user.user_id and not user.admin:
            raise HTTPException(403, "only a hub admin removes the emails of another member")
        await conn.execute(delete(e).where(e.c.id == email_id))
        await _audit(conn, user, audit.EMAIL_REMOVE, email_id, found.login)
    return _row(found)


async def logins_of_emails(conn: AsyncConnection, emails: Iterable[str]) -> dict[str, str | None]:
    """Each of ``emails``, as given, to the login of its member: its row in user_emails, else the login its GitHub
    noreply form names, else None."""
    given = list(dict.fromkeys(emails))
    keys = {email_key(email) for email in given}
    e, users = tables.user_emails, tables.users
    found = await conn.execute(
        select(func.lower(e.c.email), users.c.login)
        .join_from(e, users, users.c.id == e.c.user_id)
        .where(func.lower(e.c.email).in_(keys))
    )
    known = {key: login for key, login in found}
    return {email: login_of_email(email, known) for email in given}

"""A member's emails (``evo_agents.hub.server.user_emails``): who may see and change them, the checks of an address,
and each change with its audit row, in one transaction."""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.server import audit
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.identity.models.user_emails import UserEmail
from evo_agents.hub.server.identity.repository import user_emails as repository
from evo_agents.hub.server.security import Principal
from evo_agents.hub.user_emails import MAX_EMAILS, email_key, email_problem, login_of_email, noreply_login


async def member(conn: AsyncConnection, caller: Principal, login: str | None, *, lock: bool = False):
    """(id, login) of the member ``login`` names, the caller's own without it; 403 for another's unless the caller
    is a hub admin, 404 for a login the hub has no row of. ``lock`` holds the member's row until the transaction
    ends, so two adds count the member's addresses one after the other."""
    own = login is None or login.lower() == caller.login.lower()
    if not own and not caller.admin:
        raise HTTPException(403, "only a hub admin lists, adds or removes the emails of another member")
    found = await repository.member_row(conn, caller.user_id if own else None, login, lock=lock)
    if found is None:
        raise HTTPException(404, f"no member {login} on this hub: they sign in once, or an admin grants them a role")
    return found.id, found.login


def row(found) -> UserEmail:
    return UserEmail(id=found.id, email=found.email, login=found.login, created_at=found.created_at)


async def _audit(conn: AsyncConnection, caller: Principal, action: str, email_id: int, login: str) -> None:
    target = f"email:{email_id} user={login}"
    await audit.record(conn, actor_id=caller.user_id, token_id=caller.token_id, action=action, target=target)


def refuse_address(email: str, login: str) -> None:
    problem = email_problem(email)
    if problem:
        raise HTTPException(422, problem)
    named = noreply_login(email)
    if named is not None and named.lower() != login.lower():
        raise HTTPException(422, f"{email} is GitHub's noreply address of {named}: it maps to {named} without a row")


async def list_emails(engine: AsyncEngine, user: Principal, login: str | None) -> list[UserEmail]:
    """The emails of ``user``, or of the member ``login`` names, in the order of their addresses."""
    async with UnitOfWork(engine) as conn:
        member_id, _ = await member(conn, user, login)
        return [row(found) for found in await repository.emails_of(conn, member_id)]


async def add_email(engine: AsyncEngine, user: Principal, login: str | None, email: str) -> tuple[UserEmail, bool]:
    """Give ``user``, or the member ``login`` names, the address ``email``: the row, and whether it is new (False for
    the row they had); 409 when another member holds it."""
    async with UnitOfWork(engine) as conn:
        member_id, name = await member(conn, user, login, lock=True)
        refuse_address(email, name)
        held = await repository.holder(conn, email)
        if held is not None and held.user_id == member_id:
            return row(held), False
        if held is not None:
            whose = f" ({held.login}'s)" if user.admin else ""
            raise HTTPException(409, f"{email} is the email of another member on this hub{whose}")
        if await repository.count(conn, member_id) >= MAX_EMAILS:
            raise HTTPException(422, f"{name} holds {MAX_EMAILS} emails, the most a member does: remove one first")
        new = await repository.add(conn, member_id, email)
        if new is None:  # another member took it since the check
            raise HTTPException(409, f"{email} is the email of another member on this hub")
        await _audit(conn, user, audit.EMAIL_ADD, new.id, name)
    return UserEmail(id=new.id, email=new.email, login=name, created_at=new.created_at), True


async def remove_email(engine: AsyncEngine, user: Principal, email_id: int) -> UserEmail:
    """Remove the email ``email_id`` of ``user``, or of anyone for a hub admin; the row it removed."""
    async with UnitOfWork(engine) as conn:
        found = await repository.locked(conn, email_id)
        if found is None:
            raise HTTPException(404, f"no email {email_id} on this hub: see `evo-agents hub email list`")
        if found.user_id != user.user_id and not user.admin:
            raise HTTPException(403, "only a hub admin removes the emails of another member")
        await repository.remove(conn, email_id)
        await _audit(conn, user, audit.EMAIL_REMOVE, email_id, found.login)
    return row(found)


async def logins_of_emails(conn: AsyncConnection, emails: Iterable[str]) -> dict[str, str | None]:
    """Each of ``emails``, as given, to the login of its member: its row in user_emails, else the login its GitHub
    noreply form names, else None."""
    given = list(dict.fromkeys(emails))
    keys = {email_key(email) for email in given}
    known = await repository.logins_of_keys(conn, keys)
    return {email: login_of_email(email, known) for email in given}

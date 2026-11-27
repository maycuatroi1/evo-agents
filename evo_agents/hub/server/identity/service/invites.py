"""Invite links (``evo_agents.hub.server.invites``): making, listing, revoking, previewing and accepting a link, and
the right to register a project a register link gives, each change with its audit row in one transaction."""

from __future__ import annotations

import logging
import secrets

from fastapi import HTTPException
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.access import ROLES
from evo_agents.hub.config import HubConfig
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import project_ladder, upsert_grant
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.identity.models.invites import (
    ACCEPTED,
    ASK_AGAIN,
    EXPIRED,
    JOIN,
    OPEN,
    PAGE,
    PREFIX,
    REGISTER,
    REVOKED,
    TOKEN_BYTES,
    InviteCreated,
    InvitePreview,
    InviteRequest,
    InviteRow,
)
from evo_agents.hub.server.identity.repository import invites as repository
from evo_agents.hub.server.paging import Paging
from evo_agents.hub.server.security import Principal, hash_token

log = logging.getLogger("evo_agents.hub.server.invites")


def target(invite: InviteRow) -> str:
    """The audit target of a link: what it gives, never its token."""
    parts = [f"invite:{invite.id}", f"kind={invite.kind}"]
    if invite.kind == JOIN:
        parts += [f"project={invite.project}", f"role={invite.role}", f"max_level={invite.max_level}"]
    if invite.for_login:
        parts.append(f"for={invite.for_login}")
    return " ".join(parts)


def url(config: HubConfig, token: str) -> str | None:
    return f"{config.public_url}{PAGE}{token}" if config.public_url else None


async def make_invite(conn: AsyncConnection, user: Principal, body: InviteRequest) -> tuple[InviteRow, str]:
    """Make the one-time link ``body`` asks for, as ``user``, with its audit row; the link and its token, which the hub
    keeps only as a hash. A join link names a project and a max level of its ladder."""
    token = PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
    project_id = None
    if body.kind == JOIN:
        project_id, ladder = await project_ladder(conn, body.project)  # pyright: ignore[reportArgumentType]
        if body.max_level not in ladder:
            levels = ", ".join(ladder)
            raise HTTPException(
                422, f"max_level must be a level of project {body.project} ({levels}), not {body.max_level}"
            )
    made = await repository.insert_invite(
        conn,
        kind=body.kind,
        project_id=project_id,
        role=body.role,
        max_level=body.max_level,
        for_login=body.for_login,
        token_hash=hash_token(token),
        created_by=user.user_id,
        days=body.days,
    )
    invite = await repository.one(conn, made)
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.INVITE_CREATE,
        target=target(invite),
        project_id=project_id,
    )
    return invite, token


def created(config: HubConfig, invite: InviteRow, token: str) -> InviteCreated:
    """The answer to making a link: the link with its token and, with a public URL, the address to hand out."""
    return InviteCreated(**invite.model_dump(), token=token, url=url(config, token))


async def create(engine: AsyncEngine, config: HubConfig, user: Principal, body: InviteRequest) -> InviteCreated:
    """Make a one-time invite link in its own transaction; the answer holds its token."""
    async with UnitOfWork(engine) as conn:
        invite, token = await make_invite(conn, user, body)
    return created(config, invite, token)


async def list_invites(engine: AsyncEngine, page: Paging) -> list[InviteRow]:
    """Every invite link on ``page``, newest first, with its status; never a token."""
    async with UnitOfWork(engine) as conn:
        rows = await repository.page_of(conn, page)
    return [InviteRow(**row._asdict()) for row in rows]


def status(row: Row) -> str:
    if row.revoked_at is not None:
        return REVOKED
    if row.accepted_at is not None:
        return ACCEPTED
    return EXPIRED if row.expires_at <= row.now else OPEN


async def revoke(engine: AsyncEngine, user: Principal, invite_id: int) -> InviteRow:
    """Revoke an open link, or the unused right an accepted register link gave: 404 for no such link, 409 for any
    other."""
    async with UnitOfWork(engine) as conn:
        row = await repository.locked_by_id(conn, invite_id)
        if row is None:
            raise HTTPException(404, f"there is no invite link {invite_id}")
        found = status(row)
        unused_right = row.kind == REGISTER and found == ACCEPTED and row.used_at is None
        if found != OPEN and not unused_right:
            hint = " (`evo-agents hub admin revoke` takes back the grant it gave)" if found == ACCEPTED else ""
            raise HTTPException(409, f"invite link {invite_id} is {found}: there is nothing to revoke{hint}")
        await repository.set_revoked(conn, invite_id, user.user_id)
        invite = await repository.one(conn, invite_id)
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.INVITE_REVOKE,
            target=target(invite),
            project_id=row.project_id,
        )
    return invite


def refuse_closed(row: Row | None, user: Principal) -> None:
    """Raise unless ``row`` is a link ``user`` may accept now."""
    if row is None:
        raise HTTPException(404, f"this invite link is not known to the hub: {ASK_AGAIN}")
    found = status(row)
    if found == ACCEPTED:
        raise HTTPException(410, f"this invite link was accepted already, and a link works once: {ASK_AGAIN}")
    if found == REVOKED:
        raise HTTPException(410, f"this invite link was revoked: {ASK_AGAIN}")
    if found == EXPIRED:
        raise HTTPException(410, f"this invite link expired at {row.expires_at:%Y-%m-%d %H:%M} UTC: {ASK_AGAIN}")
    if row.for_login is not None and row.for_login.lower() != user.login.lower():
        raise HTTPException(
            403, "this invite link is for another GitHub login: sign in as that login to accept it; nothing was granted"
        )


async def preview(engine: AsyncEngine, viewer: Principal | None, token: str) -> InvitePreview:
    """What the link of ``token`` gives, as ``viewer`` (None for a visitor) may see it; 404 for a token the hub does
    not know."""
    async with UnitOfWork(engine) as conn:
        row = await repository.by_token_hash(conn, hash_token(token))
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


def higher(ladder: list[str], *levels: str) -> str:
    return max(levels, key=lambda level: ladder.index(level) if level in ladder else -1)


async def give_grant(conn: AsyncConnection, row: Row, user: Principal) -> None:
    """The grant a join link gives, never lowering the one ``user`` holds on its project."""
    name = await repository.project_name(conn, row.project_id)
    _, ladder = await project_ladder(conn, name)
    if row.max_level not in ladder:
        raise HTTPException(
            409, f"the max level of this invite link, {row.max_level}, is no longer a level of {name}: {ASK_AGAIN}"
        )
    held = await repository.held_grant(conn, user.user_id, row.project_id)
    role, level = row.role, row.max_level
    if held is not None:
        role, level = max(role, held.role, key=ROLES.index), higher(ladder, level, held.max_level)
        if (role, level) == (held.role, held.max_level):
            return
    await repository.write(conn, upsert_grant(user.user_id, row.project_id, role, level, row.created_by))


async def accept(engine: AsyncEngine, user: Principal, token: str) -> InviteRow:
    """Accept the link of ``token`` as ``user``: a join link gives its grant, a register link the right to register
    one project. 403 for the agent of a run."""
    if user.acts_for_run is not None:
        raise HTTPException(403, "the agent of a run accepts no invite link")
    async with UnitOfWork(engine) as conn:
        row = await repository.locked_by_token_hash(conn, hash_token(token))
        refuse_closed(row, user)
        if row.kind == JOIN:  # pyright: ignore[reportOptionalMemberAccess]
            await give_grant(conn, row, user)  # pyright: ignore[reportArgumentType]
        await repository.set_accepted(conn, row.id, user.user_id)  # pyright: ignore[reportOptionalMemberAccess]
        invite = await repository.one(conn, row.id)  # pyright: ignore[reportOptionalMemberAccess]
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.INVITE_ACCEPT,
            target=target(invite),
            project_id=row.project_id,  # pyright: ignore[reportOptionalMemberAccess]
        )
    log.info("invite link accepted", extra={"invite": row.id, "kind": row.kind, "login": user.login})  # pyright: ignore[reportOptionalMemberAccess]
    return invite


async def register_right(conn: AsyncConnection, user: Principal) -> Row | None:
    """The oldest right to register a project that ``user`` holds and has not used, its row locked; None without one.
    The agent of a run holds none."""
    if user.acts_for_run is not None:
        return None
    return await repository.register_right(conn, user.user_id)


async def spend_register_right(conn: AsyncConnection, right: Row, user: Principal, project_id: int, top: str) -> None:
    """Mark ``right`` used by the new project ``project_id``, and make ``user`` its admin at level ``top``."""
    await repository.set_used(conn, right.id, project_id)
    await repository.write(conn, upsert_grant(user.user_id, project_id, "admin", top, right.created_by))

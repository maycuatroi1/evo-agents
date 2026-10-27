"""Bridges of group chats to groups of a core plugin's channel (``docs/chat-bridges.md``): POST
/v1/chats/{id}/bridges/code and DELETE /v1/chats/{id}/bridges/{bridge_id}, and the queries ``core_plugin_api``'s
``bridge_*`` functions go through.

A chat admin (an admin of every project of the chat's agents, as setting its level needs) makes a one-time code from a
web session for a kind of channel a core plugin contributes, once every one of those projects enables that plugin and
grants it chats:read and chats:write. A machine token cannot make one, so a token that leaked cannot bridge a chat to a
group of its own. The plugin redeems the code from the group (``bridge_link``) while that session lives and the person
who made it is still a chat admin; a group bridges one chat at a time. A bridge comes off from the web or the CLI (a
chat admin), from the group (the plugin, ``via`` bot) or by a hub admin; each link and unlink is audited.

What a bridge carries: a message of the chat goes out (``chat_bridge_events``) when the sink of kind channel of the
bridge's kind allows its label in every project the label names (``chat_access.channel_cleared``), its text redacted
and with the files its author attached alone, never a run's outputs; a message of the group comes in as the member the
plugin finds for its sender, through the code of
POST /v1/chats/{id}/messages, with ``via`` the bridge's kind. ``chat_bridge_messages`` maps the ids both ways, so a
reply threads in the group and in the chat.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, tables, telegram
from evo_agents.hub.plugin_calls import core_registry, effective_grant, enabled_in
from evo_agents.hub.server import audit
from evo_agents.hub.server.chat_access import ChatAccess, chat_access, label_admin
from evo_agents.hub.server.chat_members import ChatId
from evo_agents.hub.server.chat_views import Chat, chat_view
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.member_channels import live_session, session_live
from evo_agents.hub.server.notifications import SESSION_ENDED
from evo_agents.hub.server.security import WEB, CurrentUser, Principal

log = logging.getLogger(__name__)

REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
KIND_PATTERN = r"^[a-z][a-z0-9_-]{0,62}$"
MAX_EXTERNAL_CHARS = 200  # migration 0165
UNLINKED_VIA = ("web", "cli", "bot", "admin")
READS, WRITES = "chats:read", "chats:write"
FROM_SESSION = (
    "a chat is bridged from a web session only, so a token that leaked cannot bridge one to a group of its own: sign "
    "in on the web and make the code there"
)
NOT_ADMIN = "bridging chat {chat} needs the admin role on every project of its agents: {names}"
UNKNOWN_KIND = "no core plugin of this hub contributes a channel of kind {kind!r}"
NOT_GRANTED = (
    "project {project} does not enable plugin {plugin} with chats:read and chats:write, which a bridge of kind {kind} "
    "needs: an admin of the project enables and grants it"
)
GROUP_TAKEN = "that group is bridged to a chat already: unbridge it there first"
NO_BRIDGE = "chat {chat} has no bridge {bridge}"

router = APIRouter(prefix="/v1/chats", tags=["chats"], responses={401: {"model": ErrorBody}})


class ChatBridgeCodeIn(BaseModel):
    kind: str = Field(pattern=KIND_PATTERN, description="the kind of channel of the group, as a core plugin names it")


class ChatBridgeCode(BaseModel):
    code: str = Field(description="the one-time code the group's plugin takes, shown once")
    kind: str
    expires_at: datetime = Field(description="ten minutes after it was made")


def kinds() -> dict[str, str]:
    """The kinds of channel the core plugins of this process contribute, each with the id of its plugin."""
    return {item["name"]: manifest.id for manifest, item in core_registry().contributions("channels")}


def external_id(value) -> str:
    """``value``, the id of a group or of a message there, as the hub keeps it: 1 to MAX_EXTERNAL_CHARS characters."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("an external id is a string or an integer")
    text = str(value)
    if not 1 <= len(text) <= MAX_EXTERNAL_CHARS:
        raise ValueError(f"an external id has 1 to {MAX_EXTERNAL_CHARS} characters")
    return text


async def _granted(conn: AsyncConnection, plugin: str, project_id: int) -> bool:
    row = await enabled_in(conn, plugin, project_id)
    return row is not None and {READS, WRITES} <= set(effective_grant(row))


async def _bridgeable(conn: AsyncConnection, user: Principal, chat: ChatAccess, kind: str) -> str:
    """The plugin of ``kind``, once ``user`` may bridge ``chat`` to a group of it; the refusal otherwise."""
    if chat.kind != chats.GROUP:
        raise HTTPException(409, f"chat {chat.chat_id} is not a group chat: only a group chat is bridged")
    if not chat.projects:
        raise HTTPException(409, f"chat {chat.chat_id} has no agents: no project's sink could let its messages out")
    if user.scope is not None or not await label_admin(conn, chat):
        raise HTTPException(403, NOT_ADMIN.format(chat=chat.chat_id, names=", ".join(p.name for p in chat.projects)))
    plugin = kinds().get(kind)
    if plugin is None:
        raise HTTPException(422, UNKNOWN_KIND.format(kind=kind))
    for project in chat.projects:
        if not await _granted(conn, plugin, project.id):
            raise HTTPException(403, NOT_GRANTED.format(project=project.name, plugin=plugin, kind=kind))
    return plugin


async def new_code(conn: AsyncConnection, user: Principal, chat_id: int, kind: str) -> tuple[str, datetime]:
    """A one-time code that bridges chat ``chat_id`` to a group of ``kind``, made by ``user``'s web session, and when it
    expires: 403 for any other credential, a session that ended or someone who is not a chat admin; it lives
    ``telegram.LINK_SECONDS``, the hub keeps its SHA-256 alone, and it replaces the codes ``user`` made for the chat and
    the kind that were not used."""
    if user.kind != WEB or user.scope is not None:
        raise HTTPException(403, FROM_SESSION)
    if not await session_live(conn, user.user_id, user.token_id):
        raise HTTPException(403, SESSION_ENDED)
    chat = await chat_access(conn, user, chat_id)
    await _bridgeable(conn, user, chat, kind)
    codes = tables.chat_bridge_codes
    day_ago = func.now() - timedelta(days=1)
    mine = and_(codes.c.user_id == user.user_id, codes.c.chat_id == chat_id, codes.c.kind == kind)
    await conn.execute(
        delete(codes).where(
            mine | (codes.c.expires_at < day_ago), codes.c.used_at.is_(None) | (codes.c.used_at < day_ago)
        )
    )
    code = telegram.new_code()
    made = (
        insert(codes)
        .values(
            chat_id=chat_id,
            user_id=user.user_id,
            token_id=user.token_id,
            kind=kind,
            code_hash=telegram.code_hash(code),
            expires_at=func.now() + timedelta(seconds=telegram.LINK_SECONDS),
        )
        .returning(codes.c.expires_at)
    )
    return code, (await conn.execute(made)).scalar_one()


def _columns():
    b = tables.chat_bridges
    return (b.c.id, b.c.chat_id, b.c.plugin, b.c.kind, b.c.external_id, b.c.config, b.c.state, b.c.linked_at)


async def redeem(conn: AsyncConnection, plugin: str, kind: str, code: str, group: str, config: dict):
    """Bridge the chat code ``code`` of ``kind`` was made for to group ``group``, with the plugin's ``config``: the
    bridge's row; None when the code is not one of the kind, was used, expired, or the session that made it ended.
    403 when its maker is no longer a chat admin or a project no longer grants the plugin, 409 when the group is
    bridged already. Audited as chat.bridge, as its maker."""
    if not isinstance(code, str) or not telegram.LINK_CODE.fullmatch(code):
        return None
    codes, users = tables.chat_bridge_codes, tables.users
    query = (
        select(codes.c.id, codes.c.chat_id, codes.c.user_id, codes.c.token_id, users.c.login)
        .join_from(codes, users, users.c.id == codes.c.user_id)
        .where(
            codes.c.code_hash == telegram.code_hash(code),
            codes.c.kind == kind,
            codes.c.used_at.is_(None),
            codes.c.expires_at > func.now(),
            live_session(codes),
        )
        .with_for_update(of=codes)
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        return None
    await conn.execute(update(codes).values(used_at=func.now()).where(codes.c.id == row.id))
    maker = Principal(user_id=row.user_id, login=row.login, admin=False, token_id=row.token_id, kind=WEB, token_hash="")
    chat = await chat_access(conn, maker, row.chat_id, lock=True)
    if await _bridgeable(conn, maker, chat, kind) != plugin:
        raise HTTPException(403, UNKNOWN_KIND.format(kind=kind))
    b = tables.chat_bridges
    made = (
        pg_insert(b)
        .values(
            chat_id=chat.chat_id, plugin=plugin, kind=kind, external_id=group, config=config, linked_by=maker.user_id
        )
        .on_conflict_do_nothing(index_elements=[b.c.kind, b.c.external_id], index_where=b.c.unlinked_at.is_(None))
        .returning(*_columns())
    )
    bridge = (await conn.execute(made)).one_or_none()
    if bridge is None:
        raise HTTPException(409, GROUP_TAKEN)
    target = f"chat:{chat.chat_id} bridge:{bridge.id} kind:{kind}"
    await audit.record(conn, actor_id=maker.user_id, token_id=maker.token_id, action=audit.CHAT_BRIDGE, target=target)
    log.info("chat bridged", extra={"chat_id": chat.chat_id, "bridge_id": bridge.id, "kind": kind})
    return bridge


async def linked(conn: AsyncConnection, kind: str, *, bridge_id: int | None = None, group: str | None = None):
    """The linked bridge of ``kind`` with id ``bridge_id`` or of group ``group``, or None."""
    b = tables.chat_bridges
    query = select(*_columns()).where(b.c.kind == kind, b.c.unlinked_at.is_(None))
    if bridge_id is not None:
        query = query.where(b.c.id == bridge_id)
    if group is not None:
        query = query.where(b.c.external_id == group)
    return (await conn.execute(query)).one_or_none()


async def unlink(
    conn: AsyncConnection, bridge_id: int, via: str, *, actor_id: int | None = None, token_id: int | None = None
) -> bool:
    """Take bridge ``bridge_id`` off, from ``via`` (web, cli, bot or admin); whether it was linked. Audited as
    chat.unbridge."""
    if via not in UNLINKED_VIA:
        raise ValueError(f"a bridge is unlinked from {', '.join(UNLINKED_VIA)}")
    b = tables.chat_bridges
    gone = (
        await conn.execute(
            update(b)
            .values(unlinked_at=func.now(), unlinked_by=actor_id, unlinked_via=via)
            .where(b.c.id == bridge_id, b.c.unlinked_at.is_(None))
            .returning(b.c.chat_id, b.c.kind)
        )
    ).one_or_none()
    if gone is None:
        return False
    target = f"chat:{gone.chat_id} bridge:{bridge_id} kind:{gone.kind} via={via}"
    await audit.record(conn, actor_id=actor_id, token_id=token_id, action=audit.CHAT_UNBRIDGE, target=target)
    log.info("chat unbridged", extra={"chat_id": gone.chat_id, "bridge_id": bridge_id, "via": via})
    return True


async def move(conn: AsyncConnection, bridge_id: int, kind: str, group: str) -> bool:
    """Bridge ``bridge_id`` follows its group to id ``group`` (a group the channel gave a new id); whether it moved.
    409 when another bridge has that group."""
    b = tables.chat_bridges
    taken = await conn.execute(
        select(b.c.id)
        .where(b.c.kind == kind, b.c.external_id == group, b.c.unlinked_at.is_(None), b.c.id != bridge_id)
        .limit(1)
    )
    if taken.first() is not None:
        raise HTTPException(409, GROUP_TAKEN)
    query = update(b).values(external_id=group).where(b.c.id == bridge_id, b.c.unlinked_at.is_(None))
    return (await conn.execute(query)).rowcount == 1


async def set_state(conn: AsyncConnection, bridge_id: int, state: dict) -> bool:
    b = tables.chat_bridges
    query = update(b).values(state=state).where(b.c.id == bridge_id, b.c.unlinked_at.is_(None))
    return (await conn.execute(query)).rowcount == 1


async def carry(conn: AsyncConnection, bridge_id: int, message_id: int, direction: str, group_message: str) -> None:
    """Map hub message ``message_id`` to the group's message ``group_message``, which it came ``in`` from or went
    ``out`` as; a mapping there is already stays."""
    bm = tables.chat_bridge_messages
    await conn.execute(
        pg_insert(bm)
        .values(bridge_id=bridge_id, message_id=message_id, direction=direction, external_id=group_message)
        .on_conflict_do_nothing()
    )


async def hub_message(conn: AsyncConnection, bridge_id: int, group_message: str) -> int | None:
    """The hub message the group's message ``group_message`` came in as or went out from, on bridge ``bridge_id``."""
    bm = tables.chat_bridge_messages
    query = select(bm.c.message_id).where(bm.c.bridge_id == bridge_id, bm.c.external_id == group_message)
    return (await conn.execute(query)).scalar_one_or_none()


async def group_message(conn: AsyncConnection, bridge_id: int, message_id: int) -> str | None:
    """The group's message hub message ``message_id`` came in from or went out as on bridge ``bridge_id``."""
    bm = tables.chat_bridge_messages
    query = (
        select(bm.c.external_id)
        .where(bm.c.bridge_id == bridge_id, bm.c.message_id == message_id)
        .order_by(bm.c.direction)
        .limit(1)
    )
    return (await conn.execute(query)).scalar_one_or_none()


# Routes


@router.post("/{chat_id}/bridges/code", response_model=ChatBridgeCode, status_code=201, responses=REFUSALS)
async def post_bridge_code(request: Request, chat_id: ChatId, body: ChatBridgeCodeIn, user: CurrentUser):
    """A one-time code that bridges the group chat to a group of ``kind``: the group's plugin takes it there within ten
    minutes. From a web session of an admin of every project of the chat's agents, each of which enables the kind's
    plugin with chats:read and chats:write."""
    async with request.app.state.engine.begin() as conn:
        code, expires_at = await new_code(conn, user, chat_id, body.kind)
    log.info("chat bridge code made", extra={"chat_id": chat_id, "kind": body.kind})
    return ChatBridgeCode(code=code, kind=body.kind, expires_at=expires_at)


@router.delete("/{chat_id}/bridges/{bridge_id}", response_model=Chat, responses=REFUSALS)
async def delete_bridge(request: Request, chat_id: ChatId, bridge_id: int, user: CurrentUser) -> Chat:
    """Take bridge ``bridge_id`` of the chat off: nothing goes to its group or comes from it any more. A chat admin
    does it from the web or the CLI, a hub admin from either."""
    b = tables.chat_bridges
    async with request.app.state.engine.begin() as conn:
        chat = await chat_access(conn, user, chat_id, lock=True)
        query = select(b.c.id).where(b.c.id == bridge_id, b.c.chat_id == chat_id, b.c.unlinked_at.is_(None))
        if (await conn.execute(query)).first() is None:
            raise HTTPException(404, NO_BRIDGE.format(chat=chat_id, bridge=bridge_id))
        if user.scope is None and await label_admin(conn, chat):
            via = "web" if user.kind == WEB else "cli"
        elif user.admin and user.scope is None:
            via = "admin"
        else:
            names = ", ".join(p.name for p in chat.projects)
            raise HTTPException(403, NOT_ADMIN.format(chat=chat_id, names=names))
        await unlink(conn, bridge_id, via, actor_id=user.user_id, token_id=user.token_id)
        return await chat_view(conn, chat)

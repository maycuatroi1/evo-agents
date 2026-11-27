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
bridge's kind allows its label in every project the label names (``chats.service.access.channel_cleared``), its text
redacted and with the files its author attached alone, never a run's outputs; a message of the group comes in as the
member the plugin finds for its sender, through the code of POST /v1/chats/{id}/messages, with ``via`` the bridge's
kind. ``chat_bridge_messages`` maps the ids both ways, so a reply threads in the group and in the chat.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import chats, telegram
from evo_agents.hub.plugin_calls import core_registry, effective_grant, enabled_in
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.chats.models.access import ChatAccess
from evo_agents.hub.server.chats.models.bridges import (
    FROM_SESSION,
    GROUP_TAKEN,
    NO_BRIDGE,
    NOT_ADMIN,
    NOT_GRANTED,
    READS,
    UNKNOWN_KIND,
    UNLINKED_VIA,
    WRITES,
)
from evo_agents.hub.server.chats.models.views import Chat
from evo_agents.hub.server.chats.repository import bridges as store
from evo_agents.hub.server.chats.service.access import chat_access, label_admin
from evo_agents.hub.server.chats.service.views import chat_view
from evo_agents.hub.server.notifications.models import SESSION_ENDED
from evo_agents.hub.server.notifications.repository.channels import session_live
from evo_agents.hub.server.security import WEB, Principal

log = logging.getLogger(__name__)


def kinds() -> dict[str, str]:
    """The kinds of channel the core plugins of this process contribute, each with the id of its plugin."""
    return {item["name"]: manifest.id for manifest, item in core_registry().contributions("channels")}


async def _granted(conn: AsyncConnection, plugin: str, project_id: int) -> bool:
    row = await enabled_in(conn, plugin, project_id)
    return row is not None and {READS, WRITES} <= set(effective_grant(row))


async def _bridgeable(conn: AsyncConnection, user: Principal, chat: ChatAccess, kind: str) -> str:
    """The plugin of ``kind``, once ``user`` may bridge ``chat`` to a group of it; the refusal otherwise."""
    if chat.kind not in (chats.GROUP, chats.WORK):  # a work chat is a group chat the hub opened
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
    await store.replace_codes(conn, user.user_id, chat_id, kind)
    code = telegram.new_code()
    expires_at = await store.insert_code(
        conn, chat_id, user.user_id, user.token_id, kind, telegram.code_hash(code), telegram.LINK_SECONDS
    )
    return code, expires_at


async def make_code(engine: AsyncEngine, user: Principal, chat_id: int, kind: str) -> tuple[str, datetime]:
    """``new_code`` in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        return await new_code(conn, user, chat_id, kind)


async def redeem(conn: AsyncConnection, plugin: str, kind: str, code: str, group: str, config: dict):
    """Bridge the chat code ``code`` of ``kind`` was made for to group ``group``, with the plugin's ``config``: the
    bridge's row; None when the code is not one of the kind, was used, expired, or the session that made it ended.
    403 when its maker is no longer a chat admin or a project no longer grants the plugin, 409 when the group is
    bridged already. Audited as chat.bridge, as its maker."""
    if not isinstance(code, str) or not telegram.LINK_CODE.fullmatch(code):
        return None
    row = await store.live_code(conn, kind, telegram.code_hash(code))
    if row is None:
        return None
    await store.use_code(conn, row.id)
    maker = Principal(user_id=row.user_id, login=row.login, admin=False, token_id=row.token_id, kind=WEB, token_hash="")
    chat = await chat_access(conn, maker, row.chat_id, lock=True)
    if await _bridgeable(conn, maker, chat, kind) != plugin:
        raise HTTPException(403, UNKNOWN_KIND.format(kind=kind))
    bridge = await store.insert_bridge(conn, chat.chat_id, plugin, kind, group, config, maker.user_id)
    if bridge is None:
        raise HTTPException(409, GROUP_TAKEN)
    target = f"chat:{chat.chat_id} bridge:{bridge.id} kind:{kind}"
    await audit.record(conn, actor_id=maker.user_id, token_id=maker.token_id, action=audit.CHAT_BRIDGE, target=target)
    log.info("chat bridged", extra={"chat_id": chat.chat_id, "bridge_id": bridge.id, "kind": kind})
    return bridge


async def unlink(
    conn: AsyncConnection, bridge_id: int, via: str, *, actor_id: int | None = None, token_id: int | None = None
) -> bool:
    """Take bridge ``bridge_id`` off, from ``via`` (web, cli, bot or admin); whether it was linked. Audited as
    chat.unbridge."""
    if via not in UNLINKED_VIA:
        raise ValueError(f"a bridge is unlinked from {', '.join(UNLINKED_VIA)}")
    gone = await store.unlink_bridge(conn, bridge_id, via, actor_id)
    if gone is None:
        return False
    target = f"chat:{gone.chat_id} bridge:{bridge_id} kind:{gone.kind} via={via}"
    await audit.record(conn, actor_id=actor_id, token_id=token_id, action=audit.CHAT_UNBRIDGE, target=target)
    log.info("chat unbridged", extra={"chat_id": gone.chat_id, "bridge_id": bridge_id, "via": via})
    return True


async def move(conn: AsyncConnection, bridge_id: int, kind: str, group: str) -> bool:
    """Bridge ``bridge_id`` follows its group to id ``group`` (a group the channel gave a new id); whether it moved.
    409 when another bridge has that group."""
    if await store.group_taken(conn, bridge_id, kind, group):
        raise HTTPException(409, GROUP_TAKEN)
    return await store.move_bridge(conn, bridge_id, group)


async def remove_bridge(engine: AsyncEngine, user: Principal, chat_id: int, bridge_id: int) -> Chat:
    """Take bridge ``bridge_id`` of chat ``chat_id`` off for ``user``, a chat admin (from the web or the CLI) or a hub
    admin: the chat as it reads now; 404 for a bridge the chat does not have, 403 for anyone else."""
    async with UnitOfWork(engine) as conn:
        chat = await chat_access(conn, user, chat_id, lock=True)
        if not await store.has_bridge(conn, chat_id, bridge_id):
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

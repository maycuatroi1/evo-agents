"""Bridges of group chats to groups of the plugin's channel through the API (1.5): the code a chat admin makes,
linking, finding and unlinking a bridge, the state it keeps, what goes out to its group and what comes in from it.
Private to ``core_plugin_api``; ``evo_agents.hub.server.chat_bridges`` holds the queries, ``docs/chat-bridges.md``
the contract."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import attachments, blob_gc, chats, outputs
from evo_agents.hub.blobs import BlobStoreUnavailable
from evo_agents.hub.redact import redact
from evo_agents.hub.server import attachment_store, chat_bridges, member_channels
from evo_agents.hub.server.blobs import blob_store, put_bytes
from evo_agents.hub.server.chat_access import agent_projects, channel_cleared, chat_access
from evo_agents.hub.server.chat_views import message_rows, profiles_by_id
from evo_agents.hub.server.chats import ChatMessageIn, deliver_message
from evo_agents.hub.server.core_plugin_api._acts import NO_PROJECT, READS, WRITES, _chat_grants, _feature_on, _granting
from evo_agents.hub.server.core_plugin_api._types import (
    Bridge,
    BridgeMessage,
    LinkCode,
    Member,
    Plugin,
    Refused,
    SentMessage,
    Session,
    admin_session,
    own_kind,
    own_member,
    own_plugin,
    own_session,
    principal_of,
    refusing,
    session_principal,
)

NOT_LINKED = "bridge {bridge} is not linked: a chat admin bridges the chat again"
UNAVAILABLE = "the hub's blob store does not answer now: send the file again later"


def _bridge(row) -> Bridge:
    return Bridge(row.id, row.chat_id, row.kind, row.external_id, dict(row.config), dict(row.state))


async def _own_bridge(conn: AsyncConnection, plugin: str, bridge_id: int):
    """The linked bridge ``bridge_id`` of plugin ``plugin``; Refused 404 for one unlinked, of another plugin or none."""
    if isinstance(bridge_id, bool) or not isinstance(bridge_id, int):
        raise ValueError("a bridge id is an integer")
    for kind in _kinds_of(plugin):
        row = await chat_bridges.linked(conn, kind, bridge_id=bridge_id)
        if row is not None and row.plugin == plugin:
            return row
    raise Refused(404, NOT_LINKED.format(bridge=bridge_id))


def _kinds_of(plugin: str) -> tuple[str, ...]:
    return tuple(kind for kind, owner in chat_bridges.kinds().items() if owner == plugin)


def _external(value) -> str:
    return chat_bridges.external_id(value)


# Linking and unlinking


async def bridge_code(conn: AsyncConnection, plugin: Plugin, session: Session, kind: str, chat_id: int) -> LinkCode:
    """A one-time code that bridges chat ``chat_id`` to a group of ``kind`` when the plugin redeems it from there
    (``bridge_link``), as POST /v1/chats/{id}/bridges/code makes it: 403 for a credential that is not a web session, a
    session that ended, or someone who is not an admin of every project of the chat's agents, and unless each of those
    projects enables the plugin with chats:read and chats:write; 404 for a chat the session's member is not in, 409
    for a chat that is not a group chat or has no agents."""
    own_kind(own_plugin(plugin), kind)
    own_session(session)
    with refusing():
        code, expires_at = await chat_bridges.new_code(conn, session_principal(session), chat_id, kind)
    return LinkCode(code, expires_at)


async def bridge_link(
    conn: AsyncConnection,
    plugin: Plugin,
    kind: str,
    code: str,
    external_id: str | int,
    *,
    config: Mapping[str, Any] | None = None,
) -> Bridge | None:
    """Bridge the chat ``code`` was made for to the group ``external_id`` of ``kind``, with ``config``: None when the
    code is not one of the kind, was used, expired, or the web session that made it ended. 403 when its maker is no
    longer an admin of every project of the chat's agents or a project no longer grants the plugin, 409 when the group
    is bridged already. Audited as chat.bridge, as the code's maker."""
    own_kind(own_plugin(plugin), kind)
    group = _external(external_id)
    kept = member_channels.json_object(config or {}, "a bridge's config")
    with refusing():
        row = await chat_bridges.redeem(conn, plugin.id, kind, code, group, kept)
    return None if row is None else _bridge(row)


async def bridge_find(conn: AsyncConnection, plugin: Plugin, kind: str, external_id: str | int) -> Bridge | None:
    """The linked bridge of the group ``external_id`` of ``kind``, or None."""
    own_kind(own_plugin(plugin), kind)
    row = await chat_bridges.linked(conn, kind, group=_external(external_id))
    return None if row is None or row.plugin != plugin.id else _bridge(row)


async def bridge_read(conn: AsyncConnection, plugin: Plugin, bridge_id: int) -> Bridge | None:
    """Bridge ``bridge_id`` of the plugin while it is linked, or None: what an event of ``BRIDGE_EVENT`` names."""
    own_plugin(plugin)
    try:
        return _bridge(await _own_bridge(conn, plugin.id, bridge_id))
    except Refused:
        return None


async def bridge_unlink(
    conn: AsyncConnection,
    plugin: Plugin,
    bridge_id: int,
    *,
    by: Literal["bot", "admin"] = "bot",
    actor: Session | None = None,
) -> bool:
    """Take bridge ``bridge_id`` off: ``by`` bot when its group asked or the plugin lost the group, ``by`` admin with
    ``actor`` from ``HubAdmin``. Whether it was linked; audited as chat.unbridge with ``via=<by>``."""
    own_plugin(plugin)
    if by not in ("bot", "admin"):
        raise ValueError("a bridge is unlinked by bot or admin through the API")
    if by == "admin":
        admin_session(actor)
    elif actor is not None:
        raise ValueError("only an unlink by admin names an actor")
    try:
        row = await _own_bridge(conn, plugin.id, bridge_id)
    except Refused:
        return False
    actor_id = None if actor is None else actor.user_id
    token_id = None if actor is None else actor.token_id or None
    return await chat_bridges.unlink(conn, row.id, by, actor_id=actor_id, token_id=token_id)


async def bridge_move(conn: AsyncConnection, plugin: Plugin, bridge_id: int, external_id: str | int) -> bool:
    """Bridge ``bridge_id`` follows its group to the id ``external_id`` the channel gave it; whether it moved. 409 when
    another bridge has that group."""
    own_plugin(plugin)
    row = await _own_bridge(conn, plugin.id, bridge_id)
    with refusing():
        return await chat_bridges.move(conn, row.id, row.kind, _external(external_id))


async def bridge_set_state(conn: AsyncConnection, plugin: Plugin, bridge_id: int, state: Mapping[str, Any]) -> bool:
    """Keep ``state`` (a JSON object of at most 16 KiB) as the bridge's, in place of what it kept; whether it is
    linked."""
    own_plugin(plugin)
    kept = member_channels.json_object(state, "a bridge's state")
    row = await _own_bridge(conn, plugin.id, bridge_id)
    return await chat_bridges.set_state(conn, row.id, kept)


# Out to the group


async def bridge_message(
    conn: AsyncConnection, plugin: Plugin, bridge_id: int, message_id: int
) -> BridgeMessage | None:
    """Message ``message_id`` of the bridge's chat as its group may read it: None unless the sink of kind channel of
    the bridge's kind allows its label in every project the label names (none: nothing goes out), or for a message
    of another chat; 403 unless the project of every agent of the chat grants the plugin chats:read. Its text goes
    through the hub's redaction, and it carries no file: neither what its author attached nor a run's outputs, so a
    tool's source never leaves this way. ``reply_to`` is the group's message its thread started from, when the bridge
    carried that one."""
    own_plugin(plugin)
    row = await _own_bridge(conn, plugin.id, bridge_id)
    found = await message_rows(conn, row.chat_id, [message_id]) if isinstance(message_id, int) else []
    if not found:
        return None
    message = found[0]
    projects = await agent_projects(conn, row.chat_id)
    if not projects:
        raise Refused(403, NO_PROJECT.format(chat=row.chat_id, plugin=plugin.id, permission=READS))
    for project in projects:
        await _granting(conn, plugin.id, project.name, READS)
    if not await channel_cleared(conn, message.label, row.kind):
        return None
    author_kind, author = "hub", None
    if message.author_profile_id is not None:
        profiles = await profiles_by_id(conn, [message.author_profile_id])
        author_kind, author = "agent", profiles[message.author_profile_id].handle
    elif message.author_login is not None:
        author_kind, author = "person", message.author_login
    root = message.thread_root_id
    reply_to = None if root is None else await chat_bridges.group_message(conn, row.id, root)
    return BridgeMessage(
        id=message.id,
        chat_id=message.chat_id,
        bridge_id=row.id,
        thread_root_id=root,
        author_kind=author_kind,
        author=author,
        body=redact(message.body),
        run_id=message.run_id,
        via=message.via,
        reply_to=reply_to,
        link=chats.chat_link(message.chat_id, message.id),
        created_at=message.created_at,
    )


async def bridge_sent(
    conn: AsyncConnection, plugin: Plugin, bridge_id: int, message_id: int, external_id: str | int
) -> None:
    """Message ``message_id`` went out to the bridge's group as its message ``external_id``: a reply to it there comes
    in to the thread of ``message_id``."""
    own_plugin(plugin)
    row = await _own_bridge(conn, plugin.id, bridge_id)
    if not await message_rows(conn, row.chat_id, [message_id]):
        raise Refused(404, f"chat {row.chat_id} has no message {message_id}")
    await chat_bridges.carry(conn, row.id, message_id, "out", _external(external_id))


# In from the group


async def _member_bridge(conn: AsyncConnection, member: Member, bridge_id: int):
    own_member(member)
    return await _own_bridge(conn, member.plugin, bridge_id)


async def bridge_send(
    conn: AsyncConnection,
    member: Member,
    bridge_id: int,
    body: str,
    *,
    external_id: str | int,
    reply_to: str | int | None = None,
    attachment_ids: Sequence[int] = (),
) -> SentMessage:
    """The group's message ``external_id`` comes in to the bridge's chat as the member, through the code of POST
    /v1/chats/{id}/messages (mentions, runs, notices, audit, and the refusals ``send_message`` names), with ``via``
    the bridge's kind and the files ``bridge_attach`` took; it does not go back to the group. A reply to a group's
    message the bridge carried (``reply_to``) goes in its thread. The same ``external_id`` again is the message it came
    in as."""
    row = await _member_bridge(conn, member, bridge_id)
    group_message = _external(external_id)
    known = await chat_bridges.hub_message(conn, row.id, group_message)
    if known is not None:
        (found,) = await message_rows(conn, row.chat_id, [known])
        return SentMessage(found.id, found.seq, found.thread_root_id)
    answered = None if reply_to is None else await chat_bridges.hub_message(conn, row.id, _external(reply_to))
    user = principal_of(member)
    with refusing():
        await _feature_on(conn, user, "chats")
        message = ChatMessageIn(body=body, reply_to=answered, attachment_ids=list(attachment_ids))
        chat = await chat_access(conn, user, row.chat_id)
        await _chat_grants(conn, member.plugin, chat, WRITES)
        message_id = await deliver_message(conn, chat, user, message, via=row.kind, via_bridge=row.id)
        (sent,) = await message_rows(conn, chat.chat_id, [message_id])
    await chat_bridges.carry(conn, row.id, message_id, "in", group_message)
    return SentMessage(sent.id, sent.seq, sent.thread_root_id)


async def bridge_attach(
    request: Request, member: Member, bridge_id: int, name: str, data: bytes, media_type: str | None = None
) -> int:
    """Keep ``data``, a file of the group named ``name``, as the member's upload to the bridge's chat, in the hub's
    blob store, as POST /v1/chats/{id}/attachments does: the id ``bridge_send`` takes in ``attachment_ids``. 422 for a
    name the hub refuses, 413 past the hub's limit of a file, 403 and 404 as ``bridge_send``, 503 while the blob store
    does not answer. Its own transactions, on the engine of ``request``'s app."""
    own_member(member)
    if not isinstance(data, bytes):
        raise ValueError("a file is bytes")
    if len(data) > attachments.MAX_FILE_BYTES:
        raise Refused(413, f"a file is at most {attachments.MAX_FILE_BYTES} bytes")
    try:
        name = attachments.check_name(name)
    except ValueError as error:
        raise Refused(422, str(error)) from None
    user = principal_of(member)
    engine = request.app.state.engine

    async def check(conn: AsyncConnection) -> int:
        row = await _member_bridge(conn, member, bridge_id)
        with refusing():
            chat = await chat_access(conn, user, row.chat_id)
            await _chat_grants(conn, member.plugin, chat, WRITES)
        return row.chat_id

    async with engine.begin() as conn:
        await check(conn)
    with refusing():
        store = blob_store(request)
    sha256 = hashlib.sha256(data).hexdigest()
    found = {
        "name": name,
        "media_type": media_type or outputs.guess_media_type(name),
        "size": len(data),
        "sha256": sha256,
    }
    try:
        await asyncio.to_thread(put_bytes, store, sha256, data)
        async with engine.begin() as conn:
            chat_id = await check(conn)
            await blob_gc.lock_shared(conn)
            if await blob_gc.revive(conn, [sha256]):
                await asyncio.to_thread(put_bytes, store, sha256, data, replace=True)
            kept = await attachment_store.record(conn, member.user_id, found, chat_id=chat_id)
    except BlobStoreUnavailable:
        raise Refused(503, UNAVAILABLE) from None
    except HTTPException as exc:
        raise Refused(exc.status_code, exc.detail) from None
    return kept.id

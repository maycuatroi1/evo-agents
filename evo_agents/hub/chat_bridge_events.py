"""The event ``chat.bridge``: what a group chat's bridges carry out, handed to the core plugin of each bridge
(``evo_agents.hub.server.chat_bridges``, ``docs/chat-bridges.md``).

A bridge's plugin gets one delivery per bridge, in the transaction that wrote what happened, of:

- ``message``: a new message of the chat, whoever wrote it, except to the bridge it came in by (no echo);
- ``progress``: a chat run that a message carried by the bridge started moved to a new state;
- ``reaction``: an emoji put on or taken off a message the bridge carried, in or out.

The payload carries ids and states, as every event does (``plugin_events``): the bridge, the chat, the message, the
run, the emoji. The plugin reads the message through the API (``core_plugin_api.bridge_message``), which checks the
channel's sinks of the projects its label names then. ``plugin_events.emit`` stores the delivery for the bridge's
plugin alone, in the first project of the chat's agents that enables it and grants it ``chats:read``; with none,
nothing goes out (fail closed), and delivering it checks the grant again.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import plugin_events, tables

EVENT = "chat.bridge"


async def _projects(conn: AsyncConnection, chat_id: int) -> list[int]:
    """The ids of the projects of the chat's agents, in the order of their ids."""
    m, ap = tables.chat_members, tables.agent_profiles
    query = (
        select(ap.c.project_id)
        .join_from(m, ap, ap.c.id == m.c.profile_id)
        .where(m.c.chat_id == chat_id, m.c.removed_at.is_(None))
        .distinct()
        .order_by(ap.c.project_id)
    )
    return list((await conn.execute(query)).scalars())


async def _bridges(conn: AsyncConnection, chat_id: int, *, carried: int | None = None) -> list:
    """The bridges of the chat that are linked, with the id of their plugin's row; those that carried message
    ``carried`` in or out alone when it is given."""
    b, p = tables.chat_bridges, tables.plugins
    query = (
        select(b.c.id, b.c.kind, p.c.id.label("plugin_row"))
        .join_from(b, p, p.c.name == b.c.plugin)
        .where(b.c.chat_id == chat_id, b.c.unlinked_at.is_(None))
        .order_by(b.c.id)
    )
    if carried is not None:
        bm = tables.chat_bridge_messages
        query = query.where(select(bm.c.bridge_id).where(bm.c.bridge_id == b.c.id, bm.c.message_id == carried).exists())
    return list((await conn.execute(query)).all())


async def _emit(conn: AsyncConnection, chat_id: int, bridges: list, payload: dict, projects=None) -> int:
    if not bridges:
        return 0
    found = list(projects) if projects is not None else await _projects(conn, chat_id)
    stored = 0
    for bridge in bridges:
        said = {"bridge_id": bridge.id, "chat_id": chat_id, **payload}
        for project_id in found:
            if await plugin_events.emit(conn, EVENT, project_id, said, plugin_id=bridge.plugin_row):
                stored += 1
                break
    return stored


async def message(
    conn: AsyncConnection, chat_id: int, said: dict, *, projects=None, via_bridge: int | None = None
) -> int:
    """A new message of the chat, ``said`` its ids, to each bridge but ``via_bridge``, the one it came in by;
    ``projects`` the ids of the projects of the chat's agents when the caller has them. How many deliveries."""
    bridges = [bridge for bridge in await _bridges(conn, chat_id) if bridge.id != via_bridge]
    return await _emit(conn, chat_id, bridges, {"what": "message", **said}, projects)


async def reaction(conn: AsyncConnection, chat_id: int, message_id: int, emoji: str, *, added: bool, count: int) -> int:
    """An emoji put on (``added``) or taken off message ``message_id``, to each bridge that carried it."""
    bridges = await _bridges(conn, chat_id, carried=message_id)
    payload = {"what": "reaction", "message_id": message_id, "emoji": emoji, "added": added, "count": count}
    return await _emit(conn, chat_id, bridges, payload)


async def progress(conn: AsyncConnection, chat_id: int, message_id: int, run_id: int, state: str) -> int:
    """Chat run ``run_id``, which message ``message_id`` started, moved to ``state``: to each bridge that carried
    the message."""
    bridges = await _bridges(conn, chat_id, carried=message_id)
    payload = {"what": "progress", "message_id": message_id, "run_id": run_id, "state": state}
    return await _emit(conn, chat_id, bridges, payload)

"""A message stored in a group chat and handed on: to the plugins of its projects (``plugin_events``), to the
builds of the graphs that hold its chats (``kg_sources.reads_chats``, ``kg_build_queue.queue_if_configured``) and
to the chat's bridges (``chat_bridge_events``). ``chats.repository.store`` writes its rows; this layer queues the
builds, which the repository may not import (a build that halts sends notices).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chat_bridge_events, kg_sources, plugin_events
from evo_agents.hub.jobs import JobQueue
from evo_agents.hub.kg_build_queue import queue_if_configured
from evo_agents.hub.server.chats.models.access import ChatAccess
from evo_agents.hub.server.chats.repository import store


async def insert_message(
    conn: AsyncConnection,
    chat: ChatAccess,
    body: str,
    *,
    thread_root_id: int | None,
    author_user_id: int | None = None,
    author_profile_id: int | None = None,
    run_id: int | None = None,
    via: str | None = None,
    via_bridge: int | None = None,
    from_output_id: int | None = None,
    copied_from_id: int | None = None,
    guest_name: str | None = None,
) -> tuple[int, int]:
    """Store a message of a person, an agent, a guest (``guest_name``) or the hub (none of them) in the chat, with the
    chat's label now, and its event, hand it to the chat's bridges but ``via_bridge``, the one it came in by (``via``
    its kind of channel), and queue the builds of the graphs that hold it; (its id, its seq). ``from_output_id`` is the
    interactive answer a person sent it from, ``copied_from_id`` the message of another chat it copies
    (``work_chats``)."""
    message_id, seq = await store.message_row(
        conn,
        chat,
        body,
        thread_root_id=thread_root_id,
        author_user_id=author_user_id,
        author_profile_id=author_profile_id,
        run_id=run_id,
        via=via,
        from_output_id=from_output_id,
        copied_from_id=copied_from_id,
        guest_name=guest_name,
    )
    by = "person" if author_user_id is not None else "agent" if author_profile_id is not None else "hub"
    by = "guest" if guest_name is not None else by
    said = {"chat_id": chat.chat_id, "message_id": message_id, "seq": seq, "thread_root_id": thread_root_id, "by": by}
    for project in chat.projects:  # the projects of its agents, its label's: a chat of people alone belongs to none
        await plugin_events.emit(conn, "chat.message", project.id, {**said, "run_id": run_id})
        if await kg_sources.reads_chats(conn, project.id):
            await queue_if_configured(JobQueue.in_transaction(), conn, project.id, project.name)
    projects = [project.id for project in chat.projects]
    await chat_bridge_events.message(
        conn, chat.chat_id, {**said, "run_id": run_id}, projects=projects, via_bridge=via_bridge
    )
    return message_id, seq

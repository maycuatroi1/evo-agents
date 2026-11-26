"""A chat and its messages as their reader sees them (``chats.models.views``), put together from what
``chats.repository.views`` reads.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats
from evo_agents.hub.server.attachments import repository as attachment_store
from evo_agents.hub.server.chats.models.access import ChatAccess, LabelReader, label_levels
from evo_agents.hub.server.chats.models.views import (
    Chat,
    ChatPerson,
    ChatProgress,
    GroupMessage,
    agent_member,
    message_author,
)
from evo_agents.hub.server.chats.repository import views as store
from evo_agents.hub.server.chats.service.access import label_admin
from evo_agents.hub.server.outputs.models import RunOutput
from evo_agents.hub.server.outputs.service.views import by_message as outputs_by_message
from evo_agents.hub.server.runs.models.attachment_parts import linked
from evo_agents.hub.server.work_status import cards
from evo_agents.hub.server.work_status_models import WorkCard


async def chat_view(conn: AsyncConnection, chat: ChatAccess) -> Chat:
    """The chat ``chat`` names, as its reader sees it."""
    rows = await store.member_rows(conn, chat.chat_id)
    found = await store.profiles_by_id(conn, [row.profile_id for row in rows if row.profile_id is not None])
    people = [
        ChatPerson(login=r.login, added_by=r.added_by, added_at=r.added_at, avatar_url=chats.github_avatar(r.github_id))
        for r in rows
        if r.user_id
    ]
    agents = [agent_member(found[r.profile_id], r.added_by, r.added_at) for r in rows if r.profile_id]
    seq, image, read = await store.chat_state(conn, chat.chat_id, chat.member_id)
    return Chat(
        id=chat.chat_id,
        title=chat.title,
        created_by=chat.creator,
        created_at=chat.created_at,
        people=people,
        agents=agents,
        label=chat.label,
        label_level=chat.label_level,
        label_levels=label_levels(chat),
        label_admin=await label_admin(conn, chat),
        bridges=await store.bridges_of(conn, chat.chat_id),
        restricted=chat.restricted,
        last_seq=seq,
        last_read_seq=read,
        kind=chat.kind,
        image_url=chats.image_url(chat.chat_id, image),
    )


async def messages_view(
    conn: AsyncConnection, rows, reader: LabelReader, progress: dict[int, list[ChatProgress]] | None = None
) -> list[GroupMessage]:
    """The messages of ``rows`` (``message_select``) that ``reader`` clears the label of, in the order given, each
    with its ``progress`` by message id (``chats.service.progress.by_message``), none when not given."""
    shown = [row for row in rows if reader.sees(row.label)]
    ids = [row.id for row in shown]
    if not ids:
        return []
    profiles = await store.profiles_by_id(conn, [row.author_profile_id for row in shown if row.author_profile_id])
    mentions = await store.mentions_of(conn, ids)
    reactions = await store.reactions_of(conn, ids, reader.user_id)
    replies = await store.reply_counts(conn, ids)
    files = await _outputs(conn, shown)
    attached = await attachment_store.of_messages(conn, ids)
    bridged = await store.bridged_of(conn, ids)
    forwarders = await store.forwarders(conn, shown)
    work = await _work_cards(conn, ids)
    return [
        GroupMessage(
            id=row.id,
            chat_id=row.chat_id,
            seq=row.seq,
            thread_root_id=row.thread_root_id,
            reply_count=replies.get(row.id, 0),
            author=message_author(row, profiles),
            run_id=row.run_id,
            body=row.body,
            mentions=mentions.get(row.id, []),
            reactions=reactions.get(row.id, []),
            label=row.label,
            created_at=row.created_at,
            progress=(progress or {}).get(row.id, []),
            outputs=files.get(row.id, []),
            attachments=linked(attached.get(row.id, []), f"/v1/chats/{row.chat_id}"),
            via=row.via,
            bridged_to=bridged.get(row.id, []),
            from_answer=row.from_output_id,
            forwarded_by=forwarders.get(row.id),
            repeats=row.repeats_id,
            work=work.get(row.id),
        )
        for row in shown
    ]


async def _work_cards(conn: AsyncConnection, ids: list[int]) -> dict[int, WorkCard]:
    """The work card each message of ``ids`` that carries one carries (``work_cards``), by message id."""
    carried = await store.card_traces(conn, ids)
    found = await cards(conn, list(carried.values())) if carried else {}
    return {message: found[trace] for message, trace in carried.items() if trace in found}


async def _outputs(conn: AsyncConnection, rows) -> dict[int, list[RunOutput]]:
    """The outputs the messages of ``rows`` that a run wrote carry, by message id."""
    found: dict[int, list[RunOutput]] = {}
    by_chat: dict[int, list[int]] = defaultdict(list)
    for row in rows:
        if row.run_id is not None:
            by_chat[row.chat_id].append(row.id)
    for chat_id, ids in by_chat.items():
        found.update(await outputs_by_message(conn, chat_id, ids))
    return found

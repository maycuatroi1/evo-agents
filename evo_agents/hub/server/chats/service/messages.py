"""A person's message in a group chat (``chats.service.chats`` has the routes it comes by): its mentions, the work
they hand the agents, and the hub's answer to what they refuse.

The hub reads a message's mentions once (``chats.mention_names``) and keeps them with it: a name resolves among the
chat's members only, a person by login, an agent by handle, a project's name to its default agent. A person mentioned
gets the notice chat_mention. An agent mentioned that is not in the chat, or was deleted, reaches no one: the mention
keeps why, and the hub answers in the message's thread, naming it. An agent mentioned gets the work: a chat run for the
sender (``chats.service.runs.hand_mentions``, at most ``chat_runs.MAX_RUNS_PER_MESSAGE`` agents a message), and the
mention keeps the run it made or woke, or why it made none, which the hub's answer names too. In a chat of one person,
a message in a thread without a mention goes to the one agent whose session the sender has in that thread (the mention
``via`` thread). In a group chat, a message that mentions no one and answers a message of an agent of the chat goes to
that agent (``via`` reply), as a reply to the bot's message in a bridged group does; the run's inbox quotes the message
it answers when another session of the agent wrote it. In a direct chat (``chats.service.direct``) a message whose
mentions do not reach its agent goes to it all the same: in the thread of the sender's session with it as above, else
``via`` direct, which for a message outside a thread starts a new session. In a settle chat (``chats.SETTLE``, which
the hub opens) every message of a person whose mentions do not reach the chat's agent goes to it ``via`` settle, in the
sender's session of the message's thread, a member who reads the agent's project alone included
(``chats.service.runs.hand_work``). Elsewhere a message that mentions no agent makes no run and asks no model
anything.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chat_runs as rules
from evo_agents.hub import chats
from evo_agents.hub.credentials import dispatch_credential
from evo_agents.hub.server import audit
from evo_agents.hub.server.attachments.service import messages as attachment_store
from evo_agents.hub.server.chats.models.access import ChatAccess
from evo_agents.hub.server.chats.models.messages import NOT_IN_CHAT, NOT_IN_DIRECT, NOT_THERE, ChatMessageIn
from evo_agents.hub.server.chats.models.runs import Answered, Hand
from evo_agents.hub.server.chats.models.views import GroupMessage
from evo_agents.hub.server.chats.repository import messages as store
from evo_agents.hub.server.chats.repository.runs import thread_session
from evo_agents.hub.server.chats.repository.store import insert_mentions, mark_read
from evo_agents.hub.server.chats.repository.views import message_rows
from evo_agents.hub.server.chats.service import answers as chat_answers
from evo_agents.hub.server.chats.service.access import chat_access, label_reader
from evo_agents.hub.server.chats.service.direct import direct_mention
from evo_agents.hub.server.chats.service.groups import open_from
from evo_agents.hub.server.chats.service.notices import notice_mention
from evo_agents.hub.server.chats.service.posting import insert_message
from evo_agents.hub.server.chats.service.progress import by_message, chat_progress
from evo_agents.hub.server.chats.service.runs import hand_mentions, hub_reply
from evo_agents.hub.server.chats.service.views import messages_view
from evo_agents.hub.server.security import Principal
from evo_agents.hub.server.systems.service import direct as direct_systems
from evo_agents.hub.server.work_chats import service as work_chats


async def visible_message(conn: AsyncConnection, chat: ChatAccess, message_id: int):
    """The row (``message_select``) of message ``message_id`` of the chat, when the reader clears its label; 404
    otherwise."""
    rows = await message_rows(conn, chat.chat_id, [message_id])
    reader = await label_reader(conn, chat.user_id, [row.label for row in rows])
    if not rows or not reader.sees(rows[0].label):
        raise HTTPException(404, NOT_THERE.format(chat=chat.chat_id, id=message_id))
    return rows[0]


async def _outsiders(conn: AsyncConnection, user: Principal, names: list[str]) -> dict[str, int]:
    """The agents ``names`` names that ``user`` can see (a grant on their project) but are not in the chat: handle to
    profile id."""
    if not names:
        return {}
    return await store.outsiders(conn, user.user_id, names)


async def resolve_mentions(conn: AsyncConnection, chat: ChatAccess, user: Principal, names: list[str]):
    """(the mentions to keep, the people they reach, why the agents they name reach no one) of ``names``."""
    members = await store.members_by_name(conn, chat.chat_id)
    outsiders = await _outsiders(conn, user, [name for name in names if name not in members])
    kept, people, refusals = [], [], []
    for name in names:
        found = members.get(name)
        if found is None and name in outsiders:
            refusal = (NOT_IN_DIRECT if chat.kind == chats.DIRECT else NOT_IN_CHAT).format(name=name)
            kept.append({"name": name, "via": None, "user_id": None, "profile_id": outsiders[name], "refusal": refusal})
            refusals.append(refusal)
        elif found is not None and "user_id" in found:
            kept.append(
                {"name": name, "via": "login", "user_id": found["user_id"], "profile_id": None, "refusal": None}
            )
            people.append(found["user_id"])
        elif found is not None:
            refusal = f"@{name} was deleted: it takes no new work" if found["deleted"] else None
            mention = {"name": name, "via": found["via"], "user_id": None, "profile_id": found["profile_id"]}
            kept.append({**mention, "refusal": refusal})
            refusals += [refusal] if refusal else []
    return kept, people, refusals


async def _thread_agent(conn: AsyncConnection, chat: ChatAccess, user: Principal, root: int | None) -> list[dict]:
    """The mention, ``via`` thread, of the one agent whose session ``user`` has in the thread of ``root``, when the
    chat has no other person, or no other person wrote in that thread; none otherwise."""
    if root is None:
        return []
    if await store.people_count(conn, chat.chat_id) != 1 and await store.others_in_thread(conn, root, user.user_id):
        return []
    profile_id = await thread_session(conn, chat.chat_id, root, user.user_id)
    if profile_id is None:
        return []
    handle = await store.handle_of(conn, profile_id)
    return [{"name": handle, "via": "thread", "user_id": None, "profile_id": profile_id, "refusal": None}]


async def _answered(conn: AsyncConnection, answered) -> Answered | None:
    """The agent's or the guest's message ``answered`` (a row of ``message_select``) that a message answers, as the inbox
    of the run it wakes quotes it; None for a message of a person or of the hub, or none."""
    if answered is not None and answered.guest_name is not None:
        return Answered(answered.id, None, rules.guest(answered.guest_name), answered.body, answered.label, None)
    if answered is None or answered.author_profile_id is None:
        return None
    handle = await store.handle_of(conn, answered.author_profile_id)
    return Answered(
        answered.id, answered.author_profile_id, f"@{handle}", answered.body, answered.label, answered.run_id
    )


async def _replied_agent(conn: AsyncConnection, chat: ChatAccess, answered: Answered | None, kept: list[dict]):
    """The mention, ``via`` reply, of the agent that wrote ``answered``, the message a message of a group chat that
    mentions no one (``kept`` is empty) answers; none otherwise, or when that agent left the chat or was deleted."""
    if chat.kind not in (chats.GROUP, chats.WORK) or answered is None or answered.profile_id is None or kept:
        return []
    handle = await store.member_handle(conn, chat.chat_id, answered.profile_id)
    if handle is None:
        return []
    return [{"name": handle, "via": chats.REPLY, "user_id": None, "profile_id": answered.profile_id, "refusal": None}]


async def _unnamed_agent(
    conn: AsyncConnection, chat: ChatAccess, user: Principal, root: int | None, answered, kept: list[dict]
) -> list[dict]:
    """The mention a message of a group chat that names no agent hands its work by: the agent it answers, else the
    one whose session is in its thread; none on the main line of a work chat, which goes to its coordinator."""
    if chat.kind == chats.WORK and await work_chats.main_line(conn, root):
        return []
    return await _replied_agent(conn, chat, answered, kept) or await _thread_agent(conn, chat, user, root)


async def _to_coordinator(conn: AsyncConnection, chat: ChatAccess, user: Principal, hand: Hand, kept: list[dict]):
    """The hub's answer, when there is one, to a message of work chat ``chat`` that mentions no agent (``kept`` names
    none): on its main line it goes to the coordinator's session (``work_chats.to_coordinator``); [] for any other."""
    root = None if hand.root_id == hand.message_id else hand.root_id
    if chat.kind != chats.WORK or any(mention["profile_id"] is not None for mention in kept):
        return []
    if not await work_chats.main_line(conn, root):
        return []
    message_id, text, via = hand.message_id, hand.body, hand.via
    said = await work_chats.to_coordinator(conn, chat, user, message_id, text, via)
    return [] if said is None else [said]


async def _direct_agent(
    conn: AsyncConnection, chat: ChatAccess, user: Principal, root: int | None, kept: list[dict]
) -> list[dict]:
    """In direct chat ``chat``, the mention of its agent for a message whose mentions ``kept`` do not reach it: ``via``
    thread when the sender has a session with it in the thread of ``root``, else ``via`` direct; none when they do."""
    found = await direct_mention(conn, chat, kept)
    threaded = await _thread_agent(conn, chat, user, root) if found else []
    return threaded if threaded and threaded[0]["profile_id"] == found[0]["profile_id"] else found


async def deliver_message(
    conn: AsyncConnection,
    chat: ChatAccess,
    user: Principal,
    body: ChatMessageIn,
    *,
    via: str | None = None,
    via_bridge: int | None = None,
    plugin: str | None = None,
    skip: frozenset[str] = frozenset(),
) -> int:
    """Store the message with its mentions, the work they hand the agents, the hub's answer to what they refuse and
    the notices; its id. ``via`` names the kind of channel it came in by and ``via_bridge`` the bridge, which does not
    get it back (``chat_bridge_events``). ``plugin`` names the isolated plugin that posts it in ``user``'s name
    (``plugin_chats_api``): the message comes ``via`` plugin, and its audit row ends with plugin:<id>. The names
    ``skip`` names are not read as mentions: the agents of a system a direct chat's message handed to its entry agent,
    left out of the group the message is copied to (``direct_systems``)."""
    if plugin is not None:
        via = chats.PLUGIN_VIA
    came_by = via
    root, answered = None, None
    if body.reply_to is not None:
        replied = await visible_message(conn, chat, body.reply_to)
        root = replied.thread_root_id or replied.id
        answered = await _answered(conn, replied)
    answer = None
    if body.from_answer is not None:  # a question asked from an interactive answer (``chats.service.answers``)
        answer = await chat_answers.answer_of(conn, chat, body.from_answer)
        root = chat_answers.thread_of(answer, root, body.reply_to)
        await chat_answers.refuse_too_many(conn, user.user_id)
    names = [name for name in chats.mention_names(body.body) if name not in skip]
    kept, people, _ = await resolve_mentions(conn, chat, user, names)
    files = await attachment_store.take(conn, body.attachment_ids, user.user_id, chat_id=chat.chat_id)
    message_id, seq = await insert_message(
        conn,
        chat,
        body.body,
        thread_root_id=root,
        author_user_id=user.user_id,
        via=via,
        via_bridge=via_bridge,
        from_output_id=body.from_answer,
    )
    await attachment_store.hold_in_chat(conn, message_id, files)  # before hand_mentions, whose inboxes carry them
    if answer is not None:
        mention = await chat_answers.answer_mention(conn, chat, answer)
        if not any(found["profile_id"] == mention["profile_id"] for found in kept):
            kept.append(mention)
    via = dispatch_credential(user.kind)
    message = direct_systems.Message(chat, message_id, root or message_id, body.body, via)
    to_systems = await direct_systems.to_systems(conn, message, user, kept)
    routed = [mention for mention in kept if "run_id" in mention]  # the work went to a system's entry agent, or not
    opened = await _open_group(conn, chat, user, root or message_id, [m for m in kept if "run_id" not in m])
    if chat.kind == chats.DIRECT:  # once a group or a system took the agents named, the direct agent takes none
        taken = opened is not None or any(mention["run_id"] is not None for mention in routed)
        kept += [] if taken else await _direct_agent(conn, chat, user, root, kept)
    elif chat.kind == chats.SETTLE:
        kept += await direct_mention(conn, chat, kept)
    elif not any(mention["profile_id"] is not None for mention in kept):
        kept += await _unnamed_agent(conn, chat, user, root, answered, kept)
    hand = Hand(
        chat,
        message_id,
        root or message_id,
        None,  # pyright: ignore[reportArgumentType]  # hand_mentions sets the profile of each mention
        user.user_id,
        user.login,
        via,
        1,
        user.login,
        body.body,
        answers=answered,
    )
    if opened is None:
        await hand_mentions(conn, hand, [mention for mention in kept if all(mention is not r for r in routed)])
    await insert_mentions(conn, message_id, kept)
    refusals = [mention["refusal"] for mention in kept if mention["refusal"]]
    refusals += await _to_coordinator(conn, chat, user, hand, kept)
    said = ([] if opened is None else [opened.note()]) + to_systems + refusals
    await _answered_and_noticed(conn, chat, user, (message_id, root or message_id, seq), said, people, body.body)
    target = f"chat:{chat.chat_id} message:{message_id}" + ("" if plugin is None else f" plugin:{plugin}")
    await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.CHAT_MESSAGE, target=target)
    if opened is not None and opened.chat_id is not None:  # the person's words, where the agents they named are
        group = await chat_access(conn, user, opened.chat_id)
        copy = ChatMessageIn(body=body.body, reply_to=None)
        handed = frozenset(mention["name"] for mention in routed)
        await deliver_message(conn, group, user, copy, via=came_by, plugin=plugin, skip=handed)
    return message_id


async def _answered_and_noticed(
    conn: AsyncConnection,
    chat: ChatAccess,
    user: Principal,
    at: tuple[int, int, int],
    said: list[str],
    people: list[int],
    text: str,
) -> None:
    """After ``deliver_message`` stored message ``at`` (its id, its thread's root and the chat's event seq then): the
    hub's answer ``said`` in its thread, which the sender has read as they read their own, the chat read up to there
    for the sender, and a notice to each person of ``people`` it mentions but the sender."""
    message_id, root, seq = at
    if said:
        await hub_reply(conn, chat.chat_id, root, "\n".join(said))
        seq = await store.event_seq(conn, chat.chat_id)
    await mark_read(conn, chat.member_id, seq)
    for user_id in dict.fromkeys(people):
        if user_id != user.user_id:
            await notice_mention(conn, chat, user_id, message_id, text)


async def _open_group(conn: AsyncConnection, chat: ChatAccess, user: Principal, root: int, kept: list[dict]):
    """The group (``chats.service.groups.Opened``) a message of direct chat ``chat`` in the thread of ``root`` goes on
    in, when its mentions ``kept`` name agents the chat does not have and one of them may go in; None otherwise. Each
    such mention reaches the group, or keeps why it could not."""
    outside = [mention for mention in kept if mention["profile_id"] is not None and mention["via"] is None]
    if chat.kind != chats.DIRECT or not outside:
        return None
    opened = await open_from(conn, chat, user, root, [mention["name"] for mention in outside])
    for mention in outside:
        why = opened.refused.get(mention["name"])
        mention["refusal"] = None if why is None else f"{NOT_IN_DIRECT.format(name=mention['name'])}: {why}"
    return opened if opened.chat_id is not None else None


async def message_view(conn: AsyncConnection, chat: ChatAccess, row, user: Principal) -> GroupMessage:
    """Message ``row`` as ``user`` reads it, with the progress of the runs it handed work to
    (``chats.service.progress``)."""
    progress = by_message(await chat_progress(conn, user, chat.chat_id))
    (view,) = await messages_view(conn, [row], await label_reader(conn, chat.user_id, [row.label]), progress)
    return view

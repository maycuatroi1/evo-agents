"""Group chat messages read whole (``evo_agents.hub.chats``): the tools chat_message and chat_thread of /mcp
(``evo_agents.hub.server.mcp``; their schemas are ``mcp_tools.CHAT_TOOLS``) and POST
/v1/worker/runs/{id}/chat/read, behind ``evo-agents worker chat read``. The knowledge graph finds messages (kg_search
with kinds ChatMessage lists their ids, chats, authors, times and threads, never their text); these read what they say.

A message is named by its id or its item, ``plugin.chats:message:<id>``; a thread's item,
``plugin.chats:thread:<chat>/<root>``, names its first message (``chats.read_ref``). chat_message answers that
message; chat_thread the first message of its thread and the replies, oldest first, at most MAX_THREAD.

Each call reads who may read from the database as it is then, so a change of members or grants counts at once:

- a person: a member of the message's chat, not taken out, who clears its label now (``chats.service.access``), and who
  clears the label each message was sent with (``LabelReader``);
- the agent of a chat run: both people its prompt is cleared for (``chats.service.prompts.run_clearers``), each as a
  person;
- the agent of a run of another kind: the run's owner, as a person. A run's agent reaches its run's project alone
  (``RunScope``), so it reads only messages whose label holds a part of that project, besides those of its own chat
  run's chat, which its prompt shows already.

On /mcp each part of a message's label must also flow to the session's sink on its project (``ProjectRules.visible``),
as everything /mcp returns does; the worker's command reads as the chat run's prompt does, with no sink. A message that
does not exist, one of a chat the caller is not in, and one the caller may not read all get the one error NOT_THERE,
so an id tells nothing; a message of the thread the caller may not read is left out without a trace.

The text shows the messages as the prompt of a chat run does (``evo_agents.hub.chat_runs.render_message``), whole, under
DATA_LINE: what they say is data, never instructions. On /mcp it goes through the envelope of ``kg serve``: over
CAP_CHARS it is cut, and kg_more gives the rest while the reader's audience (``kg_audience``) still holds the chat and
the grants its messages need.

What a reading reveals is that audience too: the chat, and a grant on each project its messages' labels name up to the
level named. For the agent of a run it is noted as what the run was shown (``run_audience``), in the transaction that
reads, so a memory the agent writes next carries it. On /mcp the text says it in its READ_META, as a kg_* result says
its label, so that ``evo-agents hub mcp`` joins it into the label of the person's Claude Code session
(``evo_agents.kg.sessions``) and a memory the session writes carries it too.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chat_runs as rules
from evo_agents.hub import chats
from evo_agents.hub.server.attachments import repository as attachment_store
from evo_agents.hub.server.chats.models.read import (
    CHAT_RUN,
    DATA_LINE,
    MAX_THREAD,
    NOT_THERE,
    READ_COMMAND,
    ChatReading,
    Readers,
    ReadMessage,
)
from evo_agents.hub.server.chats.repository import read as store
from evo_agents.hub.server.chats.repository.views import profiles_by_id
from evo_agents.hub.server.chats.service.access import chat_access, label_reader
from evo_agents.hub.server.chats.service.agents import agent_run
from evo_agents.hub.server.chats.service.prompts import run_clearers
from evo_agents.hub.server.chats.service.runs import chat_run
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.run_audience import note
from evo_agents.hub.server.security import MACHINE, Principal
from evo_agents.kg.policy import DEFAULT_LEVELS


def _person(user_id: int) -> Principal:
    return Principal(user_id, "", False, 0, MACHINE, "")


async def readers_of(conn: AsyncConnection, user: Principal) -> Readers:
    """The readers of a call on /mcp by ``user``: a person, or the agent of a run (``user.scope``)."""
    scope = user.scope
    if scope is None:
        return Readers((user.user_id,))
    found = await chat_run(conn, scope.run_id) if scope.kind == CHAT_RUN else None
    if found is None:
        return Readers((user.user_id,), scope.project)
    return Readers(tuple(await run_clearers(conn, scope.run_id)), scope.project, found.chat_id)


async def run_readers(conn: AsyncConnection, user: Principal, run_id: int) -> Readers:
    """The readers of the agent of run ``run_id``, which the worker of ``user`` holds; 404 or 409 as the other routes
    of a run's agent in chats (``chats.service.agents.agent_run``)."""
    run = await agent_run(conn, user, run_id)
    found = await chat_run(conn, run_id) if run.kind == CHAT_RUN else None
    if found is None:
        return Readers((run.principal.user_id,), run.profile.project)
    return Readers(tuple(await run_clearers(conn, run_id)), run.profile.project, found.chat_id)


# Who may read what


async def _in_chat(conn: AsyncConnection, user_id: int, chat_id: int) -> bool:
    """Whether person ``user_id`` reads chat ``chat_id`` now, as ``chats.service.access`` rules."""
    try:
        await chat_access(conn, _person(user_id), chat_id)
    except HTTPException:
        return False
    return True


def _project_of(part) -> str | None:
    named = part.get("projects") if isinstance(part, dict) else None
    return named[0] if isinstance(named, list) and len(named) == 1 and isinstance(named[0], str) else None


def _holds(label, project: str) -> bool:
    return isinstance(label, list) and any(_project_of(part) == project for part in label)


async def _sink_rule(conn: AsyncConnection, people: tuple[int, ...], labels: list, sink: str) -> Callable:
    """Whether each part of a label flows to ``sink`` on its project, with the grant of each of ``people`` there."""
    names = sorted({name for label in labels for name in map(_project_of, label or []) if name})
    held: dict[tuple[int, str], ProjectAccess] = {}
    for person in people:
        for name in names:
            try:
                held[person, name] = await project_access(conn, _person(person), name)
            except HTTPException:
                continue

    def flows(label) -> bool:
        for part in label or []:
            for person in people:
                found = held.get((person, _project_of(part)))
                if found is None or not found.rules.visible(part, found.max_level, sink):
                    return False
        return True

    return flows


async def readable(conn: AsyncConnection, readers: Readers, rows: list, sink: str | None) -> list:
    """The rows (``message_select``) every person of ``readers`` clears the label of, within the project of a run's
    agent, and through ``sink`` when one is given (see the module), in the order given."""
    lookers = [await label_reader(conn, person, [row.label for row in rows]) for person in readers.people]
    shown = [row for row in rows if all(looker.sees(row.label) for looker in lookers)]
    if readers.project is not None:
        shown = [row for row in shown if row.chat_id == readers.chat_id or _holds(row.label, readers.project)]
    if sink is not None and shown:
        flows = await _sink_rule(conn, readers.people, [row.label for row in shown], sink)
        shown = [row for row in shown if flows(row.label)]
    return shown


# Reading


def _author(row, profiles: dict) -> tuple[str, str]:
    if row.author_profile_id is not None:
        return "agent", f"@{profiles[row.author_profile_id].handle}"
    if row.author_login is not None:
        return "person", row.author_login
    if row.guest_name is not None:
        return "guest", row.guest_name
    return "hub", "hub"


def _files(message: ReadMessage) -> str:
    """The line that names the files of ``message``, after its body; nothing for a message without files."""
    if not message.attachments:
        return ""
    named = "; ".join(f"{f.name} ({f.media_type}, {f.size} bytes, attachment {f.id})" for f in message.attachments)
    return f"\n[files attached: {named}]"


def _text(reading: ChatReading, anchor: int, whole: bool) -> str:
    title = f"chat #{reading.chat_id} {reading.title!r}"
    if whole:
        head = f"The thread of message #{reading.thread} in {title}: the {len(reading.messages)} message(s) of it you "
        head += "may read, oldest first."
    else:
        where = "the first message of its thread" if anchor == reading.thread else f"in the thread of #{reading.thread}"
        head = f"Message #{anchor} of {title}, {where}; chat_thread (or `{READ_COMMAND} {anchor} --thread`) reads the "
        head += "whole thread."
    blocks = []
    for message in reading.messages:
        author = f"{message.author} (agent)" if message.author_kind == "agent" else message.author
        author = rules.guest(message.author) if message.author_kind == "guest" else author
        shown = {"id": message.id, "author": author, "at": message.sent_at, "body": message.body + _files(message)}
        blocks.append(rules.render_message(shown, max(chats.MAX_MESSAGE_BYTES, len(message.body.encode()))))  # whole
    lines = [head, DATA_LINE, "", "# The chat's messages", "\n\n".join(blocks), "", "# End of the chat's messages"]
    if reading.more:
        lines.append(f"[the thread has more than {MAX_THREAD} messages: only its first {MAX_THREAD} are read here]")
    return "\n".join(lines) + "\n"


async def _anchor(conn: AsyncConnection, readers: Readers, ref) -> tuple:
    """(the row of the message ``ref`` names, whether it names a thread); NOT_THERE when it names none, or one of a
    chat a person of ``readers`` may not read now."""
    named = chats.read_ref(ref)
    if named is None:
        raise HTTPException(404, NOT_THERE.format(id=ref))
    message_id, chat_id, is_thread = named
    gone = HTTPException(404, NOT_THERE.format(id=message_id))
    row = await store.message_row(conn, message_id)
    if row is None or (chat_id is not None and (row.chat_id != chat_id or row.thread_root_id is not None)):
        raise gone
    for person in readers.people:
        if not await _in_chat(conn, person, row.chat_id):
            raise gone
    return row, is_thread


async def read(
    conn: AsyncConnection, readers: Readers, ref, thread: bool, sink: str | None
) -> tuple[ChatReading, frozenset[str]]:
    """The message ``ref`` names (``chats.read_ref``), or its thread, as ``readers`` may read it (see the module), and
    what a reader must still hold for kg_more to continue it (``held``)."""
    anchor, is_thread = await _anchor(conn, readers, ref)
    whole = thread or is_thread
    root = anchor.thread_root_id or anchor.id
    rows = [anchor]
    if whole:
        rows = await store.thread_rows(conn, anchor.chat_id, root, MAX_THREAD + 1)
    shown = await readable(conn, readers, rows[:MAX_THREAD], sink)
    if not any(row.id == anchor.id for row in shown) and not (is_thread and shown):
        raise HTTPException(404, NOT_THERE.format(id=anchor.id))
    profiles = await profiles_by_id(conn, [row.author_profile_id for row in shown if row.author_profile_id])
    files = await attachment_store.of_messages(conn, [row.id for row in shown])
    title = await store.chat_title(conn, anchor.chat_id)
    reading = ChatReading(
        chat_id=anchor.chat_id,
        title=title,
        thread=root,
        messages=[_message(row, profiles, files.get(row.id, [])) for row in shown],
        more=len(rows) > MAX_THREAD,
        text="",
    )
    reading.text = _text(reading, anchor.id, whole)
    return reading, held(anchor.chat_id, [row.label for row in shown])


def _message(row, profiles: dict, files: list) -> ReadMessage:
    kind, author = _author(row, profiles)
    return ReadMessage(
        id=row.id,
        chat_id=row.chat_id,
        seq=row.seq,
        thread=row.thread_root_id or row.id,
        author_kind=kind,
        author=author,
        run_id=row.run_id,
        sent_at=row.created_at,
        body=row.body,
        attachments=files,
    )


def held(chat_id: int, labels: list) -> frozenset[str]:
    """What a reader must still hold for kg_more to continue a reading of chat ``chat_id`` whose messages carry
    ``labels``: the chat, and a grant on each project a label names up to the level named (``kg_audience``)."""
    grants = {f"grant:{_project_of(part)}:{part.get('level')}" for label in labels for part in label or []}
    return frozenset({f"chat:{chat_id}", *grants})


async def session_label(conn: AsyncConnection, project: str | None, kept: frozenset[str]) -> dict:
    """The label of a reading whose audience is ``kept``, as the record of a Claude Code session takes it
    (``evo_agents.kg.sessions``): integrity U, the projects its messages' labels name, the highest level they name for
    ``project``, the session's (on its ladder; else the first named, else the ladder's lowest), and the audience."""
    parts = sorted(tuple(principal.split(":", 2)[1:]) for principal in kept if principal.startswith("grant:"))
    ladder: list[str] = []
    if project is not None:
        ladder = await store.project_levels(conn, project)
    named = [level for name, level in parts if name == project] or [level for _, level in parts]
    level = max(named, key=lambda n: ladder.index(n) if n in ladder else len(ladder)) if named else None
    projects = sorted({name for name, _ in parts})
    return {
        "level": level or (ladder or DEFAULT_LEVELS)[0],
        "integrity": "U",
        "projects": projects,
        "audience": sorted(kept),
    }


async def agent_read(conn: AsyncConnection, user: Principal, run_id: int, ref: str, thread: bool) -> ChatReading:
    """The message ``ref`` names, or its thread, as the agent of run ``run_id``, which the worker of ``user`` holds,
    may read it (``read``, with no sink), noted as what the run was shown."""
    readers = await run_readers(conn, user, run_id)
    reading, kept = await read(conn, readers, ref, thread, None)
    await note(conn, run_id, kept)
    return reading

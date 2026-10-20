"""The messages of the hub's group chats as the items of a project's knowledge graph: what a source of the chats
answers a build of the graph (``evo_agents.hub.kg_sources``), the core plugin chats' through the API of core plugins
(``core_plugin_api.chat_history_items``). The label rule of chats stays here, beside ``chat_access``, so a plugin
hands the hub's items on and never decides who reads a message.

The hub keeps each message's label as its chat had it when it was sent: a part per project with an agent in the chat
then, by names (``evo_agents.hub.server.chat_access``). In the graph of project P, each message whose label holds a
part of P is:

- the item ``<source>:message:<id>`` of kind ``message``, a ChatMessage node (``plugin.chats:message:<id>`` for the
  plugin chats), named by the hub ``#<id> <login or @handle> <UTC time>``: the id chat_message and ``chat show`` call
  it by, not its seq in the chat. Its text is the message's body, which search indexes and the link stage reads for
  mentions; no tool returns it (it is free text). Its props: ``chat``, ``thread`` (the id of the thread's first
  message), ``seq``, ``author_kind`` (person, agent or hub), ``author`` (a login, an @handle, or hub), ``sent_at``
  and, for an agent's, ``run``, the run that posted it. Its parent is its thread.
- labelled with P's part (its level, location and integrity), P alone, and the audience ``chat:<id>`` with
  ``grant:<Q>:<level>`` for the part of each other project Q: a reader sees it while they are in the chat and hold
  what ``LabelReader`` asks of who reads it there. A level Q's ladder lacks reads as its highest, as it does there.

A message without a part of P is not in P's graph, so one with an empty label (sent while the chat had no agent) is in
none. One whose label has a part the hub cannot read, or of a project it no longer has, is in none either: nobody
clears it. A thread is the item ``<source>:thread:<chat>/<root>`` of kind ``thread`` (ChatThread), named by the
chat's title and its first message's id, whose parent is the chat's item, ``<source>:chat:<id>`` of kind ``chat``
(Chat), named by its title. Each carries the join of the labels of its messages in that graph. Edges the link stage
makes from what a message mentions live on the message's unit, so they carry its label, audience included.

The answer's cursor is the newest message given, its item and when it was sent, which kg_status names. The source gives
nothing while the project does not grant its plugin ``chats:read``. It adds no identifiers.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.plugin_calls import effective_grant, enabled_in
from evo_agents.hub.server.chat_access import LabelProject, projects_named
from evo_agents.kg.ids import utc_text

READS = "chats:read"  # what a source of the chats needs the project to grant its plugin
CHAT, THREAD, MESSAGE = "chat", "thread", "message"  # the kinds of the items, and the kinds in their ids
NAME_TIME = "%Y-%m-%dT%H:%M:%SZ"  # the time in a message's name


def _messages(project: str):
    """Every message whose label holds a part of ``project``, with its chat's title and its author, in order."""
    cm, c, u, ap = tables.chat_messages, tables.chats, tables.users, tables.agent_profiles
    joined = (
        cm.join(c, c.c.id == cm.c.chat_id)
        .outerjoin(u, u.c.id == cm.c.author_user_id)
        .outerjoin(ap, ap.c.id == cm.c.author_profile_id)
    )
    return (
        select(
            cm.c.id,
            cm.c.chat_id,
            cm.c.seq,
            cm.c.thread_root_id,
            cm.c.run_id,
            cm.c.body,
            cm.c.label,
            cm.c.created_at,
            c.c.title,
            u.c.login,
            ap.c.handle,
        )
        .select_from(joined)
        .where(cm.c.label.contains([{"projects": [project]}]))
        .order_by(cm.c.id)
    )


def _project_of(part) -> str | None:
    named = part.get("projects") if isinstance(part, dict) else None
    return named[0] if isinstance(named, list) and len(named) == 1 and isinstance(named[0], str) else None


def message_label(label, chat_id: int, project: str, ladders: dict[str, LabelProject]) -> dict | None:
    """The label in the graph of ``project`` of a message of chat ``chat_id`` labelled ``label`` (see the module), by
    names; None when the message stays out of that graph. ``ladders`` holds the projects the label names."""
    own, audience = None, {f"chat:{chat_id}"}
    for part in label if isinstance(label, list) else [None]:
        name = _project_of(part)
        if name is None or name not in ladders:
            return None  # a part LabelReader reads as cleared by nobody
        if name == project:
            own = part
            continue
        levels = ladders[name].levels
        audience.add(f"grant:{name}:{part.get('level') if part.get('level') in levels else levels[-1]}")
    if own is None:
        return None
    found = {key: own[key] for key in ("level", "location", "integrity") if own.get(key) is not None}
    return {**found, "projects": [project], "audience": sorted(audience)}


def _rank(names: list[str], name) -> int:
    return names.index(name) if name in names else len(names) - 1


def join(labels: list[dict], project: LabelProject) -> dict:
    """The join of message labels ``labels`` (by names) on the ladder of ``project``: the highest level and location,
    integrity U unless every one is T, and the union of their audiences."""
    level = max((label.get("level") for label in labels), key=lambda name: _rank(project.levels, name))
    found = {"level": level, "integrity": "T" if all(label.get("integrity") == "T" for label in labels) else "U"}
    located = [label["location"] for label in labels if label.get("location") is not None]
    if located:
        found["location"] = max(located, key=lambda name: _rank(project.locations, name))
    audience = sorted({principal for label in labels for principal in label["audience"]})
    return {**found, "projects": [project.name], "audience": audience}


def _author(row) -> tuple[str, str]:
    if row.login is not None:
        return "person", row.login
    if row.handle is not None:
        return "agent", f"@{row.handle}"
    return "hub", "hub"


def message_item(source: str, row, label: dict) -> dict:
    """The kg/1 item of the message of ``row`` (of ``_messages``), labelled ``label`` (see the module)."""
    kind, author = _author(row)
    root = row.thread_root_id or row.id
    props = {
        "chat": row.chat_id,
        "thread": root,
        "seq": row.seq,
        "author_kind": kind,
        "author": author,
        "sent_at": utc_text(row.created_at),
    }
    if kind == "agent" and row.run_id is not None:
        props["run"] = row.run_id
    item = {
        "id": f"{source}:{MESSAGE}:{row.id}",
        "kind": MESSAGE,
        "title": f"#{row.id} {author} {row.created_at.astimezone(UTC).strftime(NAME_TIME)}",
        "parent": f"{source}:{THREAD}:{row.chat_id}/{root}",
        "rev_time": utc_text(row.created_at),
        "props": props,
        "label": label,
    }
    if row.body.strip():
        item["body"] = {"format": "markdown", "text": row.body}
    return item


def _group_item(source: str, kind: str, key: str, title: str, props: dict, members: list[tuple], rules) -> dict:
    """The item of a thread or a chat whose messages in the graph are ``members``, (row, label) pairs: the join of
    their labels on the ladder of ``rules``, the project, and the time of the newest."""
    return {
        "id": f"{source}:{kind}:{key}",
        "kind": kind,
        "title": title,
        "rev_time": max(utc_text(row.created_at) for row, _ in members),
        "props": props,
        "label": join([label for _, label in members], rules),
    }


async def read(conn: AsyncConnection, plugin: str, source: str, project_id: int, project: str) -> dict:
    """The answer of the source ``source`` of plugin ``plugin`` for ``project`` (see the module): empty while the
    project does not enable the plugin and grant it ``chats:read``."""
    empty = {"items": [], "identifiers": []}
    enabled = await enabled_in(conn, plugin, project_id)
    if enabled is None or READS not in effective_grant(enabled):
        return empty
    rows = list(await conn.execute(_messages(project)))
    names = {_project_of(part) for row in rows if isinstance(row.label, list) for part in row.label}
    ladders = await projects_named(conn, {name for name in names if name is not None} | {project})
    if project not in ladders:
        return empty
    threads: dict[tuple[int, int], list[tuple]] = defaultdict(list)
    chats: dict[int, list[tuple]] = defaultdict(list)
    items, newest = [], None
    for row in rows:
        label = message_label(row.label, row.chat_id, project, ladders)
        if label is None:
            continue
        items.append(message_item(source, row, label))
        threads[(row.chat_id, row.thread_root_id or row.id)].append((row, label))
        chats[row.chat_id].append((row, label))
        newest = row
    if newest is None:
        return empty
    rules = ladders[project]
    for (chat_id, root), members in threads.items():
        title = f"{members[0][0].title} (thread {root})"
        item = _group_item(source, THREAD, f"{chat_id}/{root}", title, {"chat": chat_id, "root": root}, members, rules)
        items.append({**item, "parent": f"{source}:{CHAT}:{chat_id}"})
    for chat_id, members in chats.items():
        items.append(_group_item(source, CHAT, str(chat_id), members[0][0].title, {"chat": chat_id}, members, rules))
    cursor = {"item": f"{source}:{MESSAGE}:{newest.id}", "at": utc_text(newest.created_at)}
    return {"items": items, "identifiers": [], "cursor": cursor}


__all__ = ["READS", "join", "message_item", "message_label", "read"]

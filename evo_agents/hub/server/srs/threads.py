"""Threads of comments anchored to a passage of an item, the people told of an answer, and @ba asked to answer.

- ``anchor``: whether the passage a thread is anchored to (its ``quote``) is still in its item in a version: the item's
  text (title, fields, steps, body) and the quote compared once Markdown marks, table bars and runs of white space are
  left out (``plain``). ``found`` when it is, ``changed`` when the words changed or the version has no such item; a
  thread on a whole item, a version or the document has no anchor. The web finds the passage in what it draws, the
  hub in the Markdown, so the two agree on what a reader selected.
- ``threads``: the threads of a document as roots with their answers, oldest first, those of an item and the items under
  it, or those open or resolved.
- ``told``: after a comment, the notice ``srs_comment`` to everyone in its thread (the author of each comment but an
  agent's, and the member who resolved it), the members it names (``@login``) and the author of the item's revision (a
  use case's, or the version's for another item), never its own author; its link opens the thread on the item.
- ``asked``: a comment of a member that names ``@ba`` hands @ba a task run that reads the thread (``srs__read`` with
  ``thread``) and answers it in that thread (``srs__comment`` with ``parent_id``). The run keeps the comment
  (``ask_run_id``). An agent's comment asks nothing, and no agent resolves or reopens a thread.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import signals, tables
from evo_agents.hub.server.notifications import notify
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.srs import BA, revisions, store
from evo_agents.hub.server.srs.doc import Document, Item
from evo_agents.hub.server.srs.reviews import CommentRow
from evo_agents.hub.server.srs.store import PLUGIN, DocumentRow

log = logging.getLogger(__name__)

FOUND, CHANGED = "found", "changed"
NOTICE = "srs_comment"  # evo_agents.hub.runs.NOTICE_KINDS: a comment reached a thread the member is in
ASKED = re.compile(rf"(?<![\w@./-])@{BA}\b", re.IGNORECASE)
NAMED = re.compile(r"(?<![\w@./-])@([A-Za-z0-9][A-Za-z0-9_.-]{0,38})")
QUOTED_CHARS = 160  # of a passage, in a notice and a task


# Anchoring


def _text(value) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for one in value.values():
            yield from _text(one)
    elif isinstance(value, list | tuple):
        for one in value:
            yield from _text(one)


def plain(text: str) -> str:
    """``text`` as words alone: links as their text, Markdown marks, table bars and headings marks left out, white space
    as one space, case folded."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[*_`|#>~]", " ", text)
    return " ".join(text.split()).casefold()


def item_text(item: Item) -> str:
    """Everything a reader sees of ``item`` on its own: its title, the values of its fields, its body."""
    return " ".join([item.title, *(text for name, value in item.fields.items() for text in _text(value)), item.body])


def anchor(content: Document | None, key: str | None, quote: str | None) -> str | None:
    """``found`` or ``changed`` for a passage ``quote`` of item ``key`` in ``content`` (see the module); None for no
    passage."""
    if not quote or not key:
        return None
    item = next((one for one in content.items if one.key == key), None) if content is not None else None
    if item is None:
        return CHANGED
    wanted = plain(quote)
    return FOUND if wanted and wanted in plain(item_text(item)) else CHANGED


# Threads


@dataclass
class Thread:
    root: CommentRow
    replies: list[CommentRow] = field(default_factory=list)

    @property
    def open(self) -> bool:
        return self.root.resolved_at is None


def threads(rows: Sequence[CommentRow]) -> list[Thread]:
    """The threads of ``rows``, by root, oldest first; an answer whose root is not among them is left out."""
    found = {row.id: Thread(row) for row in rows if row.parent_id is None}
    for row in rows:
        if row.parent_id is not None and row.parent_id in found:
            found[row.parent_id].replies.append(row)
    return list(found.values())


def under(content: Document | None, key: str) -> set[str]:
    """``key`` and the keys of the items under it in ``content``."""
    if content is None:
        return {key}
    at = next((index for index, item in enumerate(content.items) if item.key == key), None)
    if at is None:
        return {key}
    return {key} | {item.key for index, item in enumerate(content.items) if at in content.ancestors(index) and item.key}


def chosen(found: list[Thread], keys: set[str] | None, status: str | None) -> list[Thread]:
    """The threads of ``found`` on an item of ``keys`` (every one when None), open or resolved as ``status`` asks."""
    return [
        thread
        for thread in found
        if (keys is None or thread.root.item in keys) and (status is None or (status == "open") == thread.open)
    ]


# Who is told, and @ba asked


def _clip(text: str, chars: int = QUOTED_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= chars else text[: chars - 1].rstrip() + "…"


def thread_path(project: str, slug: str, item: str | None, thread_id: int) -> str:
    """The thread on the hub's web: the document's contents at its item, the thread open beside it."""
    at = f"s={item}&" if item else ""
    return f"{store.document_path(project, slug)}?{at}c={thread_id}"


async def _revision_author(conn: AsyncConnection, doc: DocumentRow, content: Document, key: str) -> int | None:
    """The author of the revision the use case ``key`` sits under (itself, or the use case above a business rule) has
    in the latest version; the latest version's author for an item under no use case."""
    above = next(
        (
            content.items[at].key
            for index, item in enumerate(content.items)
            if item.key == key
            for at in (index, *content.ancestors(index))
            if content.items[at].kind == "use_case"
        ),
        None,
    )
    if above is not None:
        revision = (await revisions.history(conn, doc.id)).latest(above)
        if revision is not None:
            return revision.author_id
    v = tables.srs.versions
    return await conn.scalar(select(v.c.author_id).where(v.c.document_id == doc.id, v.c.number == doc.latest_version))


async def _members(conn: AsyncConnection, project_id: int, logins: Iterable[str]) -> set[int]:
    """The ids of the members of project ``project_id`` among ``logins`` (case-insensitive)."""
    wanted = {login.lower() for login in logins}
    if not wanted:
        return set()
    u, g = tables.users, tables.grants
    query = (
        select(u.c.id)
        .join_from(u, g, g.c.user_id == u.c.id)
        .where(g.c.project_id == project_id, func.lower(u.c.login).in_(sorted(wanted)))
    )
    return set((await conn.execute(query)).scalars())


async def told(
    conn: AsyncConnection, access: ProjectAccess, doc: DocumentRow, thread: Thread, comment: CommentRow
) -> list[int]:
    """Send ``srs_comment`` of ``comment`` (in ``thread``) to whom the module names; their ids."""
    content = (await store.snapshot(conn, await store.version(conn, doc))).doc if doc.latest_version else None
    people = {row.author_id for row in (thread.root, *thread.replies) if row.run_id is None and row.author_id}
    c = tables.srs.comments
    resolver = await conn.scalar(select(c.c.resolved_by).where(c.c.id == thread.root.id))
    people |= {resolver} if resolver else set()
    people |= await _members(conn, access.project_id, NAMED.findall(comment.body))
    if thread.root.item and content is not None:
        author = await _revision_author(conn, doc, content, thread.root.item)
        people |= {author} if author else set()
    people.discard(comment.author_id)
    people.discard(None)
    if not people:
        return []
    rules = await signals.project_rules(conn, access.project_id)
    path = thread_path(rules.name, doc.slug, thread.root.item, thread.root.id)
    about = thread.root.item_code or (f"an item of {doc.slug}" if thread.root.item else doc.slug)
    who = f"@{BA}" if comment.run_id else comment.author
    title = f"{who} commented on {about} in {rules.name}"
    quoted = f"> {_clip(thread.root.quote)}\n\n" if thread.root.quote else ""
    body = f"{quoted}{_clip(comment.body, 600)}\n{store.web_url(path)}"
    details = {
        "document": doc.slug,
        "document_id": doc.id,
        "thread": thread.root.id,
        "comment": comment.id,
        "item": thread.root.item,
        "item_code": thread.root.item_code,
    }
    for user_id in sorted(people):
        await notify(
            conn,
            user_id=user_id,
            kind="notice",
            notice_kind=NOTICE,
            title=title,
            body=body,
            project_id=access.project_id,
            details=details,
            link=path,
        )
    return sorted(people)


def ask_task(project: str, doc: DocumentRow, thread: Thread, comment: CommentRow) -> str:
    """The task of @ba's run that answers ``thread`` of ``doc``, where ``comment`` named it."""
    about = thread.root.item_code or (f"item {thread.root.item}" if thread.root.item else "the whole document")
    passage = f' The thread is on this passage: "{_clip(thread.root.quote, 400)}".' if thread.root.quote else ""
    return "\n".join(
        [
            f"{comment.author} asked you in a comment thread of the SRS document {doc.slug} of project {project}, "
            f"about {about}.{passage}",
            "",
            f"1. Read the thread with srs__read (document {doc.slug}, thread {thread.root.id}): the passage, whether it "
            "is still in the latest version, and every comment of it.",
            "2. Read what the question needs: the item with srs__read (item: its code or key), other documents with "
            "srs__search, the knowledge graph with kg_search and kg_context.",
            f"3. Answer once, in that thread: srs__comment (document {doc.slug}, parent_id {thread.root.id}, body). Answer "
            "the question asked, in the language it was asked in, and name your sources. When you cannot answer, say "
            "what is missing.",
            "",
            "You write that one answer and nothing else: you resolve no thread, approve, return or propose nothing, "
            "record no finding, create no task and change no file of the repositories of this run.",
        ]
    )


async def asked(
    conn: AsyncConnection, access: ProjectAccess, doc: DocumentRow, thread: Thread, comment: CommentRow
) -> int | None:
    """Hand @ba the answer of ``thread`` when ``comment``, a member's, names @ba; the task run, or None."""
    if comment.run_id is not None or not ASKED.search(comment.body):
        return None
    # Imported here: task_dispatch's own imports go round a cycle when it is the first module of the server loaded.
    from evo_agents.hub.server.task_dispatch import hand_own_task

    title = f"@{BA} answers a thread of {doc.slug} ({thread.root.item_code or doc.slug})"
    run_id, why = await hand_own_task(
        conn, PLUGIN, access.project_id, BA, ask_task(access.name, doc, thread, comment), title
    )
    if run_id is None:
        log.info("no answer by @ba", extra={"project": access.name, "document": doc.slug, "why": why})
    if run_id is not None:
        c = tables.srs.comments
        await conn.execute(update(c).where(c.c.id == comment.id).values(ask_run_id=run_id))
    return run_id


def thread_text(slug: str, thread: Thread, state: str | None, number: int | None) -> str:
    """A thread as an agent reads it (``srs__read`` with ``thread``)."""
    root = thread.root
    about = root.item_code or root.item or "the whole document"
    lines = [f"Thread {root.id} of {slug}, on {about}, {'open' if thread.open else f'resolved by {root.resolved_by}'}."]
    if root.quote:
        where = f" (in {root.field})" if root.field else ""
        said = {FOUND: "still in", CHANGED: "no longer in"}.get(state or "", "checked against")
        lines.append(f'Passage{where}, made on version {root.version}, {said} version {number}: "{root.quote}"')
    for row in (root, *thread.replies):
        by = f"@{BA} (run #{row.run_id})" if row.run_id else row.author
        lines += ["", f"#{row.id} {by}, {row.created_at:%Y-%m-%d %H:%M}:", row.body]
    return "\n".join(lines)


__all__ = [
    "CHANGED",
    "FOUND",
    "NOTICE",
    "Thread",
    "anchor",
    "ask_task",
    "asked",
    "chosen",
    "item_text",
    "plain",
    "thread_path",
    "thread_text",
    "threads",
    "told",
    "under",
]

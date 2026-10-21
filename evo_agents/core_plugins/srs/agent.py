"""The tools of the plugin srs on the hub's /mcp, which @ba's agent reads, proposes and comments with (criterion a7 of
plan srs-tasks-plugins): ``srs__read``, ``srs__search``, ``srs__diff``, ``srs__propose`` and ``srs__comment``; and
``srs__finding``, which it records and settles conflicts with (criterion a4 of plan srs-conflicts-and-tasks).

The hub lists them in a session whose project enabled the plugin, and checks their arguments against the manifest's
schemas first. Each acts for a person (``acting``): the agent of a chat run for the person who mentioned the agent,
the origin of the chain (``chat_runs.ChatRun.dispatcher_id``), also when the run went to the worker of the type's
run_as; the agent of any other run for the member who dispatched it; anyone else for themselves. A document is read
when that person sees it through the session's sink and, when the run's worker is another member's, when that member
does too: what a tool answers reaches the machine the agent runs on.

- ``read``, ``search`` and ``diff`` answer anyone with a grant on the project. ``search`` reads the latest version of
  each document, a draft or a version in review included. ``read`` of a version lists the open findings of its
  document, which @ba's review goes over (``auto_review``). An archived document is out of ``read``'s list of
  documents and of ``search``, and still read by its slug; ``propose`` and ``comment`` refuse it.
- ``propose`` answers the agent of a chat run of @ba alone (the profile of the project's agent type ``ba``, while it
  is on). Its changes, checked as a save is, become the document's next version, written as the person and drafted by
  the run, and go to review at once; the person, its author, never approves it unless the project turned the
  config's ``self_approval`` on, and the answer says which. In the settle chat of a version of the document
  (``settle``) the version is written as the author of that version instead, who must hold writer, whoever woke @ba
  there: a member who settled a conflict keeps the right to approve what follows from it. Its schema takes no drop,
  and a code an item holds never changes (``writes.store_problems``). A version it sends to review is reviewed by @ba
  in turn (``auto_review``), as every version is each time it enters review.
- ``comment`` answers the agent of a run of @ba: a chat run, or the review run of ``auto_review``. The comment is the
  person's, drafted by the run (``srs_comments.run_id``).
- ``finding`` answers the agent of a run of @ba too, whatever the role of the person it acts for: a reader who wakes
  @ba where conflicts are settled gets them settled. It records a finding with the sources that show it, resolves or
  waives an open one, or reopens one settled at most 24 hours ago (``findings``). Every source is looked up before
  anything is written: its nodes and paths in the project's knowledge graph on the hub as the run reads it, before the
  document is locked, then its codes and versions; one not found refuses the call. A resolution names the member who
  settled it, who holds a grant on the project, or the sources that settled it. The finding is the run's
  (``srs_findings.run_id``).

Nothing here approves or returns a version: the review route refuses the agent of a run. A refusal is the tool's
error text, and nothing is written.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.srs import BA, checks, findings, markdown, reviews, store
from evo_agents.core_plugins.srs.auto_review import review_entered
from evo_agents.core_plugins.srs.diff import diff as item_changes
from evo_agents.core_plugins.srs.diff import text_of
from evo_agents.core_plugins.srs.doc import Document, SrsError
from evo_agents.core_plugins.srs.edits import edited
from evo_agents.core_plugins.srs.models import SrsChange, edit_changes
from evo_agents.core_plugins.srs.settle import proposer
from evo_agents.core_plugins.srs.store import compare_path
from evo_agents.core_plugins.srs.template import code_key
from evo_agents.core_plugins.srs.writes import Writer, give_screen_codes, write_version
from evo_agents.hub import tables
from evo_agents.hub.plugin_calls import CoreContext, enabled_in
from evo_agents.hub.server.agent_types import enabled_type
from evo_agents.hub.server.chat_runs import chat_run
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import MACHINE, Principal

log = logging.getLogger(__name__)

MAX_HITS = 50  # of a search
SHOWN_PROBLEMS = 10
SHOWN_FINDINGS = 50  # the open findings srs__read prints whole; the others by id
CHANGES = TypeAdapter(list[SrsChange])
RACE = "another save in the project took a code or a version number at the same moment: nothing was written; try again"


def _text(text: str, *, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": error}


# Who a tool acts for


@dataclass(frozen=True)
class Acting:
    """The person a tool acts for, their access to the project, the session's sink, and for the agent of a run: the
    run, its kind, whether it is a run of @ba, and the access of the member whose worker runs it when another."""

    access: ProjectAccess
    person: Principal
    sink: str | None
    run_id: int | None = None
    run_kind: str | None = None
    ba: bool = False
    worker: ProjectAccess | None = None

    def sees(self, doc: store.DocumentRow) -> bool:
        """Whether the member whose worker runs the agent sees ``doc`` too (always without a run of another's)."""
        if self.worker is None:
            return True
        through = store.sink_of(self.worker, self.sink)
        return through is not None and self.worker.visible(doc.label, through)


async def _principal(conn: AsyncConnection, user_id: int, config) -> Principal:
    login = (await conn.execute(select(tables.users.c.login).where(tables.users.c.id == user_id))).scalar_one()
    return Principal(user_id, login, config is not None and config.is_admin(login), None, MACHINE, "")


async def _access(conn: AsyncConnection, person: Principal, project: str) -> ProjectAccess:
    try:
        return await project_access(conn, person, project)
    except HTTPException:
        raise SrsError(403, f"{person.login} holds no grant on project {project} now") from None


async def _is_ba(conn: AsyncConnection, context: CoreContext, profile_id: int | None) -> bool:
    """Whether ``profile_id`` is the profile of the project's agent type ba, enabled and on."""
    plugin = await enabled_in(conn, context.plugin, context.project_id)
    kind = await enabled_type(conn, context.project_id, plugin.id, BA) if plugin is not None else None
    return (
        kind is not None
        and kind.disabled_at is None
        and kind.profile_deleted_at is None
        and kind.profile_id == profile_id
    )


async def acting(conn: AsyncConnection, params: dict, context: CoreContext) -> Acting:
    """Who the tool of ``params`` acts for (see the module)."""
    if context.run_id is None:
        u = tables.users
        login = (params.get("caller") or {}).get("login") or ""
        user_id = (await conn.execute(select(u.c.id).where(u.c.login == login))).scalar_one_or_none()
        if user_id is None:
            raise SrsError(403, f"no member {login} on the hub")
        person = await _principal(conn, user_id, context.config)
        return Acting(await _access(conn, person, context.project), person, context.sink)
    r = tables.runs
    row = (
        await conn.execute(select(r.c.kind, r.c.profile_id, r.c.dispatched_by).where(r.c.id == context.run_id))
    ).one()
    person_id = row.dispatched_by
    found = await chat_run(conn, context.run_id) if row.kind == "chat" else None
    if found is not None:
        person_id = found.dispatcher_id
    person = await _principal(conn, person_id, context.config)
    access = await _access(conn, person, context.project)
    worker = None
    if person_id != row.dispatched_by:
        worker = await _access(conn, await _principal(conn, row.dispatched_by, context.config), context.project)
    ba = await _is_ba(conn, context, row.profile_id)
    return Acting(access, person, context.sink, context.run_id, row.kind, ba, worker)


async def _document(conn: AsyncConnection, who: Acting, slug: str, *, lock: bool = False) -> store.DocumentRow:
    doc = await store.document(conn, who.access, slug, who.sink, lock=lock)
    if not who.sees(doc):
        raise store.missing(who.access.name, slug)
    return doc


async def _answer(params: dict, context: CoreContext, work) -> dict:
    """``work(conn, acting)`` in one transaction, its text the tool's answer; a refusal as the tool's error."""
    try:
        async with context.engine.begin() as conn:
            return _text(await work(conn, await acting(conn, params, context), params.get("arguments") or {}))
    except SrsError as exc:
        shown = [problem.message for problem in exc.problems[:SHOWN_PROBLEMS]]
        lines = [f"error: {exc.message}", *(f"- {message}" for message in shown)]
        for conflict in exc.detail or []:
            if isinstance(conflict, dict) and "latest" in conflict:
                lines.append(f"- {conflict.get('code') or conflict.get('item')} now reads:\n{conflict['latest']}")
            elif isinstance(conflict, str):  # each source a finding names that was not found
                lines.append(f"- {conflict}")
        return _text("\n".join(lines), error=True)
    except IntegrityError:
        return _text(f"error: {RACE}", error=True)


# Reading


def _version_line(doc: store.DocumentRow, row: store.VersionRow) -> str:
    approved = f"version {doc.approved_version} approved last" if doc.approved_version else "no version approved yet"
    drafted = f", drafted by run #{row.drafted_by_run}" if row.drafted_by_run else ""
    return (
        f"{doc.slug} ({doc.kind}, template {doc.template}): {doc.title}\n"
        f"Version {row.number}, {row.state}, round {row.round}, by {row.author}{drafted}; {approved}."
    )


def _item_line(slug: str, number: int, item) -> str:
    return f"- {slug} v{number}: {item.code or '-'} ({item.kind}) {item.title} [key {item.key}]"


async def _documents(conn: AsyncConnection, who: Acting) -> str:
    rows = [doc for doc in await store.documents(conn, who.access, who.sink) if who.sees(doc)]
    if not rows:
        return f"Project {who.access.name} has no SRS document that {who.person.login} sees."
    lines = [f"The SRS documents of project {who.access.name}:"]
    for doc in rows:
        latest = f"latest version {doc.latest_version} ({doc.latest_state})" if doc.latest_version else "no version"
        approved = f", approved {doc.approved_version}" if doc.approved_version else ""
        lines.append(f"- {doc.slug} ({doc.kind}): {doc.title}; {latest}{approved}")
    return "\n".join(lines)


def _item_text(doc: store.DocumentRow, row: store.VersionRow, content: Document, ref: str) -> str:
    wanted = code_key(ref)
    found = next(
        (
            at
            for at, item in enumerate(content.items)
            if item.key == ref or wanted in {code_key(code) for code in item.codes()}
        ),
        None,
    )
    if found is None:
        raise SrsError(404, f"version {row.number} of {doc.slug} has no item {ref}")
    item = content.items[found]
    path = " > ".join(content.items[at].title for at in reversed(content.ancestors(found)))
    aliases = f"; also {', '.join(item.aliases)}" if item.aliases else ""
    lines = [
        _version_line(doc, row),
        f"Item {item.code or item.key} ({item.kind}), key {item.key}{aliases}; under: {path or 'the top'}",
        "",
        text_of(item),
    ]
    under = [content.items[at] for at in content.children(found)]
    if under:
        lines += ["", "Items under it:", *(_item_line(doc.slug, row.number, child) for child in under)]
    return "\n".join(lines)


def _open_lines(slug: str, rows: list[findings.FindingRow]) -> list[str]:
    """The open findings of document ``slug``, each on one line, the first SHOWN_FINDINGS of them whole."""
    if not rows:
        return []
    lines = [f"Open findings of {slug} ({len(rows)}):"]
    for row in rows[:SHOWN_FINDINGS]:
        about = f", {row.item_code or row.item}" if row.item else ""
        sources = "; ".join(str(findings.Source(**source)) for source in row.sources)
        lines.append(f"- #{row.id} {row.level} {row.kind}, on version {row.version}{about}: {row.summary} [{sources}]")
    if len(rows) > SHOWN_FINDINGS:
        lines.append("- also open: " + ", ".join(f"#{row.id}" for row in rows[SHOWN_FINDINGS:]))
    return lines


async def _read(conn: AsyncConnection, who: Acting, args: dict) -> str:
    if not args.get("document"):
        return await _documents(conn, who)
    doc = await _document(conn, who, args["document"])
    row = await store.version(conn, doc, args.get("version"))
    snap = await store.snapshot(conn, row)
    if args.get("item"):
        return _item_text(doc, row, snap.doc, args["item"])
    problems = checks.problems(snap.doc, (await store.settings(conn, who.access)).template)
    lines = [_version_line(doc, row)]
    if problems:
        lines.append(f"Problems ({len(problems)}):")
        lines += [f"- {p.level}: {p.message}" for p in problems[:SHOWN_PROBLEMS]]
    lines += _open_lines(doc.slug, await findings.findings(conn, doc, findings.OPEN))
    lines += ["", markdown.render(snap.doc)]
    return "\n".join(lines)


async def read(params: dict, context: CoreContext) -> dict:
    """The tool ``srs__read``: the documents, a version as Markdown, or an item with the items under it."""
    return await _answer(params, context, _read)


async def _search(conn: AsyncConnection, who: Acting, args: dict) -> str:
    query = args["query"].strip()
    wanted, words, kinds = code_key(query), query.lower(), set(args.get("kinds") or ())
    hits = []
    for doc in await store.documents(conn, who.access, who.sink):
        if not doc.latest_version or not who.sees(doc) or args.get("document") not in (None, doc.slug):
            continue
        row = await store.version(conn, doc)
        for item in (await store.snapshot(conn, row)).doc.items:
            if kinds and item.kind not in kinds:
                continue
            by_code = wanted in {code_key(code) for code in item.codes()}
            if by_code or words in item.title.lower() or words in item.body.lower():
                hits.append((not by_code, _item_line(doc.slug, row.number, item)))
    if not hits:
        return f"No item of the latest versions matches {query!r}."
    hits.sort(key=lambda hit: hit[0])
    shown = [line for _, line in hits[:MAX_HITS]]
    more = f" (the first {MAX_HITS} of {len(hits)})" if len(hits) > MAX_HITS else ""
    return "\n".join([f"Items that match {query!r}{more}, codes first:", *shown])


async def search(params: dict, context: CoreContext) -> dict:
    """The tool ``srs__search``: items of the latest versions by code, alias or words."""
    return await _answer(params, context, _search)


def _changes_text(slug: str, low: int, high: int, old: Document, new: Document) -> str:
    changes = item_changes(old, new)
    lines = [f"{slug}: version {low} to version {high}, {len(changes)} item(s) changed."]
    for change in changes:
        title = change.title_to or change.title_from or ""
        lines += ["", f"## {change.change}: {change.code or change.key} ({change.kind}) {title}", *change.lines]
    return "\n".join(lines)


async def _diff(conn: AsyncConnection, who: Acting, args: dict) -> str:
    doc = await _document(conn, who, args["document"])
    old = await store.snapshot(conn, await store.version(conn, doc, args["from"]))
    new = await store.snapshot(conn, await store.version(conn, doc, args["to"]))
    return _changes_text(doc.slug, args["from"], args["to"], old.doc, new.doc)


async def diff(params: dict, context: CoreContext) -> dict:
    """The tool ``srs__diff``: what changed between two versions, item by item."""
    return await _answer(params, context, _diff)


# Writing


def _ba_run(who: Acting, tool: str, kinds: tuple[str, ...]) -> None:
    if who.run_id is None or not who.ba or who.run_kind not in kinds:
        runs = " or ".join(f"a {kind} run" for kind in kinds)
        raise SrsError(403, f"only the agent of {runs} of @{BA} in project {who.access.name} calls srs__{tool}")


async def _author(conn: AsyncConnection, who: Acting, doc: store.DocumentRow) -> tuple[Principal, ProjectAccess]:
    """(who a version @ba proposes of ``doc`` is written as, their access): the author of the version whose settle
    chat the run is in (``settle.proposer``), else the person the run acts for."""
    author_id = await proposer(conn, who.run_id, doc.id)
    if author_id is None or author_id == who.person.user_id:
        return who.person, who.access
    author = await _principal(conn, author_id, None)
    return author, await _access(conn, author, who.access.name)


async def _propose(conn: AsyncConnection, who: Acting, args: dict) -> str:
    _ba_run(who, "propose", ("chat",))
    try:
        changes = edit_changes(CHANGES.validate_python(args["changes"]))
    except ValidationError as exc:
        raise SrsError(422, f"the changes are not taken: {exc.errors()[0]['msg']}") from None
    slug = args["document"]
    doc = await _document(conn, who, slug, lock=True)
    author, access = await _author(conn, who, doc)
    reviews.writer(access, f"proposing a version as {author.login}")
    latest = await store.snapshot(conn, await store.version(conn, doc))
    number = args["base_version"]
    base = (
        latest
        if number == latest.version.number
        else await store.snapshot(conn, await store.version(conn, doc, number))
    )
    content = edited(latest, base, changes)
    await give_screen_codes(conn, access.project_id, content)
    settings = await store.settings(conn, access)
    writer = Writer(access.project_id, doc.id, author.user_id, False, who.run_id)
    made = await write_version(conn, writer, content, latest, settings.template, args["summary"])
    doc = await store.document(conn, access, slug, who.sink)
    sent = await reviews.submit(conn, access, doc, made, settings)
    review = await review_entered(conn, access, doc, sent)
    new = await store.snapshot(conn, sent)
    since = latest.version.number
    login = author.login
    head = f"Version {made} of {slug} is in review, drafted by run #{who.run_id} and written as {login}"
    if settings.self_approval:
        approver = f"{head}. Self-approval is on in project {access.name}, so {login} may approve it as any writer may."
    else:
        approver = f"{head}, who therefore does not approve it: another member of the project does."
    said = [approver, f"Compare it with version {since}: {compare_path(access.name, slug, since, made)}"]
    if review is not None:
        said.append(
            f"@{BA} reviews it in run #{review}; its comments are advice and block no approval, and an open finding "
            "of level decide it records holds approval until members settle it."
        )
    return "\n".join([*said, "", _changes_text(slug, since, made, latest.doc, new.doc)])


async def propose(params: dict, context: CoreContext) -> dict:
    """The tool ``srs__propose``: a document's next version from @ba's changes, in review at once."""
    return await _answer(params, context, _propose)


async def _comment(conn: AsyncConnection, who: Acting, args: dict) -> str:
    _ba_run(who, "comment", ("chat", "task"))
    doc = await _document(conn, who, args["document"], lock=True)
    asked = reviews.CommentRequest(args["body"], args.get("version"), args.get("item"), args.get("parent_id"))
    row = await reviews.comment(conn, who.access, who.person, doc, asked, run_id=who.run_id)
    where = f"item {row.item_code or row.item}" if row.item else f"version {row.version}" if row.version else "it"
    return f"Comment {row.id} on {where} of {doc.slug}, as {who.person.login}, drafted by run #{who.run_id}."


async def comment(params: dict, context: CoreContext) -> dict:
    """The tool ``srs__comment``: a comment of the person the run works for, drafted by the run."""
    return await _answer(params, context, _comment)


# Findings


async def _settler(conn: AsyncConnection, who: Acting, login: str | None) -> int | None:
    """The id of the member ``login`` names as having settled a finding, who must hold a grant on the project."""
    if login is None:
        return None
    u = tables.users
    user_id = (await conn.execute(select(u.c.id).where(u.c.login == login))).scalar_one_or_none()
    if user_id is None:
        raise SrsError(422, f"no member {login} on the hub settled the finding")
    access = await _access(conn, await _principal(conn, user_id, None), who.access.name)
    if access.role is None:
        raise SrsError(422, f"{login} holds no grant on project {who.access.name}")
    return user_id


def _finding_text(row: findings.FindingRow, slug: str, done: str) -> str:
    about = f"item {row.item_code or row.item}" if row.item else "the whole document"
    lines = [
        f"Finding {row.id} of {slug} {done}: {row.level}, {row.kind}, on version {row.version}, about {about}.",
        f"Summary: {row.summary}",
        "Sources: " + "; ".join(str(findings.Source(**source)) for source in row.sources),
    ]
    if row.status != findings.OPEN:
        by = f" by {row.settled_by}" if row.settled_by else " from the graph" if row.status == findings.RESOLVED else ""
        lines.append(f"{row.status.capitalize()}{by}: {row.resolution}")
        if row.resolution_sources:
            lines.append("Settled by: " + "; ".join(str(findings.Source(**s)) for s in row.resolution_sources))
    elif row.level == findings.DECIDE:
        lines.append("While it is open, no version of the document is approved.")
    if row.chat_link:
        lines.append(f"Chat: {row.chat_link}")
    return "\n".join(lines)


async def _finding(conn: AsyncConnection, who: Acting, args: dict, graph_refs) -> str:
    _ba_run(who, "finding", ("chat", "task"))
    action = args["action"]
    findings.checked_args(action, args)
    settling = action == "resolve"
    sources = findings.sources_of(args.get("sources"), settling=settling)
    link = findings.chat_link(args.get("chat_link"))
    problems, authors = await findings.looked_up(who.access.name, sources, graph_refs)
    if problems:
        raise findings.refused(problems)
    doc = await _document(conn, who, args["document"], lock=True)
    if action == "record":
        new = findings.NewFinding(
            args["kind"], args["level"], args["summary"], sources, args.get("version"), args.get("item")
        )
        row = await findings.record(conn, who.access, doc, new, who.run_id, authors)
        return _finding_text(row, doc.slug, "is recorded")
    if action == "reopen":
        return _finding_text(await findings.reopen(conn, doc, args["finding"]), doc.slug, "is open again")
    status = findings.RESOLVED if settling else findings.WAIVED
    settler = await _settler(conn, who, args.get("settled_by"))
    how = findings.Settling(status, args["resolution"], settler, sources, link)
    return _finding_text(await findings.settle(conn, who.access, doc, args["finding"], how), doc.slug, f"is {status}")


async def finding(params: dict, context: CoreContext) -> dict:
    """The tool ``srs__finding``: record a finding with its sources, or resolve, waive or reopen one."""
    return await _answer(params, context, lambda conn, who, args: _finding(conn, who, args, context.graph_refs))


__all__ = ["Acting", "acting", "comment", "compare_path", "diff", "finding", "propose", "read", "search"]

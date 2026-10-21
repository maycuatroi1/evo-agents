"""The conflicts @ba finds in a document, and the gate a version's approval goes through (criteria a4 and a5 of plan
srs-conflicts-and-tasks).

A finding (srs_findings) is one conflict of a document: the version it was found on, the item it is about (none for
the whole document), its kind (duplicate, contradiction, kg_conflict, overlap), its level (info, advisory, decide), the
sources that show it, a summary, and its status: open, resolved or waived. Without a source there is no conflict: a
finding names at least one, and the hub looks each up before it writes anything: a code some item of the project holds
(a use case's, or any coded item's, of any document), the id of a node of the project's knowledge graph on the hub,
or a path a File or Document node of that graph carries (``repo:path``, or the path alone), the graph read as the
agent of the run reads it (``plugin_calls.CoreContext.graph_refs``). A finding carries the label of its document, and
keeps the author emails of the newest commits of the files its sources name, as that lookup found them
(``commit_authors``): whom its settle chat invites (``settle``). No route returns them.

The agent of a run of @ba writes them with the tool srs__finding (``agent``): it records one, resolves or waives an
open one, and reopens one settled at most REOPEN_HOURS ago. A resolution says how it was settled and names either
the member who settled it (with the link of the chat message) or, settled from the graph, the sources that settled it,
which may also be a later version of the document; a waiver says why. An admin of the project waives an open finding
with a reason (``server``, ``evo-agents hub srs waive``), and every member with a grant reads them
(``evo-agents hub srs findings``). An archived document takes none of these writes (``store.writable``).

``approval_hold`` is the gate of an approval (``reviews.review``): a version is not approved while its document has an
open finding of level decide, whatever version it was found on, or while @ba's review of the version's current round
(srs_versions.review_run_id, ``auto_review``) is queued or running. A review that failed, was cancelled or was lost
holds nothing, nor does a round no review was handed for; a request for changes is never held. The refusal names each
open finding with its chat link: the first message of the settle chat it is settled in while it is open (``settle``
sets it when it opens the chat), the message that settled it once one did.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.srs import BA, store
from evo_agents.core_plugins.srs.doc import SrsError
from evo_agents.core_plugins.srs.store import DocumentRow, VersionRow
from evo_agents.core_plugins.srs.template import code_key
from evo_agents.hub import tables
from evo_agents.hub.runs import ACTIVE_STATES
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.tables_srs import (
    FINDING_KINDS,
    FINDING_LEVELS,
    MAX_CHAT_LINK_CHARS,
    MAX_COMMIT_AUTHORS,
    MAX_SOURCE_CHARS,
    MAX_SOURCES,
    SOURCE_KINDS,
)

OPEN, RESOLVED, WAIVED = "open", "resolved", "waived"
DECIDE = "decide"  # the level whose open findings hold approval
REOPEN_HOURS = 24  # how long after it was settled a finding may be reopened
SHOWS = ("node", "code", "path")  # the kinds of source that show a conflict; a version of the document only settles one
LINK = re.compile(r"^(https?://|/)\S+$")
ACTIONS = {  # what srs__finding takes for each action: (required, also allowed)
    "record": (("kind", "level", "summary", "sources"), ("version", "item")),
    "resolve": (("finding", "resolution"), ("settled_by", "chat_link", "sources")),
    "waive": (("finding", "resolution"), ("settled_by", "chat_link")),
    "reopen": (("finding",), ()),
}


@dataclass(frozen=True)
class FindingRow:
    """A finding with the number of the version it was found on, its item's key and code, and its settler's login."""

    id: int
    version: int
    item: str | None
    item_code: str | None
    kind: str
    level: str
    sources: list[dict]
    summary: str
    status: str
    resolution: str | None
    resolution_sources: list[dict]
    settled_by: str | None
    settled_at: datetime | None
    chat_link: str | None
    run_id: int | None
    created_at: datetime


@dataclass(frozen=True)
class Source:
    kind: str
    ref: str
    note: str | None = None

    def as_json(self) -> dict:
        return {"kind": self.kind, "ref": self.ref, **({"note": self.note} if self.note else {})}

    def __str__(self) -> str:
        return f"{self.kind} {self.ref}"


# Reading


def _query(document_id: int):
    s, u = tables.srs, tables.users
    f, v, i = s.findings, s.versions, s.items
    return (
        select(
            f.c.id,
            v.c.number.label("version"),
            i.c.key.label("item"),
            i.c.code.label("item_code"),
            f.c.kind,
            f.c.level,
            f.c.sources,
            f.c.summary,
            f.c.status,
            f.c.resolution,
            f.c.resolution_sources,
            u.c.login.label("settled_by"),
            f.c.settled_at,
            f.c.chat_link,
            f.c.run_id,
            f.c.created_at,
        )
        .select_from(
            f.join(v, v.c.id == f.c.version_id)
            .outerjoin(i, i.c.id == f.c.item_id)
            .outerjoin(u, u.c.id == f.c.settled_by)
        )
        .where(f.c.document_id == document_id)
    )


async def findings(conn: AsyncConnection, doc: DocumentRow, status: str | None = None) -> list[FindingRow]:
    """The findings of ``doc``, oldest first; those in ``status`` only when given."""
    f = tables.srs.findings
    query = _query(doc.id)
    if status is not None:
        query = query.where(f.c.status == status)
    return [FindingRow(**row._mapping) for row in await conn.execute(query.order_by(f.c.id))]


async def code_documents(
    conn: AsyncConnection, access: ProjectAccess, sink: str | None, rows: list[FindingRow]
) -> dict[str, str]:
    """The slug of the document whose item holds each code the sources of ``rows`` name, by the code's key, for the
    documents the member sees through ``sink`` as ``store.document`` reads them; a code of another document is left
    out, so its slug is not told."""
    named = [source for row in rows for source in (*row.sources, *row.resolution_sources)]
    keys = sorted({code_key(source["ref"]) for source in named if source.get("kind") == "code"})
    through = store.sink_of(access, sink)
    if not keys or through is None:
        return {}
    s = tables.srs
    c, i, d = s.codes, s.items, s.documents
    query = (
        select(c.c.code_key, d.c.slug, d.c.label)
        .join_from(c, i, i.c.id == c.c.item_id)
        .join(d, d.c.id == i.c.document_id)
        .where(c.c.project_id == access.project_id, c.c.code_key.in_(keys))
    )
    return {row.code_key: row.slug for row in await conn.execute(query) if access.visible(row.label, through)}


async def finding(conn: AsyncConnection, doc: DocumentRow, finding_id: int) -> FindingRow:
    """Finding ``finding_id`` of ``doc``; 404 when the document has none such."""
    row = (await conn.execute(_query(doc.id).where(tables.srs.findings.c.id == finding_id))).first()
    if row is None:
        raise SrsError(404, f"document {doc.slug} has no finding {finding_id}")
    return FindingRow(**row._mapping)


# What srs__finding takes


def checked_args(action: str, args: dict) -> None:
    """422 when ``args`` lack what ``action`` needs or name what it does not take."""
    required, allowed = ACTIONS[action]
    missing = [name for name in required if args.get(name) in (None, "", [])]
    if missing:
        raise SrsError(422, f"srs__finding {action} needs {', '.join(missing)}; nothing was written")
    extra = sorted(set(args) - {"action", "document", *required, *allowed})
    if extra:
        raise SrsError(422, f"srs__finding {action} takes no {', '.join(extra)}; nothing was written")


def sources_of(raw: list[dict] | None, *, settling: bool = False) -> list[Source]:
    """The sources ``raw`` names, each once in the order given; 422 for one of a kind this use does not take: a
    version of the document settles a finding and never shows one."""
    kinds = SOURCE_KINDS if settling else SHOWS
    found: dict[tuple[str, str], Source] = {}
    for entry in raw or []:
        kind, ref = entry.get("kind"), str(entry.get("ref") or "").strip()
        if kind not in kinds:
            what = "settles" if settling else "shows"
            raise SrsError(422, f"a source that {what} a finding is one of {', '.join(kinds)}, not {kind}")
        if not ref or len(ref) > MAX_SOURCE_CHARS:
            raise SrsError(422, f"a source's ref is 1 to {MAX_SOURCE_CHARS} characters")
        found.setdefault((kind, ref), Source(kind, ref, (entry.get("note") or "").strip() or None))
    if len(found) > MAX_SOURCES:
        raise SrsError(422, f"a finding names at most {MAX_SOURCES} sources")
    return list(found.values())


def chat_link(link: str | None) -> str | None:
    """``link`` when it is a link a message can have (a path of the hub's web or an http(s) URL); 422 otherwise."""
    if link is None:
        return None
    if len(link) > MAX_CHAT_LINK_CHARS or not LINK.match(link):
        raise SrsError(422, f"chat_link is a path of the hub's web or an http(s) URL of {MAX_CHAT_LINK_CHARS} chars")
    return link


async def looked_up(project: str, sources: list[Source], graph_refs) -> tuple[list[str], tuple[str, ...]]:
    """(why each node and path of ``sources`` is not in the project's knowledge graph on the hub, the author emails of
    the newest commits of the files they name); ``graph_refs`` is ``CoreContext.graph_refs``, awaited before the
    document is locked, since it may fetch the graph."""
    ids = [source.ref for source in sources if source.kind == "node"]
    paths = [source.ref for source in sources if source.kind == "path"]
    if not ids and not paths:
        return [], ()
    if graph_refs is None:
        return [
            f"node ids and paths are looked up only by a tool on the hub's /mcp; {project}'s graph is not read here"
        ], ()
    refs = await graph_refs(ids, paths)
    if refs.why:
        return [f"node ids and paths are looked up in the knowledge graph of project {project}, and {refs.why}"], ()
    where = f"the knowledge graph of project {project} as the run reads it"
    problems = [f"no node of {where} has id {ref}" for ref in refs.ids] + [
        f"no File or Document node of {where} carries path {ref}" for ref in refs.paths
    ]
    return problems, tuple(refs.authors)[:MAX_COMMIT_AUTHORS]


async def graph_problems(project: str, sources: list[Source], graph_refs) -> list[str]:
    """Why each node and path of ``sources`` is not in the project's knowledge graph on the hub (``looked_up``)."""
    return (await looked_up(project, sources, graph_refs))[0]


async def held_problems(
    conn: AsyncConnection, access: ProjectAccess, doc: DocumentRow, found_on: int, sources: list[Source]
) -> list[str]:
    """Why each code and version of ``sources`` is not held: a code no item of the project holds, a version of ``doc``
    that is not after ``found_on``, the version a finding was found on."""
    problems = []
    codes = {code_key(source.ref): source.ref for source in sources if source.kind == "code"}
    if codes:
        c = tables.srs.codes
        query = select(c.c.code_key).where(c.c.project_id == access.project_id, c.c.code_key.in_(list(codes)))
        held = set((await conn.execute(query)).scalars())
        problems += [
            f"no item of project {access.name} holds code {code}" for key, code in codes.items() if key not in held
        ]
    for source in sources:
        if source.kind == "version" and not (
            source.ref.isdigit() and found_on < int(source.ref) <= (doc.latest_version or 0)
        ):
            problems.append(f"{doc.slug} has no version {source.ref} after version {found_on}, which the finding is on")
    return problems


def refused(problems: list[str]) -> SrsError:
    return SrsError(422, f"a source cannot be looked up, so nothing was written: {problems[0]}", detail=problems)


# Writing


@dataclass(frozen=True)
class NewFinding:
    kind: str
    level: str
    summary: str
    sources: list[Source]
    version: int | None = None  # the latest when None
    item: str | None = None  # a code, an alias or a key; the whole document when None


async def record(
    conn: AsyncConnection,
    access: ProjectAccess,
    doc: DocumentRow,
    new: NewFinding,
    run_id: int | None,
    commit_authors: tuple[str, ...] = (),
) -> FindingRow:
    """Write ``new`` on ``doc``, whose row lock the caller holds, with the label of the document and the authors of
    the newest commits of its sources' files; the codes and versions of its sources are checked here, its nodes and
    paths by the caller first (``looked_up``, which gives ``commit_authors``)."""
    if new.kind not in FINDING_KINDS or new.level not in FINDING_LEVELS:
        raise SrsError(
            422, f"a finding's kind is one of {', '.join(FINDING_KINDS)}, its level one of {', '.join(FINDING_LEVELS)}"
        )
    if not new.sources:
        raise SrsError(422, "a finding names at least one source that shows it: without one it is no conflict")
    row = await store.version(conn, doc, new.version)
    problems = await held_problems(conn, access, doc, row.number, new.sources)
    if problems:
        raise refused(problems)
    item_id = await store.item_id(conn, access, doc, new.item) if new.item else None
    f = tables.srs.findings
    values = {
        "document_id": doc.id,
        "version_id": row.id,
        "item_id": item_id,
        "kind": new.kind,
        "level": new.level,
        "sources": [source.as_json() for source in new.sources],
        "summary": new.summary,
        "run_id": run_id,
        "label": doc.label,
        "commit_authors": list(dict.fromkeys(commit_authors))[:MAX_COMMIT_AUTHORS],
    }
    return await finding(conn, doc, (await conn.execute(insert(f).values(values).returning(f.c.id))).scalar_one())


@dataclass(frozen=True)
class Settling:
    """How an open finding is settled: resolved or waived, with what and by whom."""

    status: str
    resolution: str
    settled_by: int | None = None  # a member; None when settled from the graph, or waived by @ba
    sources: list[Source] = field(default_factory=list)
    chat_link: str | None = None


async def settle(
    conn: AsyncConnection, access: ProjectAccess, doc: DocumentRow, finding_id: int, settling: Settling
) -> FindingRow:
    """Resolve or waive open finding ``finding_id`` of ``doc``, whose row lock the caller holds; the finding after. A
    resolution names the member who settled it, or the sources that settled it; the nodes and paths among them the
    caller looked up (``graph_problems``)."""
    held = await finding(conn, doc, finding_id)
    if held.status != OPEN:
        raise SrsError(409, f"finding {finding_id} of {doc.slug} is {held.status}: only an open finding is settled")
    if settling.status == RESOLVED and settling.settled_by is None and not settling.sources:
        raise SrsError(
            422,
            "a finding no member settled is settled from the graph: name the sources that settle it (a node, a code, "
            "a path, or a later version of the document), or the member who settled it",
        )
    problems = await held_problems(conn, access, doc, held.version, settling.sources)
    if problems:
        raise refused(problems)
    values = {
        "status": settling.status,
        "resolution": settling.resolution,
        "resolution_sources": [source.as_json() for source in settling.sources],
        "settled_by": settling.settled_by,
        "settled_at": func.now(),
    }
    if settling.chat_link is not None:
        values["chat_link"] = settling.chat_link
    f = tables.srs.findings
    await conn.execute(update(f).where(f.c.id == finding_id).values(values))
    return await finding(conn, doc, finding_id)


async def reopen(conn: AsyncConnection, doc: DocumentRow, finding_id: int) -> FindingRow:
    """Open settled finding ``finding_id`` of ``doc`` again, at most REOPEN_HOURS after it was settled; it keeps its
    chat link. The caller holds the document's row lock."""
    held = await finding(conn, doc, finding_id)
    if held.status == OPEN:
        raise SrsError(409, f"finding {finding_id} of {doc.slug} is open already")
    f = tables.srs.findings
    recent = and_(f.c.id == finding_id, f.c.settled_at > func.now() - timedelta(hours=REOPEN_HOURS))
    values = {"status": OPEN, "resolution": None, "resolution_sources": [], "settled_by": None, "settled_at": None}
    done = await conn.execute(update(f).where(recent).values(values))
    if not done.rowcount:
        raise SrsError(
            409,
            f"finding {finding_id} of {doc.slug} was {held.status} more than {REOPEN_HOURS} hours ago and is not "
            "reopened: record a new finding with the sources that show the conflict",
        )
    return await finding(conn, doc, finding_id)


# The gate of an approval


def _named(row: FindingRow) -> str:
    about = f", {row.item_code or row.item}" if row.item else ""
    chat = f", chat {row.chat_link}" if row.chat_link else ""
    return f"#{row.id} ({row.kind}{about}{chat})"


async def _review_run(conn: AsyncConnection, held: VersionRow):
    v, r = tables.srs.versions, tables.runs
    query = select(r.c.id, r.c.state).join_from(v, r, r.c.id == v.c.review_run_id).where(v.c.id == held.id)
    return (await conn.execute(query)).first()


async def approval_hold(conn: AsyncConnection, access: ProjectAccess, doc: DocumentRow, held: VersionRow) -> None:
    """409 when version ``held`` of ``doc`` may not be approved now (see the module), naming each open finding of
    level decide with its chat link, or the review run that is still to end."""
    deciding = [row for row in await findings(conn, doc, OPEN) if row.level == DECIDE]
    if deciding:
        waive = f"evo-agents hub srs waive {doc.slug} FINDING --project {access.name} --reason TEXT"
        raise SrsError(
            409,
            f"version {held.number} of {doc.slug} is not approved while {len(deciding)} finding(s) of level decide "
            f"are open: {', '.join(_named(row) for row in deciding)}. Settle each in its chat, or an admin of the "
            f"project waives one with `{waive}`",
            detail=[
                {"finding": row.id, "kind": row.kind, "summary": row.summary, "chat_link": row.chat_link}
                for row in deciding
            ],
        )
    run = await _review_run(conn, held)
    if run is not None and run.state in ACTIVE_STATES:
        raise SrsError(
            409,
            f"version {held.number} of {doc.slug} is not approved while @{BA}'s review of its round {held.round} "
            f"(run #{run.id}) is {run.state}: approve it once the run ends; a run that failed or was cancelled holds "
            "nothing",
        )


__all__ = [
    "ACTIONS",
    "DECIDE",
    "OPEN",
    "REOPEN_HOURS",
    "RESOLVED",
    "WAIVED",
    "FindingRow",
    "NewFinding",
    "Settling",
    "Source",
    "approval_hold",
    "chat_link",
    "checked_args",
    "code_documents",
    "finding",
    "findings",
    "graph_problems",
    "held_problems",
    "looked_up",
    "record",
    "refused",
    "reopen",
    "settle",
    "sources_of",
]

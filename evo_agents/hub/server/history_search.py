"""Reading a project's whole history: its sessions and runs with the numbers of their facets, the words of its
transcripts, run logs and group chats, and the lines of a transcript around one. What the Curator's Reviewer reads in
two tiers, the numbers first and the words where they point, through the hub's MCP tools
(``evo_agents.hub.server.history_mcp``), and what a member reads with ``evo-agents hub curator history``.

GET /v1/projects/{p}/curator/history/sessions lists the sessions and runs of the project's facets
(``evo_agents.hub.facet_job``), the latest first, filtered by time (``since``, ``until``: UTC days), member, runtime,
kind, outcome (what the cheap model wrote of a session or a run, ``history.OUTCOMES``, or a run's state) and cluster
(the key of a cluster of ``evo_agents.hub.server.history``, the groups the model joined to it counted in it), each with
the numbers of its facet (``facets.summary``) and what the model wrote of it. It examines at most SCAN_ROWS facets.

GET .../curator/history/search finds the words of a query (``websearch_to_tsquery`` of the configuration ``simple``:
words, "a phrase", -excluded) in the rows of history_lines (``evo_agents.hub.history_index``) and in the messages of the
group chats the caller is in, best first (``ts_rank``), each with a few words around the match (``ts_headline``) and the
evidence that names it: ``transcript:ID[:FILE]:LINE`` or ``run:ID:SEQ``. Each source answers at most SCAN_HITS hits
before they are merged.

GET /v1/projects/{p}/transcripts/{session_id}/lines reads the lines of one transcript file around a line, from the
blob store, each as its entries: what the person wrote, what the agent said, each tool call and its result.

Each needs a grant on the project and reads what the caller may read, as the hub's other routes read it: a
transcript and a facet of a session by their label through the sink X-Evo-Sink names (else the project's hub sink); a
run as ``runs.service.views.readable_run`` reads it (a local run its member's alone, a run on no plan through the
project's default label, a run of a plan the caller may read); a chat's message when the caller is in the chat now,
clears its label now (``chat_access``) and cleared the message's (``LabelReader``), and the message names the project
with a part of its label visible through that sink.

The agent of a run (the caller of the hub's MCP tools with a ``RunScope``) reads less than its owner, since what it
reads lands in its run's events and in what it writes (a finding, a chat's reply), which readers who are not the
owner read: no replay run, and a group chat or a local run only in a review run, whose Reviewer reads the chats and
the local sessions of the charter's owner (``views.agent_reads``, ``views.agent_reads_chats``). Any other agent
searches no group chat (AGENT_SOURCES; asking for one is 403). The Reviewer's chat hits go through the sink as the
owner's do; what it reads of a chat can reach its run's trace and its findings, a debt kept so it reads the whole
history.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, PrivateAttr
from sqlalchemy import and_, false, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import excerpts, facet_job, history, history_index, review, tables
from evo_agents.hub import transcripts as local
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server import rankers
from evo_agents.hub.server.admin import LOGIN_NAME, ProjectName
from evo_agents.hub.server.chat_access import chat_access, label_reader
from evo_agents.hub.server.digests import SessionId
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.history import cluster_rows
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.protection import need_reader
from evo_agents.hub.server.runs.service.views import agent_reads, agent_reads_chats, sees_unplanned, visible_plans
from evo_agents.hub.server.security import CurrentUser, Principal

SOURCES = ("transcript", "run", "chat")
AGENT_SOURCES = ("transcript", "run")  # what the agent of a run other than the Reviewer searches (see the module)
KINDS = ("session", "run")
SCAN_ROWS = 5000  # facets one list examines
SCAN_HITS = 200  # hits each source answers before they are merged
MAX_LIMIT = 100
MAX_SEARCH = 50
MAX_AROUND = 100  # lines before and after the one asked for
HEADLINE = "MaxWords=35, MinWords=12, MaxFragments=2, FragmentDelimiter= ... , StartSel=«, StopSel=»"
QUERY_CHARS = 500
NO_CHAT = (
    "the agent of this run searches no group chat (the Reviewer of a review run alone does): what it reads lands in its "
    "run's events and in what it writes, which readers of the project who are not in the chat read; search the "
    "transcripts and the runs"
)

router = APIRouter(prefix="/v1/projects", tags=["history"], responses={401: {"model": ErrorBody}})
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 422)}

Day = Annotated[date | None, Query(description="a UTC day, YYYY-MM-DD")]
FileKey = Annotated[str, Query(pattern=review.TRANSCRIPT_FILE, max_length=local.MAX_FILE_KEY)]


# Models


class HistorySessionItem(BaseModel):
    kind: Literal[KINDS]
    session_id: str | None = Field(description="a session's id; for a run, the session its agent ran in, if known")
    run_id: int | None
    run_kind: str | None = Field(description="the kind of a run")
    state: str | None = Field(description="the state of a run")
    plan_id: str | None
    login: str | None = Field(description="the member whose session or run it is")
    runtime: str | None
    at: datetime | None = Field(description="when it ended, or its last message")
    source: str | None = Field(description="what its facet was made from: transcript, digest, run_log or events")
    numbers: dict = Field(description="the numbers of its facet: messages, tool errors, corrections, tokens, cost")
    summary: str | None = Field(description="what the cheap model wrote of it")
    outcome: str | None = Field(description="the outcome the model wrote")
    failure_modes: list[str]
    cluster: str | None = Field(description="the key of the cluster it counts in")
    evidence: str = Field(description="how a finding cites it")
    transcript: bool = Field(description="the hub holds its transcript: transcript_lines reads it")


class HistorySessions(BaseModel):
    project: str
    items: list[HistorySessionItem] = Field(description="the latest first")
    total: int = Field(description="those that match, of the facets examined")
    limit: int
    offset: int


class HistoryHit(BaseModel):
    source: Literal[SOURCES]
    at: datetime | None
    role: str = Field(
        description="who: user, agent, call TOOL, TOOL result or failed, an error; for a chat, the author"
    )
    text: str = Field(description="the words around the match, the matched ones between « and »")
    rank: float
    evidence: str | None = Field(description="how a finding cites it; null for a chat message")
    session_id: str | None
    file: str | None
    line: int | None = Field(description="the line of the transcript, or the seq of the run's event")
    run_id: int | None
    run_kind: str | None
    chat_id: int | None
    chat: str | None = Field(description="the chat's title")
    login: str | None = Field(description="the member whose session or run it is, or the chat message's author")
    runtime: str | None
    _gate: tuple | None = PrivateAttr(default=None)  # what tells whether a sink may read it (``_cleared``); not served


class HistorySearch(BaseModel):
    project: str
    query: str
    sources: list[str]
    hits: list[HistoryHit] = Field(description="best first")
    total: int = Field(description="hits found, at most SCAN_HITS of each source")
    limit: int
    offset: int
    reranked: bool = Field(
        default=False,
        description="a ranker of a plugin the project enabled put the hits of this page in a new order; each hit's "
        "rank is still the ts_rank of its words",
    )


class TranscriptLine(BaseModel):
    line: int
    role: str
    text: str


class TranscriptLines(BaseModel):
    project: str
    session_id: str
    file: str
    runtime: str
    login: str
    lines: int = Field(description="lines of the file the hub holds")
    asked: int = Field(description="the line asked for")
    first: int = Field(description="the first line read")
    last: int = Field(description="the last line read")
    entries: list[TranscriptLine] = Field(description="each entry of the lines read, in order")
    evidence: str = Field(description="how a finding cites the line asked for")


# Who reads


async def _access(conn: AsyncConnection, user: Principal, project: str) -> ProjectAccess:
    access = await project_access(conn, user, project)
    need_reader(access)
    return access


class RunReader:
    """Whether the caller reads a run: as ``readable_run`` decides it, for many runs at once."""

    def __init__(self, user: Principal, plans: list[str], unplanned: bool):
        self.user, self.plans, self.unplanned = user, frozenset(plans), unplanned

    def reads(self, kind: str | None, plan_id: str | None, dispatched_by: int | None) -> bool:
        if not agent_reads(self.user, kind):
            return False
        if kind == "local":
            return dispatched_by == self.user.user_id
        if plan_id is None:
            return self.unplanned
        return plan_id in self.plans

    def clause(self, r):
        """The same rule in SQL, on ``r``, the runs table."""
        mine = agent_reads(self.user, "local")
        local_run = and_(r.c.kind == "local", r.c.dispatched_by == self.user.user_id, true() if mine else false())
        unplanned = and_(r.c.kind != "local", r.c.plan_id.is_(None), true() if self.unplanned else false())
        planned = and_(r.c.kind != "local", r.c.plan_id.in_(sorted(self.plans) or [""]))
        kept = [] if agent_reads(self.user, "replay") else [r.c.kind.not_in(("replay", "probe"))]
        return and_(or_(local_run, unplanned, planned), *kept)


async def run_reader(conn: AsyncConnection, user: Principal, access: ProjectAccess, sink: str | None) -> RunReader:
    return RunReader(user, await visible_plans(conn, access, sink), sees_unplanned(access, sink))


def _day(value: date | None) -> datetime | None:
    return datetime.combine(value, time.min, tzinfo=UTC) if value is not None else None


def evidence_of(kind: str, session_id: str | None = None, run_id: int | None = None, seq: int = 1) -> str:
    return f"run:{run_id}:{seq}" if kind == "run" else f"session:{session_id}"


def line_evidence(session_id: str, file: str, line: int) -> str:
    return review.evidence_text({"kind": "transcript", "session_id": session_id, "file": file, "line": line})


# The sessions and runs


def _facet_rows(project_id: int, kind, since, until, login, runtime):
    f, r, u, t = tables.session_facets, tables.runs, tables.users, tables.session_transcripts
    held = (
        select(t.c.id)
        .where(
            t.c.project_id == f.c.project_id,
            t.c.session_id == f.c.session_id,
            t.c.deleted_at.is_(None),
            t.c.parts > 0,
        )
        .exists()
    )
    query = (
        select(
            f.c.kind,
            f.c.session_id,
            f.c.run_id,
            f.c.source,
            f.c.label,
            f.c.runtime,
            f.c.at,
            f.c.summary,
            f.c.llm,
            r.c.kind.label("run_kind"),
            r.c.state,
            r.c.plan_id,
            r.c.dispatched_by,
            u.c.login,
            held.label("held"),
        )
        .select_from(f.outerjoin(r, r.c.id == f.c.run_id).outerjoin(u, u.c.id == f.c.user_id))
        .where(f.c.project_id == project_id, f.c.body.is_not(None))
    )
    if kind is not None:
        query = query.where(f.c.kind == kind)
    if since is not None:
        query = query.where(f.c.at >= since)
    if until is not None:
        query = query.where(f.c.at < until)
    if login is not None:
        query = query.where(func.lower(u.c.login) == login.lower())
    if runtime is not None:
        query = query.where(f.c.runtime == runtime)
    return query.order_by(f.c.at.desc().nulls_last(), f.c.id.desc()).limit(SCAN_ROWS)


def _item(row, into: dict) -> HistorySessionItem:
    llm = row.llm if isinstance(row.llm, dict) else None
    cluster = history.root_of(history.cluster_key(llm), into) if llm else None
    return HistorySessionItem(
        kind=row.kind,
        session_id=row.session_id,
        run_id=row.run_id,
        run_kind=row.run_kind,
        state=row.state,
        plan_id=row.plan_id,
        login=row.login,
        runtime=row.runtime,
        at=row.at,
        source=row.source,
        numbers=row.summary or {},
        summary=llm.get("summary") if llm else None,
        outcome=llm.get("outcome") if llm else None,
        failure_modes=list(llm.get("failure_modes") or []) if llm else [],
        cluster=cluster,
        evidence=evidence_of(row.kind, row.session_id, row.run_id),
        transcript=bool(row.held) and row.kind == "session",
    )


async def list_history(
    conn: AsyncConnection,
    user: Principal,
    access: ProjectAccess,
    sink: str | None,
    *,
    kind: str | None = None,
    since: date | None = None,
    until: date | None = None,
    login: str | None = None,
    runtime: str | None = None,
    outcome: str | None = None,
    cluster: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> HistorySessions:
    """The sessions and runs of the project ``user`` may read that match (see the module)."""
    through = plan_routes._sink(access, sink)
    runs_read = await run_reader(conn, user, access, sink)
    into = {row.key: row.merged_into for row in await cluster_rows(conn, access.project_id)}
    query = _facet_rows(access.project_id, kind, _day(since), _day(until), login, runtime)
    found = []
    for row in await conn.execute(query):
        if row.kind == "session":
            if through is None or not access.visible(row.label, through):
                continue
        elif not runs_read.reads(row.run_kind, row.plan_id, row.dispatched_by):
            continue
        item = _item(row, into)
        if outcome is not None and outcome not in (item.outcome, item.state if item.kind == "run" else None):
            continue
        if cluster is not None and item.cluster != cluster:
            continue
        found.append(item)
    return HistorySessions(
        project=access.name, items=found[offset : offset + limit], total=len(found), limit=limit, offset=offset
    )


@router.get("/{project}/curator/history/sessions", response_model=HistorySessions, responses=REFUSALS)
async def history_sessions(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    kind: Annotated[Literal[KINDS] | None, Query()] = None,
    since: Day = None,
    until: Day = None,
    login: Annotated[str | None, Query(pattern=LOGIN_NAME, description="of this member")] = None,
    runtime: Annotated[Literal[local.RUNTIMES] | None, Query()] = None,
    outcome: Annotated[str | None, Query(pattern=r"^[a-z_]{1,20}$", description="an outcome, or a run's state")] = None,
    cluster: Annotated[str | None, Query(pattern=history.KEY.pattern, description="a cluster's key")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 20,
    offset: Annotated[int, Query(ge=0, le=SCAN_ROWS)] = 0,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> HistorySessions:
    """The sessions and runs of the project's whole history, the latest first, with the numbers of their facets."""
    async with request.app.state.engine.begin() as conn:
        access = await _access(conn, user, project)
        return await list_history(
            conn,
            user,
            access,
            sink,
            kind=kind,
            since=since,
            until=until,
            login=login,
            runtime=runtime,
            outcome=outcome,
            cluster=cluster,
            limit=limit,
            offset=offset,
        )


# Search


def _headline(text_column, query):
    return func.ts_headline(history_index.CONFIG, text_column, query, HEADLINE)


def _between(column, since, until):
    found = []
    if since is not None:
        found.append(column >= since)
    if until is not None:
        found.append(column < until)
    return found


def _gated(hit: HistoryHit, gate: tuple) -> HistoryHit:
    """``hit`` with what tells whether a sink may read it: ("label", a label as stored), ("chat", the label of a chat
    message) or ("run", kind, plan id, dispatcher)."""
    hit._gate = gate
    return hit


async def _transcript_hits(conn, access, through, query, since, until, login, runtime) -> list[HistoryHit]:
    lines, t, u = tables.history_lines, tables.session_transcripts, tables.users
    rank = func.ts_rank(lines.c.search, query)
    where = [
        lines.c.project_id == access.project_id,
        lines.c.source == "transcript",
        lines.c.search.bool_op("@@")(query),
        t.c.deleted_at.is_(None),
        *_between(lines.c.at, since, until),
    ]
    if login is not None:
        where.append(func.lower(u.c.login) == login.lower())
    if runtime is not None:
        where.append(t.c.runtime == runtime)
    statement = (
        select(
            lines.c.session_id,
            lines.c.file,
            lines.c.line,
            lines.c.at,
            lines.c.role,
            _headline(lines.c.text, query).label("snippet"),
            rank.label("rank"),
            t.c.label,
            t.c.runtime,
            u.c.login,
        )
        .join_from(lines, t, t.c.id == lines.c.transcript_id)
        .join(u, u.c.id == t.c.user_id)
        .where(*where)
        .order_by(rank.desc(), lines.c.at.desc().nulls_last(), lines.c.id.desc())
        .limit(SCAN_HITS)
    )
    found = []
    for row in await conn.execute(statement):
        if through is None or not access.visible(row.label, through):
            continue
        found.append(
            _gated(
                HistoryHit(
                    source="transcript",
                    at=row.at,
                    role=row.role,
                    text=row.snippet,
                    rank=float(row.rank),
                    evidence=line_evidence(row.session_id, row.file, row.line),
                    session_id=row.session_id,
                    file=row.file,
                    line=row.line,
                    run_id=None,
                    run_kind=None,
                    chat_id=None,
                    chat=None,
                    login=row.login,
                    runtime=row.runtime,
                ),
                ("label", row.label),
            )
        )
    return found


async def _run_hits(conn, access, runs_read: RunReader, query, since, until, login, runtime) -> list[HistoryHit]:
    lines, r, u = tables.history_lines, tables.runs, tables.users
    rank = func.ts_rank(lines.c.search, query)
    where = [
        lines.c.project_id == access.project_id,
        lines.c.source == "run",
        lines.c.search.bool_op("@@")(query),
        runs_read.clause(r),
        *_between(lines.c.at, since, until),
    ]
    if login is not None:
        where.append(func.lower(u.c.login) == login.lower())
    if runtime is not None:
        where.append(r.c.runtime == runtime)
    statement = (
        select(
            lines.c.run_id,
            lines.c.line,
            lines.c.at,
            lines.c.role,
            _headline(lines.c.text, query).label("snippet"),
            rank.label("rank"),
            r.c.kind,
            r.c.plan_id,
            r.c.dispatched_by,
            r.c.runtime,
            u.c.login,
        )
        .join_from(lines, r, r.c.id == lines.c.run_id)
        .join(u, u.c.id == r.c.dispatched_by)
        .where(*where)
        .order_by(rank.desc(), lines.c.at.desc().nulls_last(), lines.c.id.desc())
        .limit(SCAN_HITS)
    )
    return [
        _gated(
            HistoryHit(
                source="run",
                at=row.at,
                role=row.role,
                text=row.snippet,
                rank=float(row.rank),
                evidence=f"run:{row.run_id}:{row.line}",
                session_id=None,
                file=None,
                line=row.line,
                run_id=row.run_id,
                run_kind=row.kind,
                chat_id=None,
                chat=None,
                login=row.login,
                runtime=row.runtime,
            ),
            ("run", row.kind, row.plan_id, row.dispatched_by),
        )
        for row in await conn.execute(statement)
    ]


def _through_sink(access: ProjectAccess, label, through: str | None) -> bool:
    """Whether the project's part of a chat message's label (``chat_access.LabelProject.label``) is visible through
    ``through``, the sink the caller reads through."""
    named = [item for item in label or [] if isinstance(item, dict) and item.get("projects") == [access.name]]
    return through is not None and len(named) == 1 and access.visible(named[0], through)


async def _chat_hits(conn, user: Principal, access, through, query, since, until, login) -> list[HistoryHit]:
    m, cm, c, u, ap = (tables.chat_messages, tables.chat_members, tables.chats, tables.users, tables.agent_profiles)
    vector = history_index.vector(m.c.body)
    rank = func.ts_rank(vector, query)
    member = and_(cm.c.chat_id == m.c.chat_id, cm.c.user_id == user.user_id, cm.c.removed_at.is_(None))
    author = func.coalesce(u.c.login, ap.c.handle)
    where = [vector.bool_op("@@")(query), m.c.label.contains([{"projects": [access.name]}])]
    where += _between(m.c.created_at, since, until)
    if login is not None:
        where.append(func.lower(u.c.login) == login.lower())
    statement = (
        select(
            m.c.chat_id,
            m.c.seq,
            m.c.created_at,
            m.c.label,
            m.c.author_profile_id,
            c.c.title,
            author.label("author"),
            _headline(m.c.body, query).label("snippet"),
            rank.label("rank"),
        )
        .select_from(
            m.join(cm, member)
            .join(c, c.c.id == m.c.chat_id)
            .outerjoin(u, u.c.id == m.c.author_user_id)
            .outerjoin(ap, ap.c.id == m.c.author_profile_id)
        )
        .where(*where)
        .order_by(rank.desc(), m.c.created_at.desc(), m.c.id.desc())
        .limit(SCAN_HITS)
    )
    rows = (await conn.execute(statement)).all()
    reader = await label_reader(conn, user.user_id, [row.label for row in rows])
    cleared: dict[int, bool] = {}
    found = []
    for row in rows:
        if row.chat_id not in cleared:
            try:
                await chat_access(conn, user, row.chat_id)
                cleared[row.chat_id] = True
            except HTTPException:
                cleared[row.chat_id] = False
        if not cleared[row.chat_id] or not reader.sees(row.label) or not _through_sink(access, row.label, through):
            continue
        found.append(
            _gated(
                HistoryHit(
                    source="chat",
                    at=row.created_at,
                    role="agent" if row.author_profile_id is not None else "person",
                    text=row.snippet,
                    rank=float(row.rank),
                    evidence=None,
                    session_id=None,
                    file=None,
                    line=row.seq,
                    run_id=None,
                    run_kind=None,
                    chat_id=row.chat_id,
                    chat=row.title,
                    login=row.author,
                    runtime=None,
                ),
                ("chat", row.label),
            )
        )
    return found


def _hit_id(hit: HistoryHit) -> str:
    return hit.evidence or f"chat:{hit.chat_id}:{hit.line}"


async def _cleared(conn, user: Principal, access, sink: str, hits: list[HistoryHit]) -> list[bool]:
    """Which of ``hits`` ``sink`` may read, as ``rankers.clears`` decides it for a label; a run through the sink's reading
    of the plans (``readable_run``'s rule, for the sink named); a hit without a gate is not read."""
    rules = access.rules
    reader = None
    found = []
    for hit in hits:
        gate = hit._gate
        if gate is None:
            found.append(False)
        elif gate[0] == "label":
            found.append(rankers.clears(rules, gate[1], sink))
        elif gate[0] == "chat":
            named = [item for item in gate[1] or [] if isinstance(item, dict) and item.get("projects") == [access.name]]
            found.append(len(named) == 1 and rankers.clears(rules, named[0], sink))
        else:
            reader = reader or await run_reader(conn, user, access, sink)
            found.append(reader.reads(gate[1], gate[2], gate[3]))
    return found


async def _reranked(conn, state, user: Principal, access, text: str, page: list[HistoryHit]) -> list[HistoryHit] | None:
    """``page`` in the order the project's rankers give it, or None when no ranker changed anything."""
    found = await rankers.project_rankers(conn, access.name, access.project_id, access.rules)
    if not found or len(page) < 2:
        return None
    cleared = {sink: await _cleared(conn, user, access, sink, page) for sink in {each.sink for each in found}}
    seen = [rankers.RankHit(_hit_id(hit), rankers.cut(hit.text), hit.source) for hit in page]
    order = await rankers.rerank(state, found, text, "history", seen, cleared)
    if order is None:
        return None
    by_id = {_hit_id(hit): hit for hit in page}
    return [by_id[each] for each in order]


async def search_history(
    conn: AsyncConnection,
    user: Principal,
    access: ProjectAccess,
    sink: str | None,
    text: str,
    *,
    sources: list[str] | None = None,
    since: date | None = None,
    until: date | None = None,
    login: str | None = None,
    runtime: str | None = None,
    limit: int = 20,
    offset: int = 0,
    state=None,
) -> HistorySearch:
    """The hits of ``text`` in the project's history ``user`` may read, best first (see the module). With the app's
    ``state``, the rankers of the plugins the project enabled put the page asked for in their order
    (``evo_agents.hub.server.rankers``)."""
    chats = agent_reads_chats(user)
    if not chats and "chat" in (sources or ()):
        raise HTTPException(403, NO_CHAT)
    asked = [source for source in SOURCES if source in (sources or (SOURCES if chats else AGENT_SOURCES))]
    query = history_index.tsquery(text)
    start, end = _day(since), _day(until)
    through = plan_routes._sink(access, sink)
    hits: list[HistoryHit] = []
    if "transcript" in asked:
        hits += await _transcript_hits(conn, access, through, query, start, end, login, runtime)
    if "run" in asked:
        runs_read = await run_reader(conn, user, access, sink)
        hits += await _run_hits(conn, access, runs_read, query, start, end, login, runtime)
    if "chat" in asked and runtime is None:  # a chat has no runtime
        hits += await _chat_hits(conn, user, access, through, query, start, end, login)
    floor = datetime.min.replace(tzinfo=UTC)
    hits.sort(key=lambda hit: (-hit.rank, -(hit.at or floor).timestamp()))
    page = hits[offset : offset + limit]
    moved = await _reranked(conn, state, user, access, text, page) if state is not None else None
    return HistorySearch(
        project=access.name,
        query=text,
        sources=asked,
        hits=page if moved is None else moved,
        total=len(hits),
        limit=limit,
        offset=offset,
        reranked=moved is not None,
    )


@router.get("/{project}/curator/history/search", response_model=HistorySearch, responses=REFUSALS)
async def history_search(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    q: Annotated[str, Query(min_length=1, max_length=QUERY_CHARS, description='words, "a phrase", or -excluded')],
    source: Annotated[list[Literal[SOURCES]], Query(description="these sources alone; repeat it")] = [],  # noqa: B006
    since: Day = None,
    until: Day = None,
    login: Annotated[str | None, Query(pattern=LOGIN_NAME, description="of this member")] = None,
    runtime: Annotated[Literal[local.RUNTIMES] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_SEARCH)] = 20,
    offset: Annotated[int, Query(ge=0, le=1000)] = 0,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> HistorySearch:
    """The words of the query in the project's transcripts, run logs and group chats, best first."""
    async with request.app.state.engine.begin() as conn:
        access = await _access(conn, user, project)
        return await search_history(
            conn,
            user,
            access,
            sink,
            q,
            sources=list(source),
            since=since,
            until=until,
            login=login,
            runtime=runtime,
            limit=limit,
            offset=offset,
            state=request.app.state,
        )


# The lines of a transcript


def _read_lines(store, parts: list, runtime: str, file: str, start: int, end: int) -> list[TranscriptLine]:
    reader = excerpts.ClaudeEntries(history_index.TEXT_CHARS, sidechain=file != local.MAIN)
    codex = runtime == "codex"
    found = []
    for number, line in facet_job.file_lines(store, parts, None):
        if number > end:
            break
        items = [item for item in history_index.line_entries(reader, number, line, codex) if item is not None]
        if number < start:
            continue
        found += [TranscriptLine(line=number, role=item.role, text=item.text) for item in items]
        if not items and number == start:
            kind = line.get("type") if isinstance(line, dict) else None
            found.append(TranscriptLine(line=number, role=str(kind or "line"), text=""))
    return found


async def transcript_lines(
    request: Request,
    user: Principal,
    project: str,
    session_id: str,
    *,
    line: int,
    file: str = local.MAIN,
    before: int = 10,
    after: int = 10,
    sink: str | None = None,
) -> TranscriptLines:
    """The entries of lines ``line - before`` to ``line + after`` of transcript file ``file`` of ``session_id``."""
    t, u, p = tables.session_transcripts, tables.users, tables.transcript_parts
    async with request.app.state.engine.begin() as conn:
        access = await _access(conn, user, project)
        row = (
            await conn.execute(
                select(t.c.id, t.c.label, t.c.lines, t.c.runtime, t.c.deleted_at, u.c.login)
                .join_from(t, u, u.c.id == t.c.user_id)
                .where(t.c.project_id == access.project_id, t.c.session_id == session_id, t.c.file == file)
            )
        ).one_or_none()
        through = plan_routes._sink(access, sink)
        if row is None or row.deleted_at is not None or through is None or not access.visible(row.label, through):
            raise HTTPException(404, f"project {project} has no transcript {file} of session {session_id} you may read")
        if line > row.lines:
            raise HTTPException(422, f"transcript {file} of session {session_id} has {row.lines} lines, not {line}")
        start, end = max(1, line - before), min(row.lines, line + after)
        parts = (
            await conn.execute(
                select(p.c.first_line, p.c.sha256.label("blob"), p.c.size)
                .where(p.c.transcript_id == row.id, p.c.first_line <= end, p.c.first_line + p.c.lines > start)
                .order_by(p.c.seq)
            )
        ).all()
    store = getattr(request.app.state, "blobs", None)
    try:
        entries = await asyncio.to_thread(_read_lines, store, parts, row.runtime, file, start, end)
    except facet_job.Unavailable as exc:
        raise HTTPException(503, f"the transcript cannot be read from the blob store now: {exc}") from None
    return TranscriptLines(
        project=access.name,
        session_id=session_id,
        file=file,
        runtime=row.runtime,
        login=row.login,
        lines=row.lines,
        asked=line,
        first=start,
        last=end,
        entries=entries,
        evidence=line_evidence(session_id, file, line),
    )


@router.get(
    "/{project}/transcripts/{session_id}/lines",
    response_model=TranscriptLines,
    responses={**REFUSALS, 503: {"model": ErrorBody}},
)
async def show_lines(
    request: Request,
    project: ProjectName,
    session_id: SessionId,
    user: CurrentUser,
    line: Annotated[int, Query(ge=1, le=review.MAX_LINE, description="the line to read around")],
    file: FileKey = local.MAIN,
    before: Annotated[int, Query(ge=0, le=MAX_AROUND)] = 10,
    after: Annotated[int, Query(ge=0, le=MAX_AROUND)] = 10,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> TranscriptLines:
    """The lines of a transcript around ``line``, each as its entries."""
    return await transcript_lines(
        request, user, project, session_id, line=line, file=file, before=before, after=after, sink=sink
    )


async def line_missing(conn: AsyncConnection, access: ProjectAccess, session_id: str, file: str, line: int):
    """Why line ``line`` of transcript ``file`` of ``session_id`` is no evidence of the project that ``access`` (a
    review run's owner) may cite: no such transcript it reads through the project's hub sink, its content deleted,
    or fewer lines; None when it is."""
    t = tables.session_transcripts
    row = (
        await conn.execute(
            select(t.c.label, t.c.lines, t.c.deleted_at).where(
                t.c.project_id == access.project_id, t.c.session_id == session_id, t.c.file == file
            )
        )
    ).one_or_none()
    through = access.rules.hub_sink.sink if access.rules.hub_sink else None
    if row is None or through is None or not access.visible(row.label, through):
        return f"project {access.name} has no transcript {file} of session {session_id}"
    if row.deleted_at is not None:
        return f"the content of transcript {file} of session {session_id} was deleted"
    if line > row.lines:
        return f"transcript {file} of session {session_id} has {row.lines} lines, so no line {line}"
    return None

"""Reading a project's whole history: its sessions and runs with the numbers of their facets (``list_history``), the
words of its transcripts, run logs and group chats (``search_history``), the lines of a transcript around one
(``transcript_lines``) and whether a line is evidence a review run may cite (``line_missing``). See the package's
docstring."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import excerpts, facet_job, history, history_index, review
from evo_agents.hub import transcripts as local
from evo_agents.hub.server import rankers
from evo_agents.hub.server.base.errors import Forbidden, NotFound, Unprocessable
from evo_agents.hub.server.chat_access import chat_access, label_reader
from evo_agents.hub.server.history import repository
from evo_agents.hub.server.history.models import (
    AGENT_SOURCES,
    SOURCES,
    HistoryHit,
    HistorySearch,
    HistorySessionItem,
    HistorySessions,
    TranscriptLine,
    TranscriptLines,
)
from evo_agents.hub.server.history.service.readers import RunReader, run_reader, through
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.runs.service.views import agent_reads_chats
from evo_agents.hub.server.security import Principal

NO_CHAT = (
    "the agent of this run searches no group chat (the Reviewer of a review run alone does): what it reads lands in its "
    "run's events and in what it writes, which readers of the project who are not in the chat read; search the "
    "transcripts and the runs"
)


def _day(value: date | None) -> datetime | None:
    return datetime.combine(value, time.min, tzinfo=UTC) if value is not None else None


def evidence_of(kind: str, session_id: str | None = None, run_id: int | None = None, seq: int = 1) -> str:
    return f"run:{run_id}:{seq}" if kind == "run" else f"session:{session_id}"


def line_evidence(session_id: str, file: str, line: int) -> str:
    return review.evidence_text({"kind": "transcript", "session_id": session_id, "file": file, "line": line})


# The sessions and runs


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
    """The sessions and runs of the project ``user`` may read that match (see the package)."""
    seen_through = through(access, sink)
    runs_read = await run_reader(conn, user, access, sink)
    into = {row.key: row.merged_into for row in await repository.cluster_rows(conn, access.project_id)}
    rows = await repository.facet_rows(conn, access.project_id, kind, _day(since), _day(until), login, runtime)
    found = []
    for row in rows:
        if row.kind == "session":
            if seen_through is None or not access.visible(row.label, seen_through):
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


# Search


def _hit(source: str, row, **fields) -> HistoryHit:
    """A hit of ``source`` from ``row`` of a search's query; ``fields`` hold where it is, and the rest is None."""
    where = dict.fromkeys(("evidence", "session_id", "file", "line", "run_id", "run_kind", "chat_id", "chat"))
    where |= {"login": None, "runtime": None}
    return HistoryHit(source=source, text=row.snippet, rank=float(row.rank), **{**where, **fields})


async def _transcript_hits(conn, access, seen_through, query, since, until, login, runtime) -> list[HistoryHit]:
    rows = await repository.transcript_hits(conn, access.project_id, query, since, until, login, runtime)
    return [
        _hit(
            "transcript",
            row,
            at=row.at,
            role=row.role,
            evidence=line_evidence(row.session_id, row.file, row.line),
            session_id=row.session_id,
            file=row.file,
            line=row.line,
            login=row.login,
            runtime=row.runtime,
        ).gated(("label", row.label))
        for row in rows
        if seen_through is not None and access.visible(row.label, seen_through)
    ]


async def _run_hits(conn, access, runs_read: RunReader, query, since, until, login, runtime) -> list[HistoryHit]:
    rows = await repository.run_hits(conn, access.project_id, runs_read.clause(), query, since, until, login, runtime)
    return [
        _hit(
            "run",
            row,
            at=row.at,
            role=row.role,
            evidence=f"run:{row.run_id}:{row.line}",
            line=row.line,
            run_id=row.run_id,
            run_kind=row.kind,
            login=row.login,
            runtime=row.runtime,
        ).gated(("run", row.kind, row.plan_id, row.dispatched_by))
        for row in rows
    ]


def _through_sink(access: ProjectAccess, label, seen_through: str | None) -> bool:
    """Whether the project's part of a chat message's label (``chat_access.LabelProject.label``) is visible through
    ``seen_through``, the sink the caller reads through."""
    named = [item for item in label or [] if isinstance(item, dict) and item.get("projects") == [access.name]]
    return seen_through is not None and len(named) == 1 and access.visible(named[0], seen_through)


async def _chat_hits(conn, user: Principal, access, seen_through, query, since, until, login) -> list[HistoryHit]:
    rows = await repository.chat_hits(conn, user.user_id, access.name, query, since, until, login)
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
        if not cleared[row.chat_id] or not reader.sees(row.label) or not _through_sink(access, row.label, seen_through):
            continue
        role = "agent" if row.author_profile_id is not None else "guest" if row.guest_name else "person"
        where = {"line": row.seq, "chat_id": row.chat_id, "chat": row.title, "login": row.author}
        found.append(_hit("chat", row, at=row.created_at, role=role, **where).gated(("chat", row.label)))
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
        gate = hit.gate
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
    """The hits of ``text`` in the project's history ``user`` may read, best first (see the package). With the app's
    ``state``, the rankers of the plugins the project enabled put the page asked for in their order
    (``evo_agents.hub.server.rankers``)."""
    chats = agent_reads_chats(user)
    if not chats and "chat" in (sources or ()):
        raise Forbidden(NO_CHAT)
    asked = [source for source in SOURCES if source in (sources or (SOURCES if chats else AGENT_SOURCES))]
    query = history_index.tsquery(text)
    start, end = _day(since), _day(until)
    seen_through = through(access, sink)
    hits: list[HistoryHit] = []
    if "transcript" in asked:
        hits += await _transcript_hits(conn, access, seen_through, query, start, end, login, runtime)
    if "run" in asked:
        runs_read = await run_reader(conn, user, access, sink)
        hits += await _run_hits(conn, access, runs_read, query, start, end, login, runtime)
    if "chat" in asked and runtime is None:  # a chat has no runtime
        hits += await _chat_hits(conn, user, access, seen_through, query, start, end, login)
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
    conn: AsyncConnection,
    access: ProjectAccess,
    store,
    project: str,
    session_id: str,
    *,
    line: int,
    file: str = local.MAIN,
    before: int = 10,
    after: int = 10,
    sink: str | None = None,
) -> TranscriptLines:
    """The entries of lines ``line - before`` to ``line + after`` of transcript file ``file`` of ``session_id``,
    read from ``store`` (the blob store, or None); 503 when it cannot be read now."""
    row = await repository.transcript_of(conn, access.project_id, session_id, file)
    seen_through = through(access, sink)
    if row is None or row.deleted_at is not None or seen_through is None or not access.visible(row.label, seen_through):
        raise NotFound(f"project {project} has no transcript {file} of session {session_id} you may read")
    if line > row.lines:
        raise Unprocessable(f"transcript {file} of session {session_id} has {row.lines} lines, not {line}")
    start, end = max(1, line - before), min(row.lines, line + after)
    parts = await repository.parts_between(conn, row.id, start, end)
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


async def line_missing(conn: AsyncConnection, access: ProjectAccess, session_id: str, file: str, line: int):
    """Why line ``line`` of transcript ``file`` of ``session_id`` is no evidence of the project that ``access`` (a
    review run's owner) may cite: no such transcript it reads through the project's hub sink, its content deleted,
    or fewer lines; None when it is."""
    row = await repository.transcript_head(conn, access.project_id, session_id, file)
    hub_sink = access.rules.hub_sink.sink if access.rules.hub_sink else None
    if row is None or hub_sink is None or not access.visible(row.label, hub_sink):
        return f"project {access.name} has no transcript {file} of session {session_id}"
    if row.deleted_at is not None:
        return f"the content of transcript {file} of session {session_id} was deleted"
    if line > row.lines:
        return f"transcript {file} of session {session_id} has {row.lines} lines, so no line {line}"
    return None

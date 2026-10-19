"""The job hub.index_history: the words of every transcript and of every run that ended, in history_lines (schema
0057), so a member and the Curator's Reviewer search a project's whole history with Postgres full-text search, no
service besides Postgres (``evo_agents.hub.server.history_search`` reads it).

Each line of a transcript, and each event of a run, is read into its entries as the history run reads them
(``evo_agents.hub.excerpts``: what the person wrote, what the agent said, each tool call and its result, the errors of
the model's API), each text within TEXT_CHARS, its start and its end. A row per entry keeps where it came (a
transcript's session, file and line; a run's event seq), when, who, the text and its tsvector of the configuration
``simple``: no stemming, since the history mixes Vietnamese, English and code.

Each pass, every 5 minutes, takes up to PASS_ITEMS things, the latest first, and stops after PASS_SECONDS, leaving the
rest to the next pass:

- a transcript the hub holds (content not deleted) with parts it has not indexed: only those parts are read, since a
  part never changes, from the blob store, a line at a time;
- a run that ended SETTLE ago (``runs.TERMINAL_STATES``, a local run's ``ended``; not a history run, whose batches are
  of other sessions), whose fingerprint (its log's hash and its count of events, as ``facet_job.run_fingerprint``)
  differs from the one it was indexed at: its rows are written again from its log, else its events on the hub.

The rows of one thing are written in one transaction with its progress (history_indexed), under the lock of its
transcript's row, so a transcript whose content an admin or the retention deleted meanwhile gets no row
(``evo_agents.hub.transcript_prune`` deletes its rows with its parts). A blob store that does not answer, or a blob
that is missing, fails the attempt: the next waits RETRY_FIRST, twice as long after each failure up to RETRY_MAX. The
job runs one pass at a time (the lock and the queueing lock of ``hub.index_history``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import excerpts, facet_job, facets, tables
from evo_agents.hub import transcripts as local
from evo_agents.hub.blobs import BlobStore

log = logging.getLogger(__name__)

SOURCES = ("transcript", "run")
TEXT_CHARS = 2000  # of one row's text: its start and its end
ROLE_CHARS = 200
SETTLE = timedelta(minutes=2)  # a run this long ended is indexed: its last events have come
PASS_ITEMS = 100
PASS_SECONDS = 240.0
RETRY_FIRST = timedelta(minutes=5)
RETRY_MAX = timedelta(days=1)
ERROR_CHARS = 2000
INSERT_ROWS = 500  # rows one INSERT writes
CONFIG = "simple"  # the text search configuration of the rows and of the queries


@dataclass(frozen=True)
class Todo:
    kind: str  # transcript or run
    project_id: int
    transcript_id: int | None = None
    run_id: int | None = None


@dataclass
class Rows:
    """The rows of one thing, as its entries are read."""

    values: list[dict] = field(default_factory=list)

    def add(self, base: dict, number: int, at, items) -> None:
        for item in items:
            if item is None or not item.text:
                continue
            self.values.append({**base, "line": number, "at": at, "role": item.role[:ROLE_CHARS], "text": item.text})


def _due(h, now: datetime):
    return or_(h.c.retry_at.is_(None), h.c.retry_at <= now)


# What to index


async def _transcripts_todo(conn: AsyncConnection, now: datetime, limit: int) -> list[Todo]:
    t, h = tables.session_transcripts, tables.history_indexed
    query = (
        select(t.c.id, t.c.project_id)
        .select_from(t.outerjoin(h, h.c.transcript_id == t.c.id))
        .where(t.c.deleted_at.is_(None), t.c.parts > func.coalesce(h.c.parts, 0), _due(h, now))
        .order_by(t.c.updated_at.desc(), t.c.id.desc())
        .limit(limit)
    )
    return [Todo("transcript", row.project_id, transcript_id=row.id) for row in await conn.execute(query)]


def _run_print(r):
    return func.concat(func.coalesce(r.c.log_sha256, "-"), ":", r.c.event_seq)


async def _runs_todo(conn: AsyncConnection, now: datetime, limit: int) -> list[Todo]:
    r, h = tables.runs, tables.history_indexed
    query = (
        select(r.c.id, r.c.project_id)
        .select_from(r.outerjoin(h, h.c.run_id == r.c.id))
        .where(
            r.c.state.in_(facet_job.RUN_ENDED),
            r.c.kind != "history",
            r.c.finished_at < now - SETTLE,
            h.c.fingerprint.is_distinct_from(_run_print(r)),
            _due(h, now),
        )
        .order_by(r.c.finished_at.desc(), r.c.id.desc())
        .limit(limit)
    )
    return [Todo("run", row.project_id, run_id=row.id) for row in await conn.execute(query)]


async def todo(conn: AsyncConnection, now: datetime, limit: int = PASS_ITEMS) -> list[Todo]:
    """What a pass at ``now`` indexes: transcripts with new parts first, then runs."""
    found = await _transcripts_todo(conn, now, limit)
    if len(found) < limit:
        found += await _runs_todo(conn, now, limit - len(found))
    return found


# Reading


def line_entries(reader, number: int, line, codex: bool) -> list:
    """The entries of line ``number`` of a transcript, ``reader`` a ``ClaudeEntries`` for a Claude Code one."""
    return excerpts.codex_entries(number, line, chars=TEXT_CHARS) if codex else reader.read(number, line)


def _read_transcript(store, row, parts: list, scratch: Path | None) -> Rows:
    rows = Rows()
    base = {
        "project_id": row.project_id,
        "source": "transcript",
        "transcript_id": row.id,
        "session_id": row.session_id,
        "file": row.file,
    }
    reader = excerpts.ClaudeEntries(TEXT_CHARS, sidechain=row.file != local.MAIN)
    codex = row.runtime == "codex"
    for number, line in facet_job.file_lines(store, parts, scratch):
        at = facets.timestamp(line.get("timestamp")) if isinstance(line, dict) else None
        rows.add(base, number, at, line_entries(reader, number, line, codex))
    return rows


class _RunCollector:
    """Where a run's entries go (``excerpts.RunEntries``): rows, each at the seq its ``where`` names."""

    def __init__(self, base: dict):
        self.base, self.rows, self.at = base, Rows(), {}

    def add(self, item) -> None:
        if item is None:
            return
        seq = item.where.lstrip("#")
        if seq.isdigit():
            self.rows.add(self.base, int(seq), self.at.get(int(seq)), [item])


class _RunReader:
    """A run's events into ``_RunCollector``, as ``facet_job.stored_events`` and ``read_log`` hand them."""

    def __init__(self, collector: _RunCollector):
        self.collector = collector
        self.entries = excerpts.RunEntries(chars=TEXT_CHARS, cutter=collector)

    def add(self, event) -> None:
        if isinstance(event, dict) and str(event.get("seq") or "").isdigit():
            self.collector.at[int(event["seq"])] = facets.timestamp(event.get("at"))
        self.entries.add(event)


async def _read_run(engine: AsyncEngine, store, item: Todo, scratch: Path | None):
    """(the rows of run ``item``, its fingerprint); None when it is gone."""
    r = tables.runs
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(r.c.project_id, r.c.log_sha256, r.c.event_seq, _run_print(r).label("print")).where(
                    r.c.id == item.run_id
                )
            )
        ).one_or_none()
    if row is None:
        return None
    collector = _RunCollector({"project_id": row.project_id, "source": "run", "run_id": item.run_id})
    reader = _RunReader(collector)
    read = False
    if row.log_sha256 and store is not None:
        try:
            await asyncio.to_thread(facet_job.read_log, store, row.log_sha256, reader, scratch)
            read = True
        except facet_job.Unavailable:
            if await asyncio.to_thread(store.has_blob, row.log_sha256):
                raise  # the log is there but cannot be read now: try again later
            collector = _RunCollector(collector.base)
            reader = _RunReader(collector)
    if not read:
        await facet_job.stored_events(engine, item.run_id, reader)
    reader.entries.close()
    return collector.rows, row.print


# Writing


async def _insert(conn: AsyncConnection, rows: Rows) -> None:
    """Write ``rows``, INSERT_ROWS in one statement, each with the tsvector of its text."""
    lines = tables.history_lines
    for start in range(0, len(rows.values), INSERT_ROWS):
        batch = rows.values[start : start + INSERT_ROWS]
        await conn.execute(lines.insert().values([{**value, "search": vector(value["text"])} for value in batch]))


async def _progress(conn: AsyncConnection, item: Todo, **values) -> None:
    h = tables.history_indexed
    keys = {"project_id": item.project_id, "transcript_id": item.transcript_id, "run_id": item.run_id}
    statement = pg_insert(h).values(**keys, **values)
    target = [h.c.transcript_id] if item.kind == "transcript" else [h.c.run_id]
    changes = {name: statement.excluded[name] for name in values} | {"updated_at": func.now()}
    await conn.execute(statement.on_conflict_do_update(index_elements=target, set_=changes))


async def _index_transcript(engine: AsyncEngine, store, item: Todo, now: datetime, scratch: Path | None) -> int | None:
    """Index the parts of transcript ``item`` not indexed yet; the rows written, None when it is gone."""
    t, p, h = tables.session_transcripts, tables.transcript_parts, tables.history_indexed
    async with engine.connect() as conn:
        row = (await conn.execute(select(t).where(t.c.id == item.transcript_id))).one_or_none()
        done = (await conn.execute(select(h.c.parts).where(h.c.transcript_id == item.transcript_id))).scalar()
        if row is None or row.deleted_at is not None:
            return None
        parts = (
            await conn.execute(
                select(p.c.seq, p.c.first_line, p.c.sha256.label("blob"), p.c.size)
                .where(p.c.transcript_id == row.id, p.c.seq > (done or 0))
                .order_by(p.c.seq)
            )
        ).all()
    if not parts:
        return 0
    rows = await asyncio.to_thread(_read_transcript, store, row, parts, scratch)
    async with engine.begin() as conn:
        held = (
            await conn.execute(select(t.c.deleted_at, t.c.parts).where(t.c.id == row.id).with_for_update())
        ).one_or_none()
        if held is None or held.deleted_at is not None:
            return None
        first = parts[0].first_line
        lines = tables.history_lines
        await conn.execute(delete(lines).where(lines.c.transcript_id == row.id, lines.c.line >= first))
        await _insert(conn, rows)
        indexed = (await conn.execute(select(h.c.lines).where(h.c.transcript_id == row.id))).scalar() or 0
        await _progress(
            conn,
            item,
            parts=parts[-1].seq,
            lines=indexed + len(rows.values),
            indexed_at=now,
            attempts=0,
            error=None,
            retry_at=None,
        )
    return len(rows.values)


async def _index_run(engine: AsyncEngine, store, item: Todo, now: datetime, scratch: Path | None) -> int | None:
    found = await _read_run(engine, store, item, scratch)
    if found is None:
        return None
    rows, fingerprint = found
    lines = tables.history_lines
    async with engine.begin() as conn:
        await conn.execute(delete(lines).where(lines.c.run_id == item.run_id))
        await _insert(conn, rows)
        values = {"fingerprint": fingerprint, "lines": len(rows.values), "indexed_at": now}
        await _progress(conn, item, **values, attempts=0, error=None, retry_at=None)
    return len(rows.values)


async def _failed(engine: AsyncEngine, item: Todo, error: str, now: datetime) -> datetime:
    h = tables.history_indexed
    key = h.c.transcript_id == item.transcript_id if item.kind == "transcript" else h.c.run_id == item.run_id
    async with engine.begin() as conn:
        attempts = (await conn.execute(select(h.c.attempts).where(key))).scalar_one_or_none() or 0
        retry_at = now + min(RETRY_FIRST * 2**attempts, RETRY_MAX)
        values = {"attempts": attempts + 1, "error": facets.cut(error, ERROR_CHARS), "retry_at": retry_at}
        await _progress(conn, item, **values)
    return retry_at


async def index_history(
    engine: AsyncEngine,
    store: BlobStore | None,
    *,
    now: datetime | None = None,
    limit: int = PASS_ITEMS,
    seconds: float = PASS_SECONDS,
    scratch: Path | None = None,
) -> dict:
    """One pass of hub.index_history at ``now`` (the database's now when None): how many transcripts and runs it
    indexed, the rows it wrote, the attempts that failed, how many it left for the next pass and how many were gone."""
    async with engine.connect() as conn:
        moment = now or (await conn.execute(select(func.now()))).scalar_one()
        items = await todo(conn, moment, limit)
    if scratch is not None:
        scratch.mkdir(parents=True, exist_ok=True)
    outcomes: Counter = Counter()
    deadline = time.monotonic() + seconds
    for index, item in enumerate(items):
        if time.monotonic() > deadline:
            outcomes["later"] += len(items) - index
            break
        work = _index_transcript if item.kind == "transcript" else _index_run
        try:
            written = await work(engine, store, item, moment, scratch)
        except Exception as exc:  # one thing that cannot be read leaves the others their turn
            if not isinstance(exc, facet_job.Unavailable):
                log.exception("history not indexed", extra={"kind": item.kind, "run_id": item.run_id})
            await _failed(engine, item, f"{type(exc).__name__}: {exc}", moment)
            outcomes["failed"] += 1
            continue
        if written is None:
            outcomes["gone"] += 1
            continue
        outcomes[f"{item.kind}s"] += 1
        outcomes["rows"] += written
    if items:
        log.info("history indexed", extra=dict(outcomes))
    return dict(sorted(outcomes.items()))


async def drop_transcripts(conn: AsyncConnection, ids: list[int]) -> None:
    """Delete the rows and the progress of the transcripts ``ids``, whose content is being deleted."""
    if not ids:
        return
    lines, h = tables.history_lines, tables.history_indexed
    await conn.execute(delete(lines).where(lines.c.transcript_id.in_(ids)))
    await conn.execute(update(h).values(parts=0, lines=0, updated_at=func.now()).where(h.c.transcript_id.in_(ids)))


def vector(text):
    """The tsvector of ``text`` (a value or a column) the rows hold."""
    return func.to_tsvector(CONFIG, text)


def tsquery(text: str):
    """The query of ``text`` as a person writes one (``websearch_to_tsquery``): words, "a phrase", -excluded."""
    return func.websearch_to_tsquery(CONFIG, text)

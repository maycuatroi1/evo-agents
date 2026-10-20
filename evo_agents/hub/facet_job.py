"""The job hub.analyze_facets: the deterministic facet of every session and every run that ended, made once, kept in
session_facets (schema 0054), and made again when its content or FACET_VERSION changed. ``evo_agents.hub.facets``
describes a facet; ``evo_agents.hub.server.collect`` counts the night's figures from the facets.

Each pass, every 5 minutes, takes up to PASS_ITEMS things to analyze, the latest first, and stops after PASS_SECONDS,
leaving the rest to the next pass:

- a session whose transcripts the hub holds (``session_transcripts`` with parts, content not deleted), quiet for
  SETTLE, whose facet is missing, of another version, made from its digest, or made from other content: its
  fingerprint is the md5 of its files' chains of hashes, ``file:sha256`` joined by commas in the order of the files'
  bytes (``fingerprint``). Its parts are read from the blob store, gunzipped, a line at a time;
- a run that ended (``runs.TERMINAL_STATES``, a local run's ``ended``) SETTLE ago, whose facet is missing, of another
  version, or made before its log came or more events arrived: its fingerprint is its log's hash and its count of
  events (``run_fingerprint``). The log a worker uploaded (blob kind run-log) when there is one, else the run's events
  on the hub, read EVENT_PAGE at a time;
- a session the hub holds only the digest of, quiet for SETTLE, whose facet is missing, of another version or made
  from another digest (the md5 of the digest's JSON): the facet of the digest (``facets.digest_facet``). A facet made
  from transcripts is never replaced by one from a digest.

It holds no lock on the tables the hub writes to: what it reads comes in short reads, each its own transaction, the
blobs and the parsing outside any, and a facet is written with one upsert of its row, so a part, an event or a digest
arriving meanwhile waits for nothing. A blob store that does not answer, or a blob that is missing, fails the attempt:
the facet made before stays, the row counts ``attempts`` and keeps the ``error``, and the next attempt waits
RETRY_FIRST, twice as long after each failure up to RETRY_MAX. The same content makes the same facet, so a pass that
runs twice, or two at once, writes the same rows. The job is queued once at a time and runs one at a time (the lock
and the queueing lock of ``hub.analyze_facets``).

``coverage`` counts, for the night's figures, the sessions with transcripts and how many have a facet of their
content at this version, those waiting, the failed attempts, the facets made from digests, and the same for runs.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import tempfile
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import Text, and_, cast, exists, func, or_, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import curator, facets, runs, tables
from evo_agents.hub import transcripts as local
from evo_agents.hub.blobs import KIND_LIMITS, BlobStore, BlobStoreUnavailable, blob_key
from evo_agents.hub.digest import MAX_SESSION_ID
from evo_agents.hub.facet_readers import RunFacet, session_facet

log = logging.getLogger(__name__)

SETTLE = timedelta(minutes=10)  # a session or a run this quiet is analyzed; one that is still going waits
PASS_ITEMS = 200
PASS_SECONDS = 240.0
RETRY_FIRST = timedelta(minutes=5)
RETRY_MAX = timedelta(days=1)
ERROR_CHARS = 2000  # migration 0054's bound of session_facets.error
EVENT_PAGE = 500
RUN_ENDED = (*runs.TERMINAL_STATES, "ended")
WRITTEN = (
    "source",
    "fingerprint",
    "version",
    "user_id",
    "runtime",
    "label",
    "at",
    "summary",
    "body",
    "analyzed_at",
    "attempts",
    "error",
    "retry_at",
)


class Unavailable(Exception):
    """What a facet needs cannot be read now: the blob store does not answer, or a blob is missing."""


@dataclass(frozen=True)
class Todo:
    """One thing to analyze: a session (from its transcripts or its digest) or a run."""

    kind: str
    source: str  # transcript, digest or run: what the candidate query found
    project_id: int
    session_id: str | None = None
    run_id: int | None = None
    digest: object = None  # the digest's row, for a session the hub holds only the digest of


@dataclass(frozen=True)
class Made:
    source: str
    fingerprint: str
    body: dict
    user_id: int | None
    runtime: str | None
    label: dict | None
    at: datetime | None
    session_id: str | None = None


# Fingerprints


def fingerprint(files) -> str:
    """The fingerprint of a session's transcripts, ``files`` as (file, chain of its parts' hashes): what
    ``_live_transcripts`` computes in Postgres."""
    text = ",".join(f"{file}:{chain or ''}" for file, chain in sorted(files))
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()


def run_fingerprint(log_sha256: str | None, event_seq: int) -> str:
    """The fingerprint of a run: its log's hash, ``-`` without one, and its count of events."""
    return f"{log_sha256 or '-'}:{event_seq}"


def _run_print(r):
    return func.concat(func.coalesce(r.c.log_sha256, "-"), ":", r.c.event_seq)


def _live_transcripts(project_id: int | None = None):
    """Per session with transcripts the hub holds: its fingerprint and its latest part."""
    t = tables.session_transcripts
    entry = t.c.file + ":" + func.coalesce(t.c.sha256, "")
    joined = func.array_to_string(func.array_agg(aggregate_order_by(entry, t.c.file.collate("C"))), ",")
    where = [t.c.deleted_at.is_(None), t.c.parts > 0]
    if project_id is not None:
        where.append(t.c.project_id == project_id)
    return (
        select(
            t.c.project_id,
            t.c.session_id,
            func.md5(joined).label("fingerprint"),
            func.max(t.c.updated_at).label("latest"),
        )
        .where(*where)
        .group_by(t.c.project_id, t.c.session_id)
        .subquery("live")
    )


def _stale(f, current):
    """A facet row (outer joined, so possibly none) that is not the facet of ``current`` at this version."""
    return or_(
        f.c.id.is_(None),
        f.c.body.is_(None),
        f.c.version != facets.FACET_VERSION,
        f.c.fingerprint.is_distinct_from(current),
    )


def _due(f, now: datetime):
    return or_(f.c.retry_at.is_(None), f.c.retry_at <= now)


def _session_join(f, project_id, session_id):
    return and_(f.c.kind == "session", f.c.project_id == project_id, f.c.session_id == session_id)


# What to analyze


async def _sessions_todo(conn: AsyncConnection, now: datetime, limit: int) -> list[Todo]:
    live, f = _live_transcripts(), tables.session_facets
    query = (
        select(live.c.project_id, live.c.session_id)
        .select_from(live.outerjoin(f, _session_join(f, live.c.project_id, live.c.session_id)))
        .where(
            live.c.latest < now - SETTLE,
            or_(_stale(f, live.c.fingerprint), f.c.source != "transcript"),
            _due(f, now),
        )
        .order_by(live.c.latest.desc(), live.c.session_id)
        .limit(limit)
    )
    return [Todo("session", "transcript", row.project_id, row.session_id) for row in await conn.execute(query)]


async def _runs_todo(conn: AsyncConnection, now: datetime, limit: int) -> list[Todo]:
    r, f = tables.runs, tables.session_facets
    query = (
        select(r.c.id, r.c.project_id)
        .select_from(r.outerjoin(f, and_(f.c.kind == "run", f.c.run_id == r.c.id)))
        .where(
            r.c.state.in_(RUN_ENDED),
            r.c.finished_at < now - SETTLE,
            _stale(f, _run_print(r)),
            _due(f, now),
        )
        .order_by(r.c.finished_at.desc(), r.c.id.desc())
        .limit(limit)
    )
    return [Todo("run", "run", row.project_id, run_id=row.id) for row in await conn.execute(query)]


def _digest_print(d):
    return func.md5(cast(d.c.body, Text))


async def _digests_todo(conn: AsyncConnection, now: datetime, limit: int) -> list[Todo]:
    d, t, f = tables.session_digests, tables.session_transcripts, tables.session_facets
    live = exists().where(
        t.c.project_id == d.c.project_id, t.c.session_id == d.c.session_id, t.c.deleted_at.is_(None), t.c.parts > 0
    )
    query = (
        select(
            d.c.project_id,
            d.c.session_id,
            _digest_print(d).label("fingerprint"),
            d.c.user_id,
            d.c.label,
            d.c.body,
            d.c.updated_at,
        )
        .select_from(d.outerjoin(f, _session_join(f, d.c.project_id, d.c.session_id)))
        .where(
            d.c.updated_at < now - SETTLE,
            ~live,
            or_(f.c.source.is_(None), f.c.source == "digest"),
            _stale(f, _digest_print(d)),
            _due(f, now),
        )
        .order_by(d.c.updated_at.desc(), d.c.session_id)
        .limit(limit)
    )
    return [Todo("session", "digest", row.project_id, row.session_id, digest=row) for row in await conn.execute(query)]


async def todo(conn: AsyncConnection, now: datetime, limit: int = PASS_ITEMS) -> list[Todo]:
    """What a pass at ``now`` analyzes: sessions with transcripts first, then runs, then sessions with a digest only."""
    found = await _sessions_todo(conn, now, limit)
    for query in (_runs_todo, _digests_todo):
        if len(found) < limit:
            found += await query(conn, now, limit - len(found))
    return found


# Reading


def _json(raw: bytes):
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _blob_lines(store: BlobStore | None, sha256: str, size: int | None, kind: str, scratch: Path | None):
    """The lines of blob ``sha256`` of ``kind`` (gunzipped for a transcript part), each with its index from 0, and of
    ``size`` bytes unless it is None. Unavailable when the store does not answer, or when the blob is missing, is not
    the one recorded or cannot be read."""
    if store is None:
        raise Unavailable("the hub has no blob store")
    with tempfile.TemporaryFile(dir=scratch) as handle:
        try:
            found = store.fetch(blob_key(sha256), handle, KIND_LIMITS[kind])
        except BlobStoreUnavailable as exc:
            raise Unavailable(str(exc)) from None
        if found is None or found[0] != sha256 or (size is not None and found[1] != size):
            raise Unavailable(f"blob {sha256} of kind {kind} is missing from the blob store or not what was recorded")
        handle.seek(0)
        stream = gzip.GzipFile(fileobj=handle, mode="rb") if kind == local.KIND else handle
        try:
            yield from enumerate(stream)
        except (OSError, EOFError) as exc:
            raise Unavailable(f"blob {sha256} of kind {kind} cannot be read: {exc}") from None


def file_lines(store, parts: list, scratch: Path | None) -> Iterator[tuple[int, object]]:
    """The lines of a transcript file whose ``parts`` (first_line, blob, size) are in ``store``, each as (its number
    in the file, its JSON value or None); Unavailable when a part cannot be read."""
    for part in parts:
        for offset, raw in _blob_lines(store, part.blob, part.size, local.KIND, scratch):
            yield part.first_line + offset, _json(raw)


def _read_session(store, files: dict[str, list], runtime: str | None, scratch: Path | None) -> dict:
    return session_facet(((file, file_lines(store, parts, scratch)) for file, parts in files.items()), runtime)


async def _session(engine: AsyncEngine, store, item: Todo, scratch: Path | None) -> Made | None:
    t, p = tables.session_transcripts, tables.transcript_parts
    query = (
        select(
            t.c.file,
            t.c.sha256.label("chain"),
            t.c.user_id,
            t.c.runtime,
            t.c.label,
            t.c.updated_at,
            p.c.seq,
            p.c.first_line,
            p.c.sha256.label("blob"),
            p.c.size,
        )
        .join_from(t, p, p.c.transcript_id == t.c.id)
        .where(t.c.project_id == item.project_id, t.c.session_id == item.session_id, t.c.deleted_at.is_(None))
        .order_by(t.c.file != local.MAIN, t.c.file.collate("C"), p.c.seq)
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(query)).all()
    if not rows:
        return None
    files: dict[str, list] = {}
    for row in rows:
        files.setdefault(row.file, []).append(row)
    first = rows[0]
    body = await asyncio.to_thread(_read_session, store, files, first.runtime, scratch)
    at = facets.timestamp(body.get("ended_at")) or max(row.updated_at for row in rows)
    chains = {(row.file, row.chain) for row in rows}
    return Made("transcript", fingerprint(chains), body, first.user_id, first.runtime, first.label, at)


def _digest(item: Todo) -> Made:
    row = item.digest
    body = facets.digest_facet(row.body)
    return Made("digest", row.fingerprint, body, row.user_id, None, row.label, row.updated_at)


def _cost(usage) -> float | None:
    """What a run's usage says it cost; None when it names no cost (Codex reports tokens only)."""
    if not isinstance(usage, dict) or not any(key in usage for key in ("total_cost_usd", "cost")):
        return None
    return curator.run_cost(usage)


async def stored_events(engine: AsyncEngine, run_id: int, builder) -> None:
    """Add the events of run ``run_id`` the hub holds to ``builder`` (anything with ``add``), in seq order."""
    e, after = tables.run_events, 0
    while True:
        query = (
            select(e.c.seq, e.c.at, e.c.kind, e.c.body)
            .where(e.c.run_id == run_id, e.c.seq > after)
            .order_by(e.c.seq)
            .limit(EVENT_PAGE)
        )
        async with engine.connect() as conn:
            page = (await conn.execute(query)).all()
        for row in page:
            at = row.at.isoformat() if row.at is not None else None
            builder.add({"seq": row.seq, "at": at, "kind": row.kind, "body": row.body})
        if len(page) < EVENT_PAGE:
            return
        after = page[-1].seq


def read_log(store, sha256: str, builder, scratch: Path | None) -> None:
    """Add the events of the run log blob ``sha256`` to ``builder``; Unavailable when it cannot be read."""
    for _, raw in _blob_lines(store, sha256, None, "run-log", scratch):
        builder.add(_json(raw))


async def _run(engine: AsyncEngine, store, item: Todo, scratch: Path | None) -> Made | None:
    r = tables.runs
    columns = (
        r.c.runtime,
        r.c.dispatched_by,
        r.c.session_id,
        r.c.finished_at,
        r.c.log_sha256,
        r.c.event_seq,
        r.c.usage,
    )
    async with engine.connect() as conn:
        row = (await conn.execute(select(*columns).where(r.c.id == item.run_id))).one_or_none()
    if row is None:
        return None
    builder, source = RunFacet(row.runtime), "events"
    if row.log_sha256 and store is not None:
        try:
            await asyncio.to_thread(read_log, store, row.log_sha256, builder, scratch)
            source = "run_log"
        except Unavailable:
            if store is not None and await asyncio.to_thread(store.has_blob, row.log_sha256):
                raise  # the log is there but cannot be read now: try again later
            builder = RunFacet(row.runtime)
    if source == "events":
        await stored_events(engine, item.run_id, builder)
    body = builder.body(row.usage, _cost(row.usage))
    session = row.session_id if isinstance(row.session_id, str) and len(row.session_id) <= MAX_SESSION_ID else None
    print_ = run_fingerprint(row.log_sha256, row.event_seq)
    return Made(source, print_, body, row.dispatched_by, row.runtime, None, row.finished_at, session)


async def make(engine: AsyncEngine, store: BlobStore | None, item: Todo, scratch: Path | None = None) -> Made | None:
    """The facet of ``item``; None when what it was made from is gone. Unavailable when a blob cannot be read now."""
    if item.source == "digest":
        return _digest(item)
    if item.kind == "run":
        return await _run(engine, store, item, scratch)
    return await _session(engine, store, item, scratch)


# Writing


def _key(f, item: Todo):
    if item.kind == "run":
        return and_(f.c.kind == "run", f.c.run_id == item.run_id)
    return _session_join(f, item.project_id, item.session_id)


def _upsert(item: Todo, values: dict, updated: tuple[str, ...]):
    f = tables.session_facets
    keys = {"project_id": item.project_id, "kind": item.kind, "run_id": item.run_id}
    if item.kind == "session":
        keys["session_id"] = item.session_id
    statement = pg_insert(f).values(**keys, **values)
    changes = {name: statement.excluded[name] for name in updated}
    changes["updated_at"] = func.now()
    if item.kind == "run":
        target = {"index_elements": [f.c.run_id], "index_where": f.c.kind == "run"}
    else:
        target = {"index_elements": [f.c.project_id, f.c.session_id], "index_where": f.c.kind == "session"}
    return statement.on_conflict_do_update(**target, set_=changes)


async def store_facet(engine: AsyncEngine, item: Todo, made: Made, now: datetime) -> None:
    values = {
        "source": made.source,
        "fingerprint": made.fingerprint,
        "version": facets.FACET_VERSION,
        "user_id": made.user_id,
        "runtime": made.runtime,
        "label": made.label,
        "at": made.at,
        "summary": facets.summary(made.body),
        "body": made.body,
        "analyzed_at": now,
        "attempts": 0,
        "error": None,
        "retry_at": None,
    }
    written = WRITTEN
    if item.kind == "run":
        values["session_id"] = made.session_id
        written = (*WRITTEN, "session_id")
    async with engine.begin() as conn:
        await conn.execute(_upsert(item, values, written))


async def failed(engine: AsyncEngine, item: Todo, error: str, now: datetime) -> datetime:
    """Record a failed attempt at ``item``'s facet, keeping the facet made before; when the next one may come."""
    f = tables.session_facets
    async with engine.begin() as conn:
        attempts = (await conn.execute(select(f.c.attempts).where(_key(f, item)))).scalar_one_or_none() or 0
        retry_at = now + min(RETRY_FIRST * 2**attempts, RETRY_MAX)
        values = {"attempts": attempts + 1, "error": facets.cut(error, ERROR_CHARS), "retry_at": retry_at}
        await conn.execute(_upsert(item, values, ("attempts", "error", "retry_at")))
    return retry_at


async def analyze_facets(
    engine: AsyncEngine,
    store: BlobStore | None,
    *,
    now: datetime | None = None,
    limit: int = PASS_ITEMS,
    seconds: float = PASS_SECONDS,
    scratch: Path | None = None,
) -> dict:
    """One pass of hub.analyze_facets at ``now`` (the database's now when None): how many facets it made from each
    source, how many attempts failed, how many it left for the next pass (``later``) and how many things were gone."""
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
        try:
            made = await make(engine, store, item, scratch)
        except Exception as exc:  # one facet that cannot be made leaves the others their turn
            if not isinstance(exc, Unavailable):
                log.exception("a facet could not be made", extra={"kind": item.kind, "run_id": item.run_id})
            retry_at = await failed(engine, item, f"{type(exc).__name__}: {exc}", moment)
            log.info(
                "facet waits", extra={"session_id": item.session_id, "run_id": item.run_id, "until": str(retry_at)}
            )
            outcomes["failed"] += 1
            continue
        if made is None:
            outcomes["gone"] += 1
            continue
        await store_facet(engine, item, made, moment)
        outcomes[made.source] += 1
    if items:
        log.info("facets analyzed", extra=dict(outcomes))
    return dict(sorted(outcomes.items()))


# Coverage


async def coverage(conn: AsyncConnection, project_id: int) -> dict:
    """How far the facets of project ``project_id`` are: its sessions with transcripts and those whose facet is of
    their content at FACET_VERSION (``analyzed``; the others ``waiting``), its facets made from digests, its ended runs
    and those analyzed, and the facets whose last attempt failed."""
    live, f, r = _live_transcripts(project_id), tables.session_facets, tables.runs
    current = and_(
        f.c.body.is_not(None),
        f.c.version == facets.FACET_VERSION,
        f.c.source == "transcript",
        f.c.fingerprint == live.c.fingerprint,
    )
    sessions = (
        await conn.execute(
            select(func.count().label("total"), func.count().filter(current).label("analyzed")).select_from(
                live.outerjoin(f, _session_join(f, live.c.project_id, live.c.session_id))
            )
        )
    ).one()
    ran = and_(f.c.body.is_not(None), f.c.version == facets.FACET_VERSION, f.c.fingerprint == _run_print(r))
    run_counts = (
        await conn.execute(
            select(func.count().label("total"), func.count().filter(ran).label("analyzed"))
            .select_from(r.outerjoin(f, and_(f.c.kind == "run", f.c.run_id == r.c.id)))
            .where(r.c.project_id == project_id, r.c.state.in_(RUN_ENDED))
        )
    ).one()
    mine = f.c.project_id == project_id
    digests = func.count().filter(f.c.kind == "session", f.c.source == "digest", f.c.body.is_not(None))
    row = (
        await conn.execute(
            select(digests.label("digests"), func.count().filter(f.c.attempts > 0).label("failed")).where(mine)
        )
    ).one()
    return {
        "version": facets.FACET_VERSION,
        "sessions": sessions.total,
        "analyzed": sessions.analyzed,
        "waiting": sessions.total - sessions.analyzed,
        "from_digests": row.digests,
        "runs": run_counts.total,
        "runs_analyzed": run_counts.analyzed,
        "failed": row.failed,
    }

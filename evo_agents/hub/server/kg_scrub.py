"""Old copies of secrets on the hub, for a hub admin: the built graphs and the source blobs a project holds in the
blob store, read with the patterns of the git connector (``evo_agents.kg.scrub``), and deleted on request.

POST /v1/admin/kg/scrub reads one page of a project's objects of kind kg-graph and kg-blob, by SHA-256 after
``after``, at most ``limit`` of them and PAGE_BYTES of their bytes, and answers those holding a secret: hash, kind,
size, the secrets by kind (never a value), and their state. Pass ``next`` as ``after`` for the next page; it is null
after the last. A graph is read from the api's cache when it holds it, from the bucket otherwise. A source blob is read
as the file the items of the project's run logs say it is (``props.path``), as ``evo-agents kg scrub`` reads it on a
machine: the path decides whether a bare value after ``=`` is an expression (code) or a literal (config files,
documents). A blob no item names with a path is read as a document. The paths come from every run log of the project,
ingested or pending, read once when a page first finds a source blob that may hold a secret and remembered until new
runs come; a run's log never changes, so only the new ones are read then. Run logs hold no file text, only
references, and are not themselves scanned.

The state says what a deletion would break, and nothing that is in use is deleted:

- a graph is ``newest`` when the project's latest build holding an artifact points at it: the kg_* tools read it.
  A build from a clean corpus replaces it, and the next scrub finds it old.
- a source blob is ``live`` when a run still in use refers to it: a run pending its commit, or, for each source of the
  project, its newest full run (a complete listing and an ok end) or a run after that. The build merges every run, and
  for a source whose connector lists everything each time, the items of its newest full run are its live items. Push
  a run with the secret redacted (``kg sync``, then ``hub kg push``), and the blob becomes old.
- everything else is ``old``.

A run log the bucket does not hold as the hub recorded it stops a page that found a source blob, and a purge, with 503:
without it no blob can be called old, nor read as its file.

POST /v1/admin/kg/scrub/purge takes the hashes a scan found. It reads each one again and keeps those that hold no
secret, are newest or live, or are not the project's, then, in one transaction holding the blob lock exclusively
(``evo_agents.hub.blob_gc``): the builds pointing at a graph it deletes lose ``artifact_sha256`` and get
``artifact_pruned_at``, as the retention does; the project's ``blobs`` rows of the deleted objects go; ``forget``
records the objects no other row refers to; and an audit row ``kg.scrub`` counts what went. Then the objects are
deleted from the bucket, and the deleted graphs from the api's cache. Another project holding the same bytes keeps
its row and the object.

Liveness comes from the runs, not from the merged state, which only the worker computes: when the merge kept an
older record than the newest full run holds (a removal the deletion guard held back, a machine pushing an older
checkout after a newer one), a blob in use counts as old. Deleting it makes the project's next build fail, naming the
blob; pushing a newer run of that source mends it.
"""

from __future__ import annotations

import asyncio
import io
import logging
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import blob_gc, tables
from evo_agents.hub.blobs import KIND_LIMITS, PARALLEL, BlobStore, BlobStoreUnavailable, blob_key
from evo_agents.hub.kg_build import ARTIFACT_KIND
from evo_agents.hub.kg_graph import GraphCache
from evo_agents.hub.server import audit
from evo_agents.hub.server.security import Principal
from evo_agents.kg.scrub import blob_secrets, graph_secrets, might_hold_secret, run_summary

log = logging.getLogger(__name__)

SOURCE_KIND = "kg-blob"
KINDS = (ARTIFACT_KIND, SOURCE_KIND)
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000
PAGE_BYTES = 256 * 1024 * 1024  # bytes of objects one page reads at most, beyond its first object
MAX_PURGE = 1000
LIVE_CACHE = 8  # projects whose live blobs and blob paths the api remembers, until their runs change


class ScrubFound(BaseModel):
    sha256: str
    kind: Literal["kg-graph", "kg-blob"]
    size: int
    state: Literal["old", "live", "newest"] = Field(
        description="newest: the graph the kg tools read; live: a source blob a run in use refers to; old: neither"
    )
    secrets: dict[str, int] = Field(description="the secrets it holds, by kind; never a value")
    builds: list[int] = Field(description="for a graph, the builds pointing at it")


class ScrubPage(BaseModel):
    project: str
    scanned: int = Field(description="objects read on this page")
    scanned_bytes: int
    missing: int = Field(description="objects the bucket lacks or holds with other bytes, not read")
    found: list[ScrubFound]
    next: str | None = Field(description="pass as after for the next page; null after the last")


class ScrubKept(BaseModel):
    sha256: str
    reason: Literal["newest", "live", "clean", "missing", "not held"] = Field(
        description="why it stays: in use, holds no secret now, not in the bucket, or not the project's"
    )


class ScrubPurged(BaseModel):
    project: str
    purged: list[ScrubFound]
    kept: list[ScrubKept]
    builds: int = Field(description="builds whose graph was deleted: they keep their row, content hash and counts")
    deleted: int = Field(description="objects deleted from the bucket by this request")
    deleted_bytes: int
    pending: int = Field(description="objects that still wait to be deleted; the next prune or purge tries again")


# Reading objects


def _read(store: BlobStore, cache: GraphCache, project: str, row, scratch: Path) -> dict[str, int] | str | None:
    """The secrets of the object of ``row`` (sha256, kind, size); None when the bucket lacks it or holds other
    bytes. A graph the api's cache holds is read there. A source blob that may hold a secret comes back as its text,
    for ``_read_rows`` to read as the files its run logs name."""
    if row.kind == ARTIFACT_KIND:
        cached = cache.path(project, row.sha256)
        if cached.exists():
            return graph_secrets(cached, immutable=True)[1]
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            path = Path(tmp) / "graph.sqlite"
            with open(path, "wb") as handle:
                found = store.fetch(blob_key(row.sha256), handle, row.size)
            if found != (row.sha256, row.size):
                return None
            return graph_secrets(path, immutable=True)[1]
    buffer = io.BytesIO()
    found = store.fetch(blob_key(row.sha256), buffer, KIND_LIMITS[SOURCE_KIND])
    if found != (row.sha256, row.size):
        return None
    text = buffer.getvalue().decode("utf-8", errors="replace")
    return text if might_hold_secret(text) else {}


def _read_all(store: BlobStore, cache: GraphCache, project: str, rows: list, scratch: Path) -> list:
    scratch.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=PARALLEL, thread_name_prefix="kg-scrub") as pool:
        return list(pool.map(lambda row: _read(store, cache, project, row, scratch), rows))


def _within(rows: list, budget: int) -> tuple[list, bool]:
    """The first rows whose sizes stay within ``budget`` (always the first one), and whether rows were cut."""
    total = 0
    for index, row in enumerate(rows):
        total += row.size
        if index and total > budget:
            return rows[:index], True
    return rows, False


# What is in use


def _held(project_id: int, hashes: list[str] | None = None, after: str | None = None, limit: int | None = None):
    blobs = tables.blobs
    query = select(blobs.c.sha256, blobs.c.kind, blobs.c.size).where(
        blobs.c.project_id == project_id, blobs.c.kind.in_(KINDS)
    )
    if hashes is not None:
        query = query.where(blob_gc.hashes_in(blobs.c.sha256, hashes))
    if after is not None:
        query = query.where(blobs.c.sha256 > after)
    return query.order_by(blobs.c.sha256).limit(limit)


async def _project_id(conn: AsyncConnection, name: str) -> int:
    projects = tables.projects
    found = (await conn.execute(select(projects.c.id).where(projects.c.name == name))).scalar_one_or_none()
    if found is None:
        raise HTTPException(404, f"project {name} is not registered on this hub")
    return found


async def _graphs(conn: AsyncConnection, project_id: int) -> tuple[str | None, dict[str, list[int]]]:
    """The artifact of the project's latest build holding one, and the builds pointing at each artifact."""
    builds = tables.kg_builds
    rows = await conn.execute(
        select(builds.c.id, builds.c.artifact_sha256)
        .where(builds.c.project_id == project_id, builds.c.artifact_sha256.is_not(None))
        .order_by(builds.c.id.desc())
    )
    pointing: dict[str, list[int]] = {}
    newest = None
    for build_id, sha256 in rows:
        newest = newest or sha256
        pointing.setdefault(sha256, []).append(build_id)
    return newest, {sha: sorted(ids) for sha, ids in pointing.items()}


_live_cache: dict[int, tuple[tuple, frozenset[str]]] = {}


class RunLogMissing(LookupError):
    """A run log that tells which blobs are live is not in the blob store: no blob of the project can be called old."""


async def _runs(conn: AsyncConnection, project_id: int) -> tuple[tuple, list, set[str]]:
    """The fingerprint of the project's runs, its ingested runs by source and newest first, and the blobs of its
    pending runs."""
    ingests, pending = tables.kg_ingests, tables.kg_pending_runs
    rows = (
        await conn.execute(
            select(ingests.c.run_id, ingests.c.source, ingests.c.log_sha256, ingests.c.log_size, ingests.c.received_at)
            .where(ingests.c.project_id == project_id)
            .order_by(ingests.c.source, ingests.c.run_id.desc())
        )
    ).all()
    waiting = (
        await conn.execute(
            select(pending.c.run_id, func.unnest(pending.c.blobs)).where(pending.c.project_id == project_id)
        )
    ).all()
    newest = max((row.received_at for row in rows), default=None)
    fingerprint = (len(rows), newest, len(waiting), tuple(sorted({str(row[0]) for row in waiting})))
    return fingerprint, rows, {row[1] for row in waiting}


def _newest_runs(store: BlobStore, rows: list, scratch: Path) -> set[str]:
    """The blobs of each source's newest full run and of the runs after it (``rows``: by source, newest first)."""
    live: set[str] = set()
    done: set[str] = set()
    scratch.mkdir(parents=True, exist_ok=True)
    for row in rows:
        if row.source in done:
            continue
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            path = Path(tmp) / "run.jsonl.gz"
            with open(path, "wb") as handle:
                found = store.fetch(blob_key(row.log_sha256), handle, KIND_LIMITS["kg-log"])
            if found != (row.log_sha256, row.log_size):
                raise RunLogMissing(f"the log of run {row.run_id} is not in the blob store as the hub recorded it")
            summary = run_summary(path)
        live |= summary.refs
        if summary.full:
            done.add(row.source)
    return live


async def live_blobs(conn: AsyncConnection, store: BlobStore, project_id: int, scratch: Path) -> frozenset[str]:
    """The source blobs of the project that a run in use refers to (see the module), remembered until its runs
    change."""
    fingerprint, rows, waiting = await _runs(conn, project_id)
    cached = _live_cache.get(project_id)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    try:
        live = frozenset(waiting | await asyncio.to_thread(_newest_runs, store, rows, scratch))
    except BlobStoreUnavailable:
        raise HTTPException(503, "the blob store did not answer: the runs in use were not read; try again") from None
    except RunLogMissing as exc:
        raise HTTPException(503, f"{exc}: no source blob can be called old until it is") from None
    _live_cache[project_id] = (fingerprint, live)
    while len(_live_cache) > LIVE_CACHE:
        _live_cache.pop(next(iter(_live_cache)))
    return live


_path_cache: dict[int, tuple[frozenset[str], dict[str, frozenset[str]]]] = {}


async def _logs(engine, project_id: int) -> list:
    """The run id and log (sha256, size) of every run of the project, ingested or pending."""
    ingests, pending = tables.kg_ingests, tables.kg_pending_runs
    rows = []
    async with engine.begin() as conn:
        for runs in (ingests, pending):
            query = select(runs.c.run_id, runs.c.log_sha256, runs.c.log_size).where(runs.c.project_id == project_id)
            rows += (await conn.execute(query)).all()
    return rows


def _log_paths(store: BlobStore, rows: list, scratch: Path, known: dict[str, frozenset[str]]) -> dict:
    """``known`` with the paths the items of the logs of ``rows`` give each blob, read from the bucket."""

    def read(row) -> dict[str, set[str]]:
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            path = Path(tmp) / "run.jsonl.gz"
            with open(path, "wb") as handle:
                found = store.fetch(blob_key(row.log_sha256), handle, KIND_LIMITS["kg-log"])
            if found != (row.log_sha256, row.log_size):
                raise RunLogMissing(f"the log of run {row.run_id} is not in the blob store as the hub recorded it")
            return run_summary(path).paths

    scratch.mkdir(parents=True, exist_ok=True)
    paths = dict(known)
    with ThreadPoolExecutor(max_workers=PARALLEL, thread_name_prefix="kg-scrub-logs") as pool:
        for found in pool.map(read, rows):
            for sha, names in found.items():
                paths[sha] = paths.get(sha, frozenset()) | names
    return paths


async def blob_paths(state, project_id: int) -> dict[str, frozenset[str]]:
    """The paths the items of the project's run logs give each source blob, remembered until its runs change: only
    the logs not read yet are read."""
    rows = await _logs(state.engine, project_id)
    read, known = _path_cache.get(project_id, (frozenset(), {}))
    if not read <= {row.log_sha256 for row in rows}:  # not the runs it was read from: another database
        read, known = frozenset(), {}
    todo = [row for row in rows if row.log_sha256 not in read]
    if not todo:
        return known
    try:
        paths = await asyncio.to_thread(_log_paths, state.blobs, todo, _scratch(state), known)
    except BlobStoreUnavailable:
        raise HTTPException(503, "the blob store did not answer: the run logs were not read; try again") from None
    except RunLogMissing as exc:
        raise HTTPException(503, f"{exc}: no source blob can be read as its file until it is") from None
    _path_cache[project_id] = (read | {row.log_sha256 for row in todo}, paths)
    while len(_path_cache) > LIVE_CACHE:
        _path_cache.pop(next(iter(_path_cache)))
    return paths


def _state(row, newest: str | None, live: frozenset[str]) -> str:
    if row.kind == ARTIFACT_KIND:
        return "newest" if row.sha256 == newest else "old"
    return "live" if row.sha256 in live else "old"


async def _found(conn: AsyncConnection, state, project_id: int, rows: list, read: list) -> list[ScrubFound]:
    """The rows whose object holds a secret, with their state."""
    holding = [(row, secrets) for row, secrets in zip(rows, read, strict=True) if secrets]
    newest, pointing = await _graphs(conn, project_id)
    live: frozenset[str] = frozenset()
    if any(row.kind == SOURCE_KIND for row, _ in holding):
        live = await live_blobs(conn, state.blobs, project_id, _scratch(state))
    return [
        ScrubFound(
            sha256=row.sha256,
            kind=row.kind,
            size=row.size,
            state=_state(row, newest, live),
            secrets=dict(sorted(secrets.items())),
            builds=pointing.get(row.sha256, []),
        )
        for row, secrets in holding
    ]


def _store(state) -> BlobStore:
    if state.blobs is None:
        missing = ", ".join(state.config.blob_store_missing())
        raise HTTPException(503, f"the blob store is not configured on this hub: set {missing}")
    return state.blobs


def _scratch(state) -> Path:
    return Path(state.config.data_dir) / "kg" / "scrub"


async def _read_rows(state, project: str, project_id: int, rows: list) -> list:
    """The secrets of the objects of ``rows`` (``_read``), a source blob read as the files its run logs name."""
    try:
        read = await asyncio.to_thread(_read_all, state.blobs, state.kg_graphs, project, rows, _scratch(state))
    except BlobStoreUnavailable:
        raise HTTPException(503, "the blob store did not answer: nothing was read to the end; try again") from None
    if not any(isinstance(found, str) for found in read):
        return read
    paths = await blob_paths(state, project_id)

    def with_paths() -> list:
        return [
            blob_secrets(found, paths.get(row.sha256, ())) if isinstance(found, str) else found
            for row, found in zip(rows, read, strict=True)
        ]

    return await asyncio.to_thread(with_paths)


async def scan(state, project: str, after: str | None, limit: int) -> ScrubPage:
    """Read one page of the project's graphs and source blobs, and say which hold a secret; changes nothing."""
    _store(state)
    async with state.engine.begin() as conn:
        project_id = await _project_id(conn, project)
        rows = (await conn.execute(_held(project_id, after=after, limit=limit))).all()
    rows, cut = _within(rows, PAGE_BYTES)
    read = await _read_rows(state, project, project_id, rows)
    async with state.engine.begin() as conn:
        found = await _found(conn, state, project_id, rows, read)
    more = cut or len(rows) == limit
    return ScrubPage(
        project=project,
        scanned=sum(1 for secrets in read if secrets is not None),
        scanned_bytes=sum(row.size for row, secrets in zip(rows, read, strict=True) if secrets is not None),
        missing=sum(1 for secrets in read if secrets is None),
        found=found,
        next=rows[-1].sha256 if rows and more else None,
    )


async def _drop(conn: AsyncConnection, project: str, project_id: int, doomed: list[ScrubFound], user: Principal) -> int:
    """Drop the project's references to ``doomed``, record the objects left unreferenced and the audit row, in
    ``conn``'s transaction, which holds the blob lock exclusively; the builds whose graph went."""
    builds, blobs = tables.kg_builds, tables.blobs
    graphs = sorted(f.sha256 for f in doomed if f.kind == ARTIFACT_KIND)
    dropped = 0
    if graphs:
        result = await conn.execute(
            update(builds)
            .values(artifact_sha256=None, artifact_pruned_at=func.now())
            .where(builds.c.project_id == project_id, blob_gc.hashes_in(builds.c.artifact_sha256, graphs))
        )
        dropped = result.rowcount
    hashes = sorted(f.sha256 for f in doomed)
    await conn.execute(
        delete(blobs).where(
            blobs.c.project_id == project_id, blobs.c.kind.in_(KINDS), blob_gc.hashes_in(blobs.c.sha256, hashes)
        )
    )
    await blob_gc.forget(conn, {f.sha256: (f.size, f.kind) for f in doomed})
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.KG_SCRUB,
        target=f"{project} artifacts={len(graphs)} blobs={len(hashes) - len(graphs)}",
        project_id=project_id,
    )
    return dropped


async def purge(state, project: str, hashes: list[str], user: Principal) -> ScrubPurged:
    """Delete the old graphs and source blobs of ``hashes`` that hold a secret; keep the others and say why."""
    store = _store(state)
    requested = sorted(set(hashes))
    async with state.engine.begin() as conn:
        project_id = await _project_id(conn, project)
        rows = (await conn.execute(_held(project_id, hashes=requested))).all()
    kept = [ScrubKept(sha256=sha, reason="not held") for sha in sorted(set(requested) - {r.sha256 for r in rows})]
    read = await _read_rows(state, project, project_id, rows)
    kept += [ScrubKept(sha256=row.sha256, reason="missing") for row, s in zip(rows, read, strict=True) if s is None]
    kept += [ScrubKept(sha256=row.sha256, reason="clean") for row, s in zip(rows, read, strict=True) if s == {}]
    async with state.engine.begin() as conn:
        found = await _found(conn, state, project_id, rows, read)
    kept += [ScrubKept(sha256=f.sha256, reason=f.state) for f in found if f.state != "old"]
    doomed = [f for f in found if f.state == "old"]
    dropped, deleted = 0, blob_gc.Deleted(0, 0, 0)
    if doomed:
        async with state.engine.connect() as conn, conn.begin():
            await blob_gc.lock_exclusive(conn)
            dropped = await _drop(conn, project, project_id, doomed, user)
        for finding in doomed:
            if finding.kind == ARTIFACT_KIND:
                state.kg_graphs.drop(project, finding.sha256)
        try:
            deleted = await blob_gc.delete_pending(state.engine, store)
        except BlobStoreUnavailable:
            raise HTTPException(
                503, "the blob store did not answer: the references are gone and the objects wait for the next prune"
            ) from None
    async with state.engine.begin() as conn:
        pending = (await blob_gc.pending(conn))[0]
    _report(project, doomed, dropped, deleted)
    return ScrubPurged(
        project=project,
        purged=doomed,
        kept=sorted(kept, key=lambda k: k.sha256),
        builds=dropped,
        deleted=deleted.objects,
        deleted_bytes=deleted.bytes,
        pending=pending,
    )


def _report(project: str, doomed: list[ScrubFound], builds: int, deleted: blob_gc.Deleted) -> None:
    """A log line counting what a purge deleted, never a hash."""
    if not doomed:
        return
    kinds = Counter(f.kind for f in doomed)
    log.info(
        "kg secrets purged",
        extra={
            "project": project,
            "graphs": kinds[ARTIFACT_KIND],
            "blobs": kinds[SOURCE_KIND],
            "builds": builds,
            "deleted": deleted.objects,
            "deleted_bytes": deleted.bytes,
        },
    )

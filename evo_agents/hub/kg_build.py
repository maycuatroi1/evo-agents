"""The worker's build of a project's knowledge graph, from the run logs and blobs in the blob store.

A build of project P (job ``hub.kg_build``, under the lock ``kg:P``) runs in the worker's cache, ``<data dir>/kg``:

- ``harness/P/``: a harness.yaml, and the knowledge.yaml and ontology.yaml of P's config (``kg_configs``), so the
  build loads P like any project, and ``evo-agents kg build --verify --project <data dir>/kg/harness/P`` with
  ``EVO_KG_HOME=<data dir>/kg/home`` checks the worker's store by hand.
- ``home/P/``: P's corpus as ``evo_agents.kg.corpus`` lays it out (log/, blobs/, corpus.sqlite), its memo and its
  latest graph.sqlite.

Each run in ``kg_ingests`` that corpus.sqlite does not list yet is fetched (its log, checked against its SHA-256),
merged with the corpus's own merge, and listed; the merge is order-independent, so the runs of several machines in
any order give one state. Then every blob a live item refers to and the cache lacks is fetched, checked, and moved
into place. The graph is built by ``evo_agents.kg.build`` into a fresh store, which leaves WAL mode and replaces
graph.sqlite in one rename: a single file, the artifact. Anything that fails marks the build failed with a message
naming no content, and the worker goes on with the next job.

The artifact's bytes are not reproducible (SQLite pages carry more than the content), so two builds of one corpus
give two files with one content hash. A build whose content hash equals that of the project's latest build still
holding an artifact therefore uploads nothing: it points at that artifact (its SHA-256 and size) and names in
``artifact_reused_from`` the build that uploaded it, once the object is found in the bucket with that size. Otherwise
the file is uploaded, its SHA-256 names it in the bucket and in ``blobs`` (kind kg-graph, no creator: the hub made
it), and the build row gets it with the content hash and the counts. Either way the row is written holding the blob
lock shared (``evo_agents.hub.blob_gc``), so the retention of old artifacts (``evo_agents.hub.kg_prune``) never runs
in between, and an uploaded artifact whose hash waits for deletion is written again before the row names it.

The cache can be deleted at any time: the next build fetches everything again and gets the same content hash.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import yaml

from evo_agents.hub import blob_gc
from evo_agents.hub.blobs import KIND_LIMITS, PARALLEL, BlobStore, BlobStoreUnavailable, blob_key
from evo_agents.hub.db import legacy
from evo_agents.hub.jobs import KG_BUILD, JobQueue, kg_lock
from evo_agents.hub.kg_ingest import ONTOLOGY_FILE, blob_refs
from evo_agents.kg.corpus import Corpus
from evo_agents.kg.project import Project, load_project_at

log = logging.getLogger(__name__)

ARTIFACT_KIND = "kg-graph"  # a built graph in ``blobs``: made by the hub, never uploaded
ERROR_CHARS = 2000  # kg_builds.error holds at most this much
QUEUE_ATTEMPTS = 5
STALLED_AFTER = 120.0  # seconds without a heartbeat from its worker before a running build counts as abandoned
STOPPED = "the worker running this build stopped before it finished; a new build was queued"
CHUNK = 1024 * 1024
HARNESS_YAML = "name: {name}\nknowledge_file: knowledge.yaml\nrepos: []\n"


class BuildFailure(Exception):
    """A build that cannot finish, for a reason that names no content."""


class QueueBusy(Exception):
    """A build could not be queued: the waiting one kept starting in between."""


@dataclass(frozen=True)
class Ingest:
    run_id: str
    log_sha256: str
    log_size: int


@dataclass(frozen=True)
class Built:
    path: Path
    sha256: str
    size: int
    content_hash: str
    nodes: int
    edges: int
    runs: int
    merged: int  # runs merged by this build; the others were in the cache already


@dataclass(frozen=True)
class Artifact:
    """The artifact a build points at: its own upload, or the one of an earlier build with the same content."""

    sha256: str
    size: int
    reused_from: int | None  # the build whose upload it is, when this build uploaded nothing


def kg_root(data_dir: Path) -> Path:
    return data_dir / "kg"


def harness_dir(data_dir: Path, project: str) -> Path:
    return kg_root(data_dir) / "harness" / project


def kg_home_dir(data_dir: Path) -> Path:
    return kg_root(data_dir) / "home"


def _write_if_changed(path: Path, text: str) -> None:
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_config(data_dir: Path, project: str, knowledge: dict, ontology: dict | None) -> Path:
    """The harness directory of ``project`` in the cache, holding its config; its root."""
    root = harness_dir(data_dir, project)
    root.mkdir(parents=True, exist_ok=True)
    _write_if_changed(root / "harness.yaml", HARNESS_YAML.format(name=json.dumps(project)))
    _write_if_changed(root / "knowledge.yaml", yaml.safe_dump(knowledge, allow_unicode=True, sort_keys=True))
    if ontology is not None:
        _write_if_changed(root / ONTOLOGY_FILE, yaml.safe_dump(ontology, allow_unicode=True, sort_keys=True))
    else:
        (root / ONTOLOGY_FILE).unlink(missing_ok=True)
    return root


def worker_project(data_dir: Path, project: str) -> Project:
    """``project`` as the worker's cache holds it, for a build or a check by hand."""
    return load_project_at(harness_dir(data_dir, project), kg_home_dir(data_dir))


def _fetch(store: BlobStore, sha256: str, target: Path, limit: int, what: str) -> None:
    """Put blob ``sha256`` at ``target`` unless it is there; written to a temporary file, checked, then renamed, so
    a file at ``target`` always has the right bytes."""
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
    try:
        with open(tmp, "wb") as handle:
            found = store.fetch(blob_key(sha256), handle, limit)
        if found is None:
            raise BuildFailure(f"{what} {sha256} is not in the blob store")
        if found[0] != sha256:
            raise BuildFailure(f"{what} {sha256} in the blob store does not have that SHA-256")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def _merge(corpus: Corpus, ingest: Ingest) -> None:
    path = corpus.log_dir / f"{ingest.run_id}.jsonl.gz"
    run_id, records = corpus.run_records(path)
    if run_id != ingest.run_id:
        raise BuildFailure(f"the log of run {ingest.run_id} names run {run_id}")
    header = records[0]
    end = next((r for r in reversed(records) if r.get("type") == "run_end"), {})
    with corpus.db:
        corpus.merge(records, run_id)
        corpus.db.execute(
            "INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                header.get("source") or "",
                header.get("connector"),
                None,
                end.get("status") or "unknown",
                header.get("started_at"),
                None,
                json.dumps({"hub": True}),
            ),
        )


def prepare_corpus(store: BlobStore, project: Project, ingests: list[Ingest]) -> int:
    """Bring the cached corpus of ``project`` up to ``ingests``; how many runs were merged now."""
    corpus = project.corpus()
    try:
        merged = {row[0] for row in corpus.db.execute("SELECT run_id FROM runs")}
        new = [ingest for ingest in ingests if ingest.run_id not in merged]

        def fetch_log(ingest: Ingest) -> None:
            target = corpus.log_dir / f"{ingest.run_id}.jsonl.gz"
            _fetch(store, ingest.log_sha256, target, KIND_LIMITS["kg-log"], f"the log of run {ingest.run_id}")

        with ThreadPoolExecutor(max_workers=PARALLEL, thread_name_prefix="kg-fetch") as pool:
            list(pool.map(fetch_log, new))
            for ingest in new:
                _merge(corpus, ingest)
            needed = {ref for record in corpus.items() for ref in blob_refs(record.record)}
            missing = sorted(ref for ref in needed if not corpus.blob_path(f"sha256:{ref}").exists())

            def fetch_blob(ref: str) -> None:
                _fetch(store, ref, corpus.blob_path(f"sha256:{ref}"), KIND_LIMITS["kg-blob"], "blob")

            list(pool.map(fetch_blob, missing))
        return len(new)
    finally:
        corpus.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_graph(project: Project) -> tuple[Path, object]:
    """Build ``project`` into a fresh store and make it its graph.sqlite: one file, out of WAL mode. The path and the
    build report; BuildFailure when the build reports errors."""
    from evo_agents.kg.build import build_project
    from evo_agents.kg.store import Store

    target = project.root / "graph.sqlite"
    stage = Store.staging(target)
    try:
        report = build_project(project, store=stage)
        if not report.ok:
            shown = "; ".join(report.errors[:5])
            raise BuildFailure(f"the build reported {len(report.errors)} error(s): {shown}")
        stage.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        stage.db.execute("PRAGMA journal_mode=DELETE")
    finally:
        stage.close()
    for stale in (Path(f"{target}-wal"), Path(f"{target}-shm")):
        stale.unlink(missing_ok=True)
    os.replace(stage.path, target)
    return target, report


def build_in_cache(store: BlobStore, data_dir: Path, name: str, config: dict, ingests: list[Ingest]) -> Built:
    """Everything a build does on disk: the config, the corpus fetched from the blob store, and the graph, which
    ``record`` uploads unless an earlier build has its content. Blocking."""
    write_config(data_dir, name, config["knowledge"], config.get("ontology"))
    project = worker_project(data_dir, name)
    merged = prepare_corpus(store, project, ingests)
    path, report = build_graph(project)
    sha256 = _sha256(path)
    return Built(
        path, sha256, path.stat().st_size, report.content_hash, report.nodes, report.edges, len(ingests), merged
    )


def describe_failure(exc: BaseException) -> str:
    """What kg_builds.error says about ``exc``: its own message for the failures this module names, the type and a
    short message otherwise."""
    if isinstance(exc, (BuildFailure, BlobStoreUnavailable)):
        text = str(exc)
    else:
        text = f"{type(exc).__name__}: {exc}"
    return (text or type(exc).__name__)[:ERROR_CHARS]


START = """
UPDATE kg_builds SET status = 'running', started_at = now(), job_id = coalesce(job_id, %s)
 WHERE id = %s AND status IN ('queued', 'running')
RETURNING id
"""
START_UNQUEUED = """
INSERT INTO kg_builds (project_id, job_id, status, started_at) VALUES (%s, %s, 'running', now()) RETURNING id
"""
SUCCEEDED = """
UPDATE kg_builds SET status = 'succeeded', config_digest = %s, runs = %s, artifact_sha256 = %s, artifact_size = %s,
       artifact_reused_from = %s, content_hash = %s, nodes = %s, edges = %s, error = NULL, finished_at = now()
 WHERE id = %s
"""
LATEST_ARTIFACT = """
SELECT id, artifact_sha256, artifact_size, content_hash, artifact_reused_from FROM kg_builds
 WHERE project_id = %s AND artifact_sha256 IS NOT NULL ORDER BY id DESC LIMIT 1
"""
FAILED = """
UPDATE kg_builds SET status = 'failed', config_digest = %s, runs = %s, error = %s, finished_at = now() WHERE id = %s
"""
ARTIFACT = """
INSERT INTO blobs (project_id, sha256, size, kind, created_by) VALUES (%s, %s, %s, %s, NULL)
    ON CONFLICT (project_id, sha256) DO NOTHING
"""


async def _reusable(engine, store: BlobStore, project_id: int, built: Built) -> tuple | None:
    """The latest build of the project still holding an artifact, when it has ``built``'s content and its object is
    in the bucket with its size; None otherwise."""
    async with engine.begin() as conn:
        latest = await (await legacy(conn, LATEST_ARTIFACT, (project_id,))).fetchone()
    if latest is None or latest[3] != built.content_hash:
        return None
    if await asyncio.to_thread(store.size, blob_key(latest[1])) != latest[2]:
        log.warning("the artifact of the latest build is not in the blob store as recorded; uploading a new one")
        return None
    return latest


async def record(
    engine, store: BlobStore, project_id: int, build_id: int, digest: str | None, built: Built
) -> Artifact:
    """Mark build ``build_id`` succeeded with ``built``, pointing at the artifact of the project's latest build that
    has the same content, or at ``built``'s file, uploaded now (see the module). The artifact it points at."""
    latest = await _reusable(engine, store, project_id, built)
    if latest is None:
        await asyncio.to_thread(store.put_file, built.sha256, built.path)
    async with engine.begin() as conn:
        await blob_gc.lock_shared(conn)
        if latest is not None and await (await legacy(conn, LATEST_ARTIFACT, (project_id,))).fetchone() == latest:
            artifact = Artifact(latest[1], latest[2], latest[4] or latest[0])
        else:
            # Uploaded above, unless another build of the project finished meanwhile, which the queue's lock rules
            # out. A key on its way out of the bucket gets the bytes again, while the lock keeps the deletion out.
            revived = await blob_gc.revive(conn, [built.sha256])
            if latest is not None or revived:
                await asyncio.to_thread(store.put_file, built.sha256, built.path, replace=bool(revived))
            artifact = Artifact(built.sha256, built.size, None)
        await legacy(conn, ARTIFACT, (project_id, artifact.sha256, artifact.size, ARTIFACT_KIND))
        await legacy(
            conn,
            SUCCEEDED,
            (
                digest,
                built.runs,
                artifact.sha256,
                artifact.size,
                artifact.reused_from,
                built.content_hash,
                built.nodes,
                built.edges,
                build_id,
            ),
        )
    return artifact


async def _failed(engine, digest: str | None, runs: int, error: str, build_id: int) -> None:
    async with engine.begin() as conn:
        await legacy(conn, FAILED, (digest, runs, error, build_id))


async def _stopped(
    engine, digest: str | None, runs: int, build_id: int, project_id: int, project: str, manager
) -> None:
    """Record that the worker stopped before build ``build_id`` finished, and queue another so the runs it would
    have built are not left waiting for the next push."""
    async with engine.begin() as conn:
        await legacy(conn, FAILED, (digest, runs, STOPPED if manager else STOPPED.split(";")[0], build_id))
        if manager is not None:
            await queue_build(JobQueue(manager), conn, project_id, project, None)


async def run_build(context, project: str, build_id: int | None, job_id: int | None, manager=None) -> dict:
    """The job: build ``project`` and record the outcome on build ``build_id`` (a new row when it is None or gone).
    ``context`` is the worker's HubContext; ``manager``, procrastinate's job manager, queues the build again when
    the worker stops before this one finished."""
    engine = context.engine
    async with engine.begin() as conn:
        row = await (await legacy(conn, "SELECT id FROM projects WHERE name = %s", (project,))).fetchone()
        if row is None:
            log.warning("kg build of a project the hub does not have", extra={"project": project, "job_id": job_id})
            return {"status": "skipped"}
        project_id = row[0]
        started = None
        if build_id is not None:
            started = await (await legacy(conn, START, (job_id, build_id))).fetchone()
        if started is None:
            started = await (await legacy(conn, START_UNQUEUED, (project_id, job_id))).fetchone()
        build_id = started[0]
        found = await (
            await legacy(
                conn, "SELECT digest, knowledge, ontology FROM kg_configs WHERE project_id = %s", (project_id,)
            )
        ).fetchone()
        cursor = await legacy(
            conn,
            "SELECT run_id::text, log_sha256, log_size FROM kg_ingests WHERE project_id = %s ORDER BY run_id",
            (project_id,),
        )
        ingests = [Ingest(*row) for row in await cursor.fetchall()]
    digest = found[0] if found else None
    log.info("kg build started", extra={"project": project, "build_id": build_id, "job_id": job_id})
    try:
        if found is None:
            raise BuildFailure(
                f"project {project} has no knowledge config on the hub yet: run `evo-agents hub kg push`"
            )
        config = {"knowledge": found[1], "ontology": found[2]}
        built = await asyncio.to_thread(build_in_cache, context.blobs, context.data_dir, project, config, ingests)
        artifact = await record(engine, context.blobs, project_id, build_id, digest, built)
    except asyncio.CancelledError:  # the worker is stopping and gave up waiting: say so, then let it stop
        await asyncio.shield(_stopped(engine, digest, len(ingests), build_id, project_id, project, manager))
        log.warning("kg build stopped with the worker", extra={"project": project, "build_id": build_id})
        raise
    except Exception as exc:
        error = describe_failure(exc)
        await _failed(engine, digest, len(ingests), error, build_id)
        log.error(
            "kg build failed",
            extra={"project": project, "build_id": build_id, "job_id": job_id, "error": error[:300]},
        )
        return {"status": "failed", "build_id": build_id}
    log.info(
        "kg build succeeded",
        extra={
            "project": project,
            "build_id": build_id,
            "job_id": job_id,
            "runs": built.runs,
            "merged": built.merged,
            "nodes": built.nodes,
            "edges": built.edges,
            "content_hash": built.content_hash,
            "artifact_bytes": artifact.size,
            "artifact_reused_from": artifact.reused_from,
        },
    )
    return {
        "status": "succeeded",
        "build_id": build_id,
        "content_hash": built.content_hash,
        "artifact_reused_from": artifact.reused_from,
    }


# Queueing


async def queue_build(
    job_queue: JobQueue, conn, project_id: int, project: str, requested_by: int | None
) -> tuple[int | None, bool]:
    """Queue a build of ``project`` in ``conn``'s transaction: (its build id, True), or (the waiting build's id,
    False) when one waits already. The waiting job's row stays locked until the transaction ends, and the worker
    takes jobs with SKIP LOCKED, so it cannot start that build before the caller's rows are visible to it."""
    key = kg_lock(project)
    for _ in range(QUEUE_ATTEMPTS):
        (build_id,) = await (
            await legacy(
                conn,
                "INSERT INTO kg_builds (project_id, status, requested_by) VALUES (%s, 'queued', %s) RETURNING id",
                (project_id, requested_by),
            )
        ).fetchone()
        job_id = await job_queue.defer_kg(KG_BUILD, project, connection=conn, project=project, build_id=build_id)
        if job_id is not None:
            await legacy(conn, "UPDATE kg_builds SET job_id = %s WHERE id = %s", (job_id, build_id))
            return build_id, True
        await legacy(conn, "DELETE FROM kg_builds WHERE id = %s", (build_id,))
        waiting = await (
            await legacy(
                conn,
                "SELECT j.id, b.id FROM procrastinate_jobs j LEFT JOIN kg_builds b ON b.job_id = j.id "
                "WHERE j.queueing_lock = %s AND j.status = 'todo' FOR UPDATE OF j",
                (key,),
            )
        ).fetchone()
        if waiting is not None:
            return waiting[1], False
        # The waiting job started in between: queue another, which will see this transaction's rows.
    raise QueueBusy(f"a build of project {project} could not be queued; try again shortly")


async def recover_stalled(context, manager) -> dict:
    """Fail the kg builds whose worker stopped sending heartbeats (killed, or its machine lost), which would hold
    their project's lock for ever, and queue a new build of each such project."""
    from procrastinate.jobs import Status

    stalled = await manager.get_stalled_jobs(task_name=KG_BUILD, seconds_since_heartbeat=STALLED_AFTER)
    projects = set()
    for job in stalled:
        await manager.finish_job(job, Status.FAILED, delete_job=False)
        async with context.engine.begin() as conn:
            await legacy(
                conn,
                "UPDATE kg_builds SET status = 'failed', error = %s, started_at = coalesce(started_at, now()), "
                "finished_at = now() WHERE job_id = %s AND status IN ('queued', 'running')",
                (STOPPED, job.id),
            )
        projects.add(job.task_kwargs.get("project"))
    queued = []
    for project in sorted(p for p in projects if isinstance(p, str)):
        async with context.engine.begin() as conn:
            row = await (await legacy(conn, "SELECT id FROM projects WHERE name = %s", (project,))).fetchone()
            if row is not None:
                await queue_build(JobQueue(manager), conn, row[0], project, None)
                queued.append(project)
    if stalled:
        log.warning("abandoned kg builds failed and queued again", extra={"jobs": len(stalled), "projects": queued})
    return {"failed": len(stalled), "queued": queued}

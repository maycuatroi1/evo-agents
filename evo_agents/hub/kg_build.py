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
from sqlalchemy import BigInteger, Text, cast, column, delete, func, insert, literal, select, table, update
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import blob_gc, tables
from evo_agents.hub.blobs import KIND_LIMITS, PARALLEL, BlobStore, BlobStoreUnavailable, blob_key
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

# procrastinate's job table, which the hub reads and locks but never writes: the columns the kg queries use. The
# status is procrastinate's enum, so a value compared with it is sent untyped and Postgres reads it as the enum.
JOB_STATUS = ENUM(
    "todo",
    "doing",
    "succeeded",
    "failed",
    "cancelled",
    "aborting",
    "aborted",
    name="procrastinate_job_status",
    create_type=False,
)
procrastinate_jobs = table(
    "procrastinate_jobs", column("id"), column("status", JOB_STATUS), column("lock"), column("queueing_lock")
)


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
        corpus.record_run(
            run_id,
            source=header.get("source") or "",
            connector=header.get("connector"),
            status=end.get("status") or "unknown",
            started_at=header.get("started_at"),
            detail={"hub": True},
        )


def prepare_corpus(store: BlobStore, project: Project, ingests: list[Ingest]) -> int:
    """Bring the cached corpus of ``project`` up to ``ingests``; how many runs were merged now."""
    corpus = project.corpus()
    try:
        merged = corpus.run_ids()
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
        stage.leave_wal()
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


def _start(build_id: int, job_id: int | None):
    """Mark build ``build_id`` running under job ``job_id``, unless it ended already; its id, when it did not."""
    builds = tables.kg_builds
    return (
        update(builds)
        .values(
            status="running",
            started_at=func.now(),
            job_id=func.coalesce(builds.c.job_id, literal(job_id, BigInteger)),
        )
        .where(builds.c.id == build_id, builds.c.status.in_(["queued", "running"]))
        .returning(builds.c.id)
    )


def _start_unqueued(project_id: int, job_id: int | None):
    """A new build of the project, running under job ``job_id``: for a job whose build row is gone."""
    builds = tables.kg_builds
    return (
        insert(builds)
        .values(project_id=project_id, job_id=job_id, status="running", started_at=func.now())
        .returning(builds.c.id)
    )


def _latest_artifact(project_id: int):
    """The project's latest build still holding an artifact: id, artifact_sha256, artifact_size, content_hash and
    artifact_reused_from."""
    builds = tables.kg_builds
    return (
        select(
            builds.c.id,
            builds.c.artifact_sha256,
            builds.c.artifact_size,
            builds.c.content_hash,
            builds.c.artifact_reused_from,
        )
        .where(builds.c.project_id == project_id, builds.c.artifact_sha256.is_not(None))
        .order_by(builds.c.id.desc())
        .limit(1)
    )


def _failed_build(build_id: int, digest: str | None, runs: int, error: str):
    builds = tables.kg_builds
    return (
        update(builds)
        .values(status="failed", config_digest=digest, runs=runs, error=error, finished_at=func.now())
        .where(builds.c.id == build_id)
    )


def _project_id(name: str):
    projects = tables.projects
    return select(projects.c.id).where(projects.c.name == name)


async def _reusable(engine, store: BlobStore, project_id: int, built: Built) -> tuple | None:
    """The latest build of the project still holding an artifact, when it has ``built``'s content and its object is
    in the bucket with its size; None otherwise."""
    async with engine.begin() as conn:
        latest = (await conn.execute(_latest_artifact(project_id))).one_or_none()
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
        if latest is not None and (await conn.execute(_latest_artifact(project_id))).one_or_none() == latest:
            artifact = Artifact(latest[1], latest[2], latest[4] or latest[0])
        else:
            # Uploaded above, unless another build of the project finished meanwhile, which the queue's lock rules
            # out. A key on its way out of the bucket gets the bytes again, while the lock keeps the deletion out.
            revived = await blob_gc.revive(conn, [built.sha256])
            if latest is not None or revived:
                await asyncio.to_thread(store.put_file, built.sha256, built.path, replace=bool(revived))
            artifact = Artifact(built.sha256, built.size, None)
        blobs, builds = tables.blobs, tables.kg_builds
        await conn.execute(
            pg_insert(blobs)
            .values(
                project_id=project_id, sha256=artifact.sha256, size=artifact.size, kind=ARTIFACT_KIND, created_by=None
            )
            .on_conflict_do_nothing(index_elements=[blobs.c.project_id, blobs.c.sha256])
        )
        await conn.execute(
            update(builds)
            .values(
                status="succeeded",
                config_digest=digest,
                runs=built.runs,
                artifact_sha256=artifact.sha256,
                artifact_size=artifact.size,
                artifact_reused_from=artifact.reused_from,
                content_hash=built.content_hash,
                nodes=built.nodes,
                edges=built.edges,
                error=None,
                finished_at=func.now(),
            )
            .where(builds.c.id == build_id)
        )
    return artifact


async def _failed(engine, digest: str | None, runs: int, error: str, build_id: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(_failed_build(build_id, digest, runs, error))


async def _stopped(
    engine, digest: str | None, runs: int, build_id: int, project_id: int, project: str, manager
) -> None:
    """Record that the worker stopped before build ``build_id`` finished, and queue another so the runs it would
    have built are not left waiting for the next push."""
    async with engine.begin() as conn:
        await conn.execute(_failed_build(build_id, digest, runs, STOPPED if manager else STOPPED.split(";")[0]))
        if manager is not None:
            await queue_build(JobQueue(manager), conn, project_id, project, None)


async def run_build(context, project: str, build_id: int | None, job_id: int | None, manager=None) -> dict:
    """The job: build ``project`` and record the outcome on build ``build_id`` (a new row when it is None or gone).
    ``context`` is the worker's HubContext; ``manager``, procrastinate's job manager, queues the build again when
    the worker stops before this one finished."""
    engine = context.engine
    configs, kg_ingests = tables.kg_configs, tables.kg_ingests
    async with engine.begin() as conn:
        row = (await conn.execute(_project_id(project))).one_or_none()
        if row is None:
            log.warning("kg build of a project the hub does not have", extra={"project": project, "job_id": job_id})
            return {"status": "skipped"}
        project_id = row[0]
        started = None
        if build_id is not None:
            started = (await conn.execute(_start(build_id, job_id))).one_or_none()
        if started is None:
            started = (await conn.execute(_start_unqueued(project_id, job_id))).one()
        build_id = started[0]
        found = (
            await conn.execute(
                select(configs.c.digest, configs.c.knowledge, configs.c.ontology).where(
                    configs.c.project_id == project_id
                )
            )
        ).one_or_none()
        run_id = cast(kg_ingests.c.run_id, Text).label("run_id")
        rows = await conn.execute(
            select(run_id, kg_ingests.c.log_sha256, kg_ingests.c.log_size)
            .where(kg_ingests.c.project_id == project_id)
            .order_by(run_id)
        )
        ingests = [Ingest(**row._mapping) for row in rows]
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
    job_queue: JobQueue, conn: AsyncConnection, project_id: int, project: str, requested_by: int | None
) -> tuple[int | None, bool]:
    """Queue a build of ``project`` in ``conn``'s transaction: (its build id, True), or (the waiting build's id,
    False) when one waits already. The waiting job's row stays locked until the transaction ends, and the worker
    takes jobs with SKIP LOCKED, so it cannot start that build before the caller's rows are visible to it."""
    key = kg_lock(project)
    builds, jobs = tables.kg_builds, procrastinate_jobs
    for _ in range(QUEUE_ATTEMPTS):
        build_id = (
            await conn.execute(
                insert(builds)
                .values(project_id=project_id, status="queued", requested_by=requested_by)
                .returning(builds.c.id)
            )
        ).scalar_one()
        job_id = await job_queue.defer_kg(KG_BUILD, project, connection=conn, project=project, build_id=build_id)
        if job_id is not None:
            await conn.execute(update(builds).values(job_id=job_id).where(builds.c.id == build_id))
            return build_id, True
        await conn.execute(delete(builds).where(builds.c.id == build_id))
        waiting = (
            await conn.execute(
                select(jobs.c.id.label("job_id"), builds.c.id.label("build_id"))
                .join_from(jobs, builds, builds.c.job_id == jobs.c.id, isouter=True)
                .where(jobs.c.queueing_lock == key, jobs.c.status == "todo")
                .with_for_update(of=jobs)
            )
        ).first()
        if waiting is not None:
            return waiting.build_id, False
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
        builds = tables.kg_builds
        async with context.engine.begin() as conn:
            await conn.execute(
                update(builds)
                .values(
                    status="failed",
                    error=STOPPED,
                    started_at=func.coalesce(builds.c.started_at, func.now()),
                    finished_at=func.now(),
                )
                .where(builds.c.job_id == job.id, builds.c.status.in_(["queued", "running"]))
            )
        projects.add(job.task_kwargs.get("project"))
    queued = []
    for project in sorted(p for p in projects if isinstance(p, str)):
        async with context.engine.begin() as conn:
            row = (await conn.execute(_project_id(project))).one_or_none()
            if row is not None:
                await queue_build(JobQueue(manager), conn, row[0], project, None)
                queued.append(project)
    if stalled:
        log.warning("abandoned kg builds failed and queued again", extra={"jobs": len(stalled), "projects": queued})
    return {"failed": len(stalled), "queued": queued}

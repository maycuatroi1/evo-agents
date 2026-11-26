"""The worker's build of a project's knowledge graph, from the run logs and blobs in the blob store.

A build of project P (job ``hub.kg_build``, under the lock ``kg:P``) runs in the worker's cache, ``<data dir>/kg``:

- ``harness/P/``: a harness.yaml, and the knowledge.yaml and ontology.yaml of P's config (``kg_configs``), so the
  build loads P like any project, and ``evo_agents.kg.build.verify_rows`` on that harness, with
  ``EVO_KG_HOME=<data dir>/kg/home``, checks a build's content hash by hand.
- ``home/P/``: P's corpus as ``evo_agents.kg.corpus`` lays it out (log/, blobs/, state.json) and its memo.

The sources of the core plugins the project enables (``evo_agents.hub.kg_sources``) are read from the hub's database
first: the config written into the cache names them and their identifiers, and their items and cursors in the corpus
become those they give now, written only when something changed. Each run in ``kg_ingests`` that the corpus does not list yet
is fetched (its log, checked against its SHA-256), merged with the corpus's own merge, and listed; the merge is
order-independent, so the runs of several machines in any order give one state; the cursor a run ended with is kept
unless a later run's is, so a git source's coverage names the commit its newest run read (``evo_agents.kg.freshness``).
A merge cannot be undone, so a corpus holding a run merged from the hub that kg_ingests no longer lists (its source
was taken off the hub) is started again first, its blobs kept (``drop_forgotten``).
Then every blob a live item refers to and the cache lacks is fetched, checked, and moved into place. The graph is
built in memory by ``evo_agents.kg.build.build_rows``, into the rows of its build (``evo_agents.kg.graph_rows``),
its clustering started from the clusters of the graph the hub keeps; no graph file is written. Anything that fails
marks the build failed with a message naming no content, and the worker goes on with the next job.

A build whose corpus state and inputs (the knowledge config, the code, the cursors) are those the graph the hub keeps
was built from builds nothing (``skipped``): it takes those rows, whose content hash is the latest build's. Most pushes
carry runs that changed nothing, and such a build takes seconds, not minutes.

A build uploads no graph file: the graph's rows go to the hub's tables (``keep_graph``, ``evo_agents.hub.kg_graph_rows``),
which the api's snapshots read, and the build row gets the content hash and the counts, its ``artifact_*`` columns
null. The kg-graph blobs older builds uploaded stay until the retention (``evo_agents.hub.kg_prune``) deletes them. The
row is written holding the blob lock shared (``evo_agents.hub.blob_gc``), as the entity requests it holds are blobs.

A build that succeeds also keeps what it covers (``kg_builds.coverage``, ``evo_agents.hub.kg_coverage.build_coverage``:
mentions resolved, ambiguous and dangling per source, the commit each git source read, the Python functions and
methods no call edge reaches), and the files each git source holds in it, path and blob SHA (kg_source_files, one row a
source, replaced by each build), which the measure of the graph's health compares with the default branches
(``evo_agents.hub.server.kg_health``).

An entities step that runs Claude Code calls nothing here, since the container has no ``claude``: the build defers
the items its memo lacks, keeps their requests as a blob the hub made (kind kg-entity-requests, named in the coverage),
and reads the answers a run of the project's agent pool posted (kind kg-entity-answers) as the step's inbox
(``evo_agents.hub.kg_entity_relay``).

The cache can be deleted at any time: the next build fetches everything again and gets the same content hash.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from sqlalchemy import BigInteger, Text, cast, delete, func, insert, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import (
    blob_gc,
    kg_asked,
    kg_branches,
    kg_coverage,
    kg_entity_map_db,
    kg_entity_maps,
    kg_entity_relay,
    kg_eval_starter,
    kg_graph_rows,
    kg_init_db,
    kg_semantic,
    kg_settings,
    kg_sources,
    kg_upload_sources,
    tables,
)
from evo_agents.hub.blobs import KIND_LIMITS, PARALLEL, BlobStore, BlobStoreUnavailable, blob_key
from evo_agents.hub.heap import give_back
from evo_agents.hub.jobs import JobQueue
from evo_agents.hub.kg_build_queue import (  # noqa: F401 (where its callers import them, as before)
    HALTED_NOTICE,
    JOB_STATUS,
    ORPHANED,
    REPLACED,
    STOPPED,
    QueueBusy,
    no_graph_reason,
    procrastinate_jobs,
    project_id_query,
    queue_build,
    queue_if_configured,
    recover_stalled,
)
from evo_agents.hub.kg_ingest import ONTOLOGY_FILE, blob_refs
from evo_agents.hub.kg_source_state import fill_run_facts
from evo_agents.kg import graph_rows, settings
from evo_agents.kg import strategy as kg_strategy
from evo_agents.kg.corpus import Corpus
from evo_agents.kg.project import Project, load_project_at
from evo_agents.kg.protocol import sha256 as sha256_of
from evo_agents.kg.snapshot import StoreRows

if TYPE_CHECKING:
    from evo_agents.kg.build import BuildReport

log = logging.getLogger(__name__)

ARTIFACT_KIND = "kg-graph"  # a graph file an older build put in ``blobs``: made by the hub, never uploaded
ERROR_CHARS = 2000  # kg_builds.error holds at most this much
HARNESS_YAML = "name: {name}\nknowledge_file: knowledge.yaml\nrepos: []\n"


class BuildFailure(Exception):
    """A build that cannot finish, for a reason that names no content."""


@dataclass(frozen=True)
class Ingest:
    run_id: str
    log_sha256: str
    log_size: int


@dataclass(frozen=True)
class Built:
    rows: StoreRows  # the graph's rows, with its asked index and vectors (``evo_agents.kg.graph_rows``)
    content_hash: str
    nodes: int
    edges: int
    runs: int
    merged: int  # runs merged by this build; the others were in the cache already
    skipped: bool = False  # the cache's graph was current: nothing was built
    coverage: dict | None = None  # what kg_builds.coverage keeps (``kg_coverage.build_coverage``)
    files: dict | None = None  # the files of each git source (``kg_coverage.source_files``)
    requests: tuple | None = None  # the deferred entity requests: path, sha256, size, count (``kg_entity_relay``)
    items: tuple = ()  # the live items of its corpus, as kg_corpus_items keeps them (``kg_graph_rows``)
    read: str | None = None  # a skipped build's: the content hash of the head of main it took its rows from


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


def knowledge_and_ontology(config: dict, sources: list, tuning: settings.Tuning | None) -> tuple[dict, dict | None]:
    """The knowledge.yaml and the ontology a build of ``config`` reads: the plugins' ``sources`` added, and the node
    kinds and relations of the strategy the project's settings name added to its ontology (``kg.strategy``). A strategy
    set on the hub (``hub kg settings --strategy``, the Entities view's button) changes no file of the harness, so
    without this an entities step whose relations the harness's ontology lacks would drop every relation."""
    knowledge = kg_sources.knowledge_with(config["knowledge"], sources)
    ontology = kg_strategy.with_ontology(tuning.strategy if tuning else None, config.get("ontology"))
    if ontology is not None and knowledge.get("ontology") != ONTOLOGY_FILE:
        knowledge = {**knowledge, "ontology": ONTOLOGY_FILE}
    return knowledge, ontology


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
    state = next((r for r in reversed(records) if r.get("type") == "state"), None)
    with corpus.transaction():
        corpus.merge(records, run_id)
        if state is not None and header.get("source"):  # the commit a git source read, for the build's coverage
            corpus.record_cursor(header["source"], state.get("cursor"), run_id)
        corpus.record_run(
            run_id,
            source=header.get("source") or "",
            connector=header.get("connector"),
            status=end.get("status") or "unknown",
            started_at=header.get("started_at"),
            detail={"hub": True},
        )


def drop_forgotten(project: Project, ingests: list[Ingest]) -> int:
    """Start the cached corpus of ``project`` again when it holds a run merged from the hub that ``ingests`` no longer
    lists (a source taken off the hub, ``evo-agents hub kg forget-source``): the merge cannot take a run back, and
    the items of a source the config still declares would stay in every build. The blobs stay; the next steps write
    the plugins' sources and fetch the logs again. How many such runs there were."""
    corpus = project.corpus()
    try:
        merged = corpus.hub_run_ids()
    finally:
        corpus.close()
    forgotten = merged - {ingest.run_id for ingest in ingests}
    if forgotten:
        corpus.state_path.unlink(missing_ok=True)
        for path in corpus.log_dir.iterdir():
            path.unlink(missing_ok=True)
        log.info(
            "kg cache started again: runs taken off the hub", extra={"project": project.name, "runs": len(forgotten)}
        )
    return len(forgotten)


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
            with corpus.transaction():  # the state file written once, not once a run
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


def build_graph(
    project: Project,
    *,
    previous: StoreRows | None = None,
    entity_inbox: dict | None = None,
    entity_outbox: list | None = None,
    entity_keys: dict | None = None,
    whole: Callable[[], StoreRows | None] | None = None,
) -> tuple[StoreRows, BuildReport]:
    """Build ``project`` in memory (``evo_agents.kg.build.build_rows``): the rows of its graph and the build report;
    BuildFailure when the build reports errors. ``previous``, the rows of the graph the hub keeps: when they were
    built from the corpus state and the inputs ``project`` has now (``unchanged_rows``), nothing is built, and they
    come back with a report of them, ``skipped``; otherwise the clustering starts from their clusters. ``whole``,
    when ``previous`` holds only the head and the clusters (``kg_graph_rows.read_head``), reads all their rows, for
    a skipped build only: held during a build, they took as much memory as the build's own rows. ``entity_inbox``,
    ``entity_outbox`` and ``entity_keys`` go to the entities step (``evo_agents.kg.entities.run``)."""
    from evo_agents.kg.build import build_rows, unchanged_rows
    from evo_agents.kg.snapshot import rows_clusters

    current = unchanged_rows(project, previous)
    if current is not None and previous is not None:
        kept = whole() if whole is not None else previous
        if kept is not None and kept.content_hash == previous.content_hash:
            return kept, current
    report, rows = build_rows(
        project,
        warm=rows_clusters(previous.clusters) if previous is not None else None,
        entity_outbox=entity_outbox,
        entity_inbox=entity_inbox,
        entity_keys=entity_keys,
    )
    if not report.ok or rows is None:
        shown = "; ".join(report.errors[:5])
        raise BuildFailure(f"the build reported {len(report.errors)} error(s): {shown}")
    return rows, report


def put_tuning(root: Path, tuning: settings.Tuning) -> None:
    """The project's overrides as the settings.json a build reads (none: no file)."""
    rest = {k: v for k, v in tuning.overrides().items() if k not in settings.SEMANTIC_KEYS}
    path = root / settings.SETTINGS_FILE
    if rest:
        path.write_text(json.dumps(rest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


def build_in_cache(
    store: BlobStore,
    data_dir: Path,
    name: str,
    config: dict,
    ingests: list[Ingest],
    sources: list | None = None,
    learned: kg_asked.Learned | None = None,
    semantic: kg_semantic.semantic.Settings | None = None,
    tuning: settings.Tuning | None = None,
    answers: list[str] | None = None,
    uploads: list[kg_upload_sources.Held] | None = None,
    keys: dict | None = None,
    maps: list[str] | None = None,
    vector_cache: kg_semantic.HubCache | None = None,
    previous: StoreRows | None = None,
    whole: Callable[[], StoreRows | None] | None = None,
) -> Built:
    """Everything a build does on disk: the config with the plugins' ``sources``, their items and the corpus fetched
    from the blob store, the graph, whose rows ``record`` keeps in the hub's tables, and what the
    build covers with the files of each git source, which ``record`` keeps. With ``learned``, the graph gets the
    asked index of the project's searches (``kg_asked``), and its content hash folds in the index's digest, so a
    build whose graph is the same but whose index is not is a new graph. With ``semantic`` settings that ask for
    them, the graph gets the vectors of its rows (``kg_semantic``), embedded only when ``vector_cache``, the project's
    in the hub's database (seeded once from its vectors.sqlite and graph.sqlite), else a cache of this build only,
    lacks them, and the content hash folds in their digest and the mode, the same way. With ``tuning``, the project's
    overrides are put in its settings.json first, so the build reads them as a local one does. ``answers``, the hashes
    of the project's kg-entity-answers blobs, are the entities step's inbox, and the requests it defers are written
    for ``record`` to keep (``kg_entity_relay``). ``uploads``: the newest upload read of each upload source, whose
    items are fetched and held as a plugin's (``kg_upload_sources``). ``keys``: the key of the entities step's url
    model, in memory (``evo_agents.hub.server.kg_entity_key``). ``maps``: the hashes of the project's kg-entity-map
    blobs, put where the entity_map step reads them (``kg_entity_maps``). ``previous``: the rows of the graph the hub
    keeps, which a build of the same corpus state and inputs takes again, and whose clusters a build starts from
    (``build_graph``), with ``whole`` when they are only its head. Blocking."""
    give_back()  # what the worker's last build and the tables it wrote still held, before this one
    uploads = uploads or []
    sources = (sources or []) + kg_upload_sources.load_all(store, data_dir, uploads, config["knowledge"], name)
    write_config(data_dir, name, *knowledge_and_ontology(config, sources, tuning))
    project = worker_project(data_dir, name)
    drop_forgotten(project, ingests)
    kg_sources.keep_sources(project, sources)
    merged = prepare_corpus(store, project, ingests)
    if tuning is not None:
        put_tuning(project.root, tuning)
        semantic = tuning.semantic_settings()
    kg_entity_maps.write_maps(store, project.root, maps or [])
    if vector_cache is not None and semantic is not None and semantic.indexes:
        vector_cache.seed(project.root, semantic.model)  # before the build replaces the graph that holds them
    inbox = kg_entity_relay.read_answers(store, answers) if answers else {}
    outbox: list[dict] = []
    rows, report = build_graph(
        project, previous=previous, entity_inbox=inbox, entity_outbox=outbox, entity_keys=keys, whole=whole
    )
    give_back()  # what the build freed, before the model of its vectors loads on top of its rows
    requests = kg_entity_relay.write_requests(project.root, outbox) if not report.skipped else None
    asked = None
    if learned is not None:
        rows, asked = kg_asked.put(rows, learned)
    graph_hash = str(rows.summary["content_hash"])  # the graph's own, without the asked index and the vectors
    content_hash = graph_hash if asked is None else sha256_of(f"{graph_hash}\n{asked}")
    vector_coverage = None
    if semantic is not None:
        aside = None
        if semantic.indexes:  # the model loads: what it does not read is held packed under it
            rows, aside = kg_semantic.set_aside(rows)
            give_back()
        rows, vectors = kg_semantic.index(rows, semantic, vector_cache)
        if aside is not None:
            rows = kg_semantic.taken_back(rows, aside)
            del aside
        if vectors.error:
            log.warning("kg build without vectors", extra={"project": name, "error": vectors.error})
        if vectors.digest is not None:
            content_hash = sha256_of(f"{content_hash}\n{vectors.mode}\n{vectors.digest}")
        if semantic.indexes:
            vector_coverage = vectors.coverage(semantic.model)
    give_back()  # what the build and the model of its vectors took, before the hub's tables take the graph's rows
    coverage = kg_coverage.build_coverage(report.coverage, graph_rows.call_reach(rows), skipped=report.skipped)
    kg_upload_sources.annotate(coverage, uploads)
    if vector_coverage is not None:
        coverage["vectors"] = vector_coverage
    if requests is not None:
        coverage["entity_requests"] = {"sha256": requests[1], "count": requests[3]}
    return Built(
        rows,
        content_hash,
        report.nodes,
        report.edges,
        len(ingests),
        merged,
        report.skipped,
        coverage,
        kg_coverage.source_files(project),
        requests,
        tuple(_corpus_items(project)),
        previous.content_hash if report.skipped and previous is not None else None,
    )


def _corpus_items(project: Project) -> list:
    corpus = project.corpus()
    try:
        return kg_graph_rows.corpus_items(corpus.items())
    finally:
        corpus.close()


def whole_graph(
    engine: AsyncEngine, loop: asyncio.AbstractEventLoop, project_id: int
) -> Callable[[], StoreRows | None]:
    """A call that reads all the rows of the graph the hub keeps of ``project_id`` (``kg_graph_rows.read_graph``) on
    ``loop``, the hub's, from the thread of a build: ``build_graph`` reads them only when it builds nothing. The
    connection goes back to the pool once the rows are fetched: putting them together takes minutes on a large graph
    and needs none."""

    def read() -> StoreRows | None:
        async def rows() -> StoreRows | None:
            async with engine.connect() as conn:
                fetched = await kg_graph_rows.fetch_graph(conn, project_id)
            return None if fetched is None else await kg_graph_rows.assemble_graph(fetched)

        return asyncio.run_coroutine_threadsafe(rows(), loop).result()

    return read


def describe_failure(exc: BaseException) -> str:
    """What kg_builds.error says about ``exc``: its own message for the failures this module names, the type and a
    short message otherwise."""
    if isinstance(exc, (BuildFailure, BlobStoreUnavailable, kg_sources.SourceFailure)):
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


def _failed_build(build_id: int, digest: str | None, runs: int, error: str):
    builds = tables.kg_builds
    return (
        update(builds)
        .values(status="failed", config_digest=digest, runs=runs, error=error, finished_at=func.now())
        .where(builds.c.id == build_id)
    )


async def record(engine, store: BlobStore, project_id: int, build_id: int, digest: str | None, built: Built) -> None:
    """Mark build ``build_id`` succeeded with ``built`` and keep its graph in the hub's tables (see the module)."""
    if built.requests is not None:
        await asyncio.to_thread(store.put_file, built.requests[1], built.requests[0])
    async with engine.begin() as conn:
        await blob_gc.lock_shared(conn)
        builds = tables.kg_builds
        await conn.execute(
            update(builds)
            .values(
                status="succeeded",
                config_digest=digest,
                runs=built.runs,
                content_hash=built.content_hash,
                nodes=built.nodes,
                edges=built.edges,
                error=None,
                finished_at=func.now(),
                coverage=built.coverage,
            )
            .where(builds.c.id == build_id)
        )
        if built.requests is not None:
            path, sha, size, _ = built.requests
            await kg_entity_relay.hold(conn, store, project_id, sha, path, size, kg_entity_relay.REQUESTS_KIND, None)
        await keep_source_files(conn, project_id, build_id, built.files)
        await keep_graph(conn, project_id, build_id, built)
        try:  # the Curator configures a new graph from its profile (kg_init); a failure never fails the build
            async with conn.begin_nested():
                await kg_init_db.on_build(conn, project_id, build_id, built.coverage)
        except Exception:
            log.exception("a kg init run was not queued", extra={"project_id": project_id, "build_id": build_id})
        try:  # a run maps the entities to the code (strategy entity-graph-agent); a failure never fails the build
            async with conn.begin_nested():
                await kg_entity_map_db.on_build(conn, project_id, build_id, built.coverage)
        except Exception:
            log.exception("a kg entity map run was not queued", extra={"project_id": project_id, "build_id": build_id})


async def keep_graph(conn: AsyncConnection, project_id: int, build_id: int, built: Built) -> None:
    """Keep the corpus items and the graph of a build in the hub's tables, only what changed, and notify the readers
    (``kg_graph_rows``). The head's content hash is the build's, which folds in the asked index and the vectors. A
    skipped build's rows are those the hub kept, its merged branches applied already, and main's hash folds them in as
    their merges did (``kg_branches.merged_hash``); a new build's get them. A
    skipped build whose head of main a merge moved since it read it (under the project's lock, as here) leaves it,
    and its build row then names the head's content hash and counts, not those of the rows it read, as the row of a
    new build does once merged branches changed its rows."""
    corpus = await kg_graph_rows.merge_corpus(conn, project_id, list(built.items))
    rows = dataclasses.replace(built.rows, content_hash=built.content_hash)
    if not built.skipped:  # a skipped build keeps the head it read, and its build id
        rows = dataclasses.replace(rows, summary={**rows.summary, "build_id": build_id})
    await kg_branches.lock(conn, project_id)  # a merge of a draft branch writes main too (``kg_branches``)
    if built.skipped and await _head_hash(conn, project_id) != built.read:
        # a branch merged since the build read main: main is as the merge wrote it, which the next build takes again
        log.info("kg graph left as a merge wrote it", extra={"project_id": project_id, "build_id": build_id})
        await _name_head(conn, project_id, build_id)
        return
    groups = await kg_branches.merged(conn, project_id)
    if not built.skipped:
        rows = await kg_branches.overlay(conn, project_id, rows, groups)
    elif groups:  # rows that hold the merged branches already: main's hash, as the merges gave it
        rows = dataclasses.replace(rows, content_hash=kg_branches.merged_hash(rows.content_hash, groups))
    changed = await kg_graph_rows.write_graph(conn, project_id, build_id, rows)
    if groups:  # the merged branches changed what the build made: its row names what main holds
        await _name_head(conn, project_id, build_id)
    log.info(
        "kg graph kept", extra={"project_id": project_id, "build_id": build_id, "corpus": corpus.counts(), **changed}
    )


async def _head_hash(conn: AsyncConnection, project_id: int) -> str | None:
    heads = tables.kg_graph_heads
    return (
        await conn.execute(
            select(heads.c.content_hash).where(
                heads.c.project_id == project_id, heads.c.branch_id == kg_graph_rows.MAIN
            )
        )
    ).scalar_one_or_none()


async def _name_head(conn: AsyncConnection, project_id: int, build_id: int) -> None:
    """Give build ``build_id`` the content hash and the node and edge counts of the head of main, which a merge wrote."""
    counts = [
        select(func.count())
        .select_from(t)
        .where(t.c.project_id == project_id, t.c.branch_id == kg_graph_rows.MAIN)
        .scalar_subquery()
        for t in (tables.kg_nodes, tables.kg_edges)
    ]
    builds = tables.kg_builds
    await conn.execute(
        update(builds)
        .values(content_hash=await _head_hash(conn, project_id), nodes=counts[0], edges=counts[1])
        .where(builds.c.id == build_id)
    )


async def start_eval_set(engine, project_id: int, project: str, built: Built) -> None:
    """A project with no question set gets version 1 from the commits of its first graph (``kg_eval_starter``). It
    never fails the build: a question set is a convenience, and the next build tries again."""
    try:
        async with engine.connect() as conn:
            if (
                await conn.execute(
                    select(tables.kg_eval_sets.c.version).where(tables.kg_eval_sets.c.project_id == project_id).limit(1)
                )
            ).first():
                return
            ceiling = await kg_eval_starter.ceiling_of(conn, project_id, project)
        questions = await asyncio.to_thread(kg_eval_starter.questions_of, built.rows, ceiling)
        async with engine.begin() as conn:
            made = await kg_eval_starter.start(conn, project_id, project, questions)
        if made:
            log.info("kg eval set started", extra={"project": project, "questions": made})
    except Exception as exc:
        log.warning("kg eval set not started", extra={"project": project, "error": str(exc)[:300]})


async def keep_source_files(conn: AsyncConnection, project_id: int, build_id: int, files: dict | None) -> None:
    """Make kg_source_files the files of each git source of build ``build_id`` (``kg_coverage.source_files``): a row
    each, and none for a source the build no longer has. Nothing when the build says nothing of them."""
    if files is None:
        return
    held = tables.kg_source_files
    await conn.execute(delete(held).where(held.c.project_id == project_id, held.c.source.not_in(list(files))))
    for source, found in sorted(files.items()):
        values = {
            "build_id": build_id,
            "read_commit": found.get("commit"),
            "files": found.get("files") or {},
            "file_count": len(found.get("files") or {}),
            "truncated": bool(found.get("truncated")),
            "updated_at": func.now(),
        }
        await conn.execute(
            pg_insert(held)
            .values(project_id=project_id, source=source, **values)
            .on_conflict_do_update(index_elements=[held.c.project_id, held.c.source], set_=values)
        )


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


async def _go_on(engine, manager, project_id: int, project: str, coverage: dict | None) -> None:
    """Queue the next build of a project whose entities step stopped at its ceiling after reading items
    (``entity_mapping.goes_on``), so the rest is read now rather than after the next push; a busy queue leaves it to
    that push, and a build that read nothing queues none."""
    from evo_agents.kg.entity_mapping import goes_on

    if manager is None or not goes_on(coverage):
        return
    try:
        async with engine.begin() as conn:
            await queue_build(JobQueue(manager), conn, project_id, project, None)
    except QueueBusy:
        log.warning(
            "kg build to go on with the entities step not queued: the queue was busy", extra={"project": project}
        )


async def _entity_key(conn: AsyncConnection, context, project_id: int, tuning) -> tuple[dict, str | None]:
    """The key of the entities step's url model, opened from a member's secret by the server's finder the ``hub worker``
    command hands down (``HubContext.entity_key``, ``evo_agents.hub.server.kg_entity_key.find``), and the secret's
    ``secret:<id>``; nothing when the project's strategy does not run the step with a url, or without a finder."""
    from evo_agents.kg import entities

    find = getattr(context, "entity_key", None)
    mapping = entities.enabled(tuning) if tuning is not None else None
    if find is None or mapping is None:
        return {}, None
    return await find(conn, getattr(context, "sealer", None), project_id, mapping)


async def _started(conn: AsyncConnection, project_id: int, build_id: int | None, job_id: int | None) -> int:
    """The id of the build this job runs, marked running: ``build_id``, or a new row when it is None or gone."""
    started = None
    if build_id is not None:
        started = (await conn.execute(_start(build_id, job_id))).one_or_none()
    if started is None:
        started = (await conn.execute(_start_unqueued(project_id, job_id))).one()
    return started[0]


async def _ingests(conn: AsyncConnection, project_id: int) -> list[Ingest]:
    """The runs pushed for the project's graph (kg_ingests), in the order of their ids."""
    kg_ingests = tables.kg_ingests
    run_id = cast(kg_ingests.c.run_id, Text).label("run_id")
    rows = await conn.execute(
        select(run_id, kg_ingests.c.log_sha256, kg_ingests.c.log_size)
        .where(kg_ingests.c.project_id == project_id)
        .order_by(run_id)
    )
    return [Ingest(**row._mapping) for row in rows]


async def run_build(context, project: str, build_id: int | None, job_id: int | None, manager=None) -> dict:
    """The job: build ``project`` and record the outcome on build ``build_id`` (a new row when it is None or gone).
    ``context`` is the worker's HubContext; ``manager``, procrastinate's job manager, queues the build again when
    the worker stops before this one finished."""
    engine = context.engine
    configs = tables.kg_configs
    async with engine.begin() as conn:
        row = (await conn.execute(project_id_query(project))).one_or_none()
        if row is None:
            log.warning("kg build of a project the hub does not have", extra={"project": project, "job_id": job_id})
            return {"status": "skipped"}
        project_id = row[0]
        build_id = await _started(conn, project_id, build_id, job_id)
        found = (
            await conn.execute(
                select(configs.c.digest, configs.c.knowledge, configs.c.ontology).where(
                    configs.c.project_id == project_id
                )
            )
        ).one_or_none()
        ingests = await _ingests(conn, project_id)
        answers = await kg_entity_relay.answer_hashes(conn, project_id)
        maps = await kg_entity_maps.map_hashes(conn, project_id)
    digest = found[0] if found else None
    log.info("kg build started", extra={"project": project, "build_id": build_id, "job_id": job_id})
    try:
        if found is None:
            raise BuildFailure(
                f"project {project} has no knowledge config on the hub yet: run `evo-agents hub kg push`"
            )
        config = {"knowledge": found[1], "ontology": found[2]}
        sources = await kg_sources.read_sources(engine, context.config, project_id, project, found[1])
        async with engine.connect() as conn:
            learned = await kg_asked.read(conn, project_id, project, found[1])
            tuning = await kg_settings.read(conn, project_id)
            uploads = await kg_upload_sources.latest(conn, project_id)
            keys, key_from = await _entity_key(conn, context, project_id, tuning)
            previous = await kg_graph_rows.read_head(conn, project_id)
        built = await asyncio.to_thread(
            build_in_cache,
            context.blobs,
            context.data_dir,
            project,
            config,
            ingests,
            sources,
            learned,
            None,
            tuning,
            answers,
            uploads,
            keys,
            maps,
            kg_semantic.HubCache(engine, asyncio.get_running_loop(), project_id),
            previous,
            whole_graph(engine, asyncio.get_running_loop(), project_id),
        )
        entities_coverage = (built.coverage or {}).get("entities")
        if key_from is not None and isinstance(entities_coverage, dict):
            entities_coverage["key_from"] = key_from
        await record(engine, context.blobs, project_id, build_id, digest, built)
        await fill_run_facts(engine, kg_home_dir(context.data_dir) / project / "log", project_id)
        await start_eval_set(engine, project_id, project, built)
        await _go_on(engine, manager, project_id, project, built.coverage)
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
            "skipped": built.skipped,
            "nodes": built.nodes,
            "edges": built.edges,
            "content_hash": built.content_hash,
        },
    )
    return {
        "status": "succeeded",
        "build_id": build_id,
        "content_hash": built.content_hash,
        "skipped": built.skipped,
    }


async def tell_halted(
    conn: AsyncConnection,
    project_id: int,
    project: str,
    count: int,
    *,
    body: str | None = None,
    error: str | None = None,
) -> None:
    """Tell the admins and the creator of a project that its builds stopped being queued (HALTED_NOTICE): by default
    because their worker kept dying; ``body`` and ``error`` say otherwise (``kg_build_queue._halted_by_failures``)."""
    from evo_agents.hub.server.notifications.service.deliveries import (
        NOTIFIER,  # notifications import the claim, which imports the KG
    )

    g, p = tables.grants, tables.projects
    admins = await conn.execute(select(g.c.user_id).where(g.c.project_id == project_id, g.c.role == "admin"))
    creator = await conn.scalar(select(p.c.created_by).where(p.c.id == project_id))
    details = {"project": project, "builds_in_a_row": count, **({"error": error} if error is not None else {})}
    for user_id in sorted({*admins.scalars(), *([creator] if creator is not None else [])}):
        await NOTIFIER.notify(
            conn,
            user_id=user_id,
            kind="notice",
            notice_kind=HALTED_NOTICE,
            project_id=project_id,
            title=f"Knowledge graph builds of {project} are no longer queued again",
            body=body
            or (
                f"The last {count} builds of {project} ended because the worker running them stopped before they "
                "finished, which a build over the memory limit of worker-kg (EVO_HUB_WORKER_KG_MEMORY) does. The hub "
                "queues no new build after such a stop until one succeeds or a member queues one: "
                f"`evo-agents hub kg build --project {project}` once the cause is fixed."
            ),
            details=details,
            link=f"/p/{project}/kg",
        )

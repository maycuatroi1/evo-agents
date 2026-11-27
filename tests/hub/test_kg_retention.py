"""The graph files older builds uploaded leave the bucket: the retention deletes the artifacts of older graphs
(``evo_agents.hub.kg_prune``, ``evo_agents.hub.blob_gc``). A build keeps its graph in the hub's tables and uploads no
file (``evo_agents.hub.kg_build``), so each test gives its builds the artifact such a build had by hand
(``seed_artifact``): bytes in the bucket, a kg-graph row in ``blobs`` and the build's ``artifact_*`` columns.

Checked here: a prune keeps
the artifacts of each project's newest graphs (an artifact several builds share counted once), drops the others from
the build rows and ``blobs`` before deleting their objects, never touches run logs, source blobs or skill bundles, is
refused to anyone but a hub admin, and deletes nothing the second time. Every hash the restore drill of the
deployment reads (``blobs``, ``skill_versions``, ``kg_builds``, ``kg_ingests``) has its object after a prune. A build
that finishes while a prune runs, two prunes at once, and a commit of an artifact's bytes racing its deletion all
leave rows and objects consistent. The CLI's ``hub kg prune`` runs against ``hub serve``.

The api and the worker run in this process, as in ``tests.hub.test_kg``, whose machines and helpers these tests use;
the blob store is moto's server (``tests.hub.s3``)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import threading
import time

import pytest

from tests.hub import live, pg
from tests.hub.contract_keys import assert_json_keys

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import Boolean, Text, cast, column, func, insert, select, table, update

from evo_agents.hub import blob_gc, tables
from evo_agents.hub.blobs import BLOB_PREFIX, BlobStore, blob_key
from evo_agents.hub.client import Hub
from evo_agents.hub.config import MAX_KG_KEEP_ARTIFACTS
from evo_agents.hub.db import make_engine, open_pool
from evo_agents.hub.jobs import PRUNE_KG_ARTIFACTS, JobQueue
from evo_agents.hub.kg_prune import prune
from evo_agents.hub.skills import pack
from evo_agents.hub.worker import HubContext, connector, queue
from tests.hub.s3 import put_presigned
from tests.hub.test_kg import (
    PROJECT,
    REGISTRATION,
    BackgroundWorker,
    Machine,
    build_rows,
    count,
    doc,
    grant,
    latest,
    make_config,
    ok,
    open_hub,
    procrastinate_jobs,
    push,
    query,
    sha,
    sign_in,
    tool_ok,
)

# What the restore drill of the deployment reads, statement for statement: the distinct (hash, size) pairs of these
# four tables, the builds' only where they still hold an artifact. Every hash these name must have its object.
DRILL = {
    "blobs": select(tables.blobs.c.sha256, tables.blobs.c.size).distinct(),
    "skill_versions": select(tables.skill_versions.c.sha256, tables.skill_versions.c.size).distinct(),
    "kg_builds": select(tables.kg_builds.c.artifact_sha256, tables.kg_builds.c.artifact_size)
    .distinct()
    .where(tables.kg_builds.c.artifact_sha256.is_not(None)),
    "kg_ingests": select(tables.kg_ingests.c.log_sha256, tables.kg_ingests.c.log_size).distinct(),
}
# The catalogs the concurrency tests watch the advisory locks in.
pg_locks = table("pg_locks", column("database"), column("locktype"), column("granted", Boolean))
pg_database = table("pg_database", column("oid"), column("datname", Text))
BETA = "beta"


@pytest.fixture
def hub(hub_db, tmp_path, s3):
    client, found = open_hub(hub_db, tmp_path, s3)
    try:
        yield found
    finally:
        client.__exit__(None, None, None)


def run_worker(hub, keep: int = MAX_KG_KEEP_ARTIFACTS) -> None:
    """Run every queued job with a worker in this process, until none is left. Its own retention keeps ``keep``
    graphs: the hourly one may come due during a test, and must not prune what the test prunes itself."""
    config = dataclasses.replace(make_config(hub.db, hub.worker_dir, hub.s3), kg_keep_artifacts=keep)

    async def main():
        store = BlobStore.from_config(config)
        try:
            with queue.replace_connector(connector(config, 1)):
                await queue.open_async()
                try:
                    context = HubContext(config, queue.connector.pool, store, config.data_dir)
                    await queue.run_worker_async(
                        concurrency=1,
                        wait=False,
                        install_signal_handlers=False,
                        listen_notify=False,
                        additional_context={"hub": context},
                    )
                finally:
                    await queue.close_async()
        finally:
            store.close()

    asyncio.run(main())


def seed_artifact(
    hub, build: dict, project: str = PROJECT, shares: dict | None = None, data: bytes | None = None
) -> dict:
    """Give ``build`` the artifact a build had before the hub kept graphs in its tables: a graph file of its own in
    the bucket and in ``blobs`` (kind kg-graph, no creator), ``data`` or bytes naming the build, or the one of
    ``shares``, which it then names in ``artifact_reused_from``. The build as the api shows it then."""
    builds = tables.kg_builds
    if shares is None:
        data = data or f"the graph file of build {build['id']}".encode()
        sha256, size, reused_from = sha(data), len(data), None
        hub.s3.put(blob_key(sha256), data)
        owner = select(builds.c.project_id).where(builds.c.id == build["id"]).scalar_subquery()
        values = {"project_id": owner, "sha256": sha256, "size": size, "kind": "kg-graph", "created_by": None}
        query(hub.db, insert(tables.blobs).values(**values))
    else:
        sha256, size, reused_from = shares["artifact_sha256"], shares["artifact_size"], shares["id"]
    query(
        hub.db,
        update(builds)
        .values(artifact_sha256=sha256, artifact_size=size, artifact_reused_from=reused_from)
        .where(builds.c.id == build["id"]),
    )
    found = hub.as_alice.call("GET", f"/v1/kg/{project}/builds")["builds"]
    return next(b for b in found if b["id"] == build["id"])


def graph(hub, machine: Machine, version: int) -> dict:
    """Push a run whose content is new (version ``version`` of the guide) and build it; the build, with an artifact
    of its own."""
    machine.sync("docs", doc("guide", f"How the app works, version {version}.", rev=str(version)))
    assert push(hub, machine).ok
    run_worker(hub)
    build = latest(hub)
    assert build["status"] == "succeeded", build
    return seed_artifact(hub, build)


def rebuild(hub, shares: dict) -> dict:
    """Build the project again, its content unchanged; the build, pointing at the artifact of ``shares``."""
    response = hub.client.post(f"/v1/kg/{PROJECT}/builds", headers=hub.alice)
    assert response.status_code == 202 and response.json()["queued"], response.text
    run_worker(hub)
    build = latest(hub)
    assert build["status"] == "succeeded" and build["content_hash"] == shares["content_hash"], build
    return seed_artifact(hub, build, shares=shares)


def objects(hub) -> dict[str, int]:
    """Every object under blobs/sha256/ in the bucket: hash to size."""
    found = {}
    pages = hub.s3.client().get_paginator("list_objects_v2").paginate(Bucket=hub.s3.bucket, Prefix=BLOB_PREFIX)
    for page in pages:
        for item in page.get("Contents", []):
            found[item["Key"].removeprefix(BLOB_PREFIX)] = item["Size"]
    return found


def assert_drill_holds(hub) -> None:
    """The restore drill's check: no hash it reads lacks its object, and no object has another size."""
    present = objects(hub)
    for name, statement in DRILL.items():
        rows = {(sha256, size) for sha256, size in query(hub.db, statement)}
        missing = sorted(sha256 for sha256, _ in rows if sha256 not in present)
        wrong = sorted(sha256 for sha256, size in rows if sha256 in present and present[sha256] != size)
        assert (missing, wrong) == ([], []), f"{name}: missing {missing}, wrong size {wrong}"


def corpus(hub) -> set[str]:
    """The hashes of every run log and source blob the hub holds."""
    blobs = tables.blobs
    return {row[0] for row in query(hub.db, select(blobs.c.sha256).where(blobs.c.kind.in_(["kg-log", "kg-blob"])))}


def kg_graphs(hub) -> set[str]:
    blobs = tables.blobs
    return {row[0] for row in query(hub.db, select(blobs.c.sha256).where(blobs.c.kind == "kg-graph"))}


def prune_api(hub, who: str = "admin", **body):
    return hub.client.post("/v1/admin/kg/prune", json=body, headers=getattr(hub, who))


def add_beta(hub) -> None:
    beta = dict(REGISTRATION, harness={"name": BETA, "workspace": "~/ws", "path": "beta-harness"})
    assert hub.client.put(f"/v1/projects/{BETA}", json=beta, headers=hub.admin).status_code == 200
    grant(hub.client, hub.admin, "alice", "writer", "internal", project=BETA)


def publish_bundle(hub, tmp_path) -> str:
    """A global skill published by the admin; its bundle's hash."""
    data = pack(live_skill(tmp_path)).data
    item = {"sha256": sha(data), "size": len(data), "kind": "skill-bundle"}
    asked = hub.client.post("/v1/blobs/uploads", json={"items": [item]}, headers=hub.admin)
    assert asked.status_code == 200, asked.text
    (ticket,) = asked.json()["uploads"]
    assert put_presigned(ticket["url"], data) == 200
    committed = hub.client.post("/v1/blobs/commit", json={"upload_ids": [ticket["upload_id"]]}, headers=hub.admin)
    assert committed.status_code == 200, committed.text
    version = hub.client.post(
        "/v1/skills/global/house-style/versions", json={"sha256": sha(data), "size": len(data)}, headers=hub.admin
    )
    assert version.status_code == 200, version.text
    return sha(data)


def live_skill(tmp_path):
    directory = tmp_path / "skills" / "house-style"
    directory.mkdir(parents=True)
    text = "---\nname: house-style\ndescription: Use when writing for the team\n---\nWrite plainly.\n"
    (directory / "SKILL.md").write_text(text, encoding="utf-8")
    return directory


# The retention


def test_prune_keeps_each_project_s_newest_graphs_and_deletes_only_older_artifacts(hub, tmp_path):
    bundle = publish_bundle(hub, tmp_path)
    laptop = Machine(tmp_path, "laptop")
    b1 = graph(hub, laptop, 1)
    b2 = rebuild(hub, b1)  # shares b1's artifact
    b3, b4, b5 = (graph(hub, laptop, version) for version in (2, 3, 4))
    add_beta(hub)
    beta_build = graph_of(hub, Machine(tmp_path, "beta-laptop", project=BETA), BETA)
    kept_corpus = corpus(hub)
    before = objects(hub)
    all_builds = select(tables.kg_builds).order_by(tables.kg_builds.c.id)
    rows_before = query(hub.db, all_builds)

    dry = prune_api(hub, keep=2, dry_run=True)
    assert dry.status_code == 200, dry.text
    old = {b1["artifact_sha256"]: b1["artifact_size"], b3["artifact_sha256"]: b3["artifact_size"]}
    old_bytes = sum(old.values())
    assert dry.json() == {
        "dry_run": True,
        "keep": 2,
        "graph_files": False,
        "projects": [
            {"project": PROJECT, "artifacts": 4, "kept": 2, "pruned": 2, "pruned_bytes": old_bytes, "builds": 3},
            {"project": BETA, "artifacts": 1, "kept": 1, "pruned": 0, "pruned_bytes": 0, "builds": 0},
        ],
        "deleted": 2,
        "deleted_bytes": old_bytes,
        "pending": 0,
    }
    assert objects(hub) == before and query(hub.db, all_builds) == rows_before
    assert count(hub.db, tables.blob_deletions) == 0
    assert query(hub.db, select(func.count()).where(tables.audit.c.action == "kg.prune")) == [(0,)]

    done = prune_api(hub, keep=2)
    assert done.status_code == 200, done.text
    assert done.json() == {**dry.json(), "dry_run": False}
    after = objects(hub)
    assert set(before) - set(after) == set(old)  # the two oldest artifacts, nothing else
    assert kept_corpus <= set(after) and corpus(hub) == kept_corpus and bundle in after
    assert {b4["artifact_sha256"], b5["artifact_sha256"], beta_build["artifact_sha256"]} <= set(after)
    assert kg_graphs(hub) == {b4["artifact_sha256"], b5["artifact_sha256"], beta_build["artifact_sha256"]}

    builds = {b["id"]: b for b in build_rows(hub)}
    for pruned in (b1, b2, b3):
        row = builds[pruned["id"]]
        assert row["status"] == "succeeded" and row["artifact_sha256"] is None and row["artifact_pruned_at"]
        assert (row["content_hash"], row["nodes"], row["edges"], row["artifact_size"]) == (
            pruned["content_hash"],
            pruned["nodes"],
            pruned["edges"],
            pruned["artifact_size"],
        )
    assert builds[b2["id"]]["artifact_reused_from"] == b1["id"]
    for kept in (b4, b5):
        assert builds[kept["id"]]["artifact_sha256"] == kept["artifact_sha256"]
        assert builds[kept["id"]]["artifact_pruned_at"] is None
    deletions = tables.blob_deletions
    deleted = select(deletions.c.sha256, deletions.c.kind).where(deletions.c.deleted_at.is_not(None))
    assert query(hub.db, deleted.order_by(deletions.c.sha256)) == sorted((sha256, "kg-graph") for sha256 in old)
    trail, projects, users = tables.audit, tables.projects, tables.users
    audit = query(
        hub.db,
        select(trail.c.target, projects.c.name, users.c.login)
        .join_from(trail, projects, projects.c.id == trail.c.project_id)
        .join(users, users.c.id == trail.c.actor_id)
        .where(trail.c.action == "kg.prune"),
    )
    assert audit == [(f"{PROJECT} keep=2", PROJECT, live.ADMIN)]
    assert_drill_holds(hub)
    # the newest graph still answers, and a second prune finds nothing to do
    assert tool_ok(hub, "kg_status")["structuredContent"]["hub_build"]["build_id"] == b5["id"]
    again = prune_api(hub, keep=2).json()
    assert again["deleted"] == 0 and all(p["pruned"] == 0 for p in again["projects"])
    assert objects(hub) == after

    # one project at a time, with the hub's own keep: beta holds a single graph, which always stays
    only = prune_api(hub, project=BETA).json()
    assert only["keep"] == 3 and only["projects"] == [
        {"project": BETA, "artifacts": 1, "kept": 1, "pruned": 0, "pruned_bytes": 0, "builds": 0}
    ]


def graph_of(hub, machine: Machine, project: str) -> dict:
    """Push ``machine``'s first run of project ``project`` and build it; the build."""
    machine.sync("docs", doc("guide", f"How {project} works."))
    assert push(hub, machine).ok
    run_worker(hub)
    (build, *_) = hub.as_alice.call("GET", f"/v1/kg/{project}/builds")["builds"]
    assert build["status"] == "succeeded", build
    return seed_artifact(hub, build, project)


def test_prune_is_for_hub_admins_and_checks_what_it_is_given(hub):
    refused = prune_api(hub, "alice", dry_run=True)
    assert refused.status_code == 403
    unknown = prune_api(hub, project="nowhere", dry_run=True)
    assert unknown.status_code == 404 and "nowhere" in unknown.json()["message"]
    for keep in (0, MAX_KG_KEEP_ARTIFACTS + 1):
        assert prune_api(hub, keep=keep).status_code == 422
    assert prune_api(hub, dry_run=True).json() == {
        "dry_run": True,
        "keep": 3,
        "graph_files": False,
        "projects": [],
        "deleted": 0,
        "deleted_bytes": 0,
        "pending": 0,
    }


def test_graph_files_prunes_every_graph_file_of_a_project_whose_graph_the_tables_hold(hub, tmp_path):
    laptop = Machine(tmp_path, "laptop")
    b1, b2 = (graph(hub, laptop, version) for version in (1, 2))
    kept_corpus = corpus(hub)
    hourly = prune_api(hub, keep=3, dry_run=True).json()
    assert hourly["graph_files"] is False and hourly["deleted"] == 0  # the newest graphs keep their file
    dry = prune_api(hub, keep=3, dry_run=True, graph_files=True).json()
    assert dry["graph_files"] is True and dry["projects"][0]["pruned"] == 2 and dry["projects"][0]["kept"] == 0
    assert kg_graphs(hub) == {b1["artifact_sha256"], b2["artifact_sha256"]}  # a dry run deletes nothing
    done = prune_api(hub, keep=3, graph_files=True)
    assert done.status_code == 200, done.text
    assert done.json()["deleted"] == 2 and kg_graphs(hub) == set()
    assert not {b1["artifact_sha256"], b2["artifact_sha256"]} & set(objects(hub))
    assert corpus(hub) == kept_corpus  # run logs and source blobs stay
    response = hub.client.post(
        f"/v1/kg/{PROJECT}/tools/kg_search", json={"arguments": {"query": "app"}}, headers=hub.alice
    )
    assert response.status_code == 200 and not response.json().get("isError")  # the tools read the tables


def test_every_hash_the_restore_drill_reads_has_its_object_after_the_hourly_prune(hub, tmp_path):
    from evo_agents.hub import jobs

    periodic = {p.task.name: p for p in queue.periodic_registry.periodic_tasks.values()}
    assert periodic[jobs.PRUNE_KG_ARTIFACTS].cron == "31 * * * *"  # hourly
    assert queue.tasks[jobs.PRUNE_KG_ARTIFACTS].queueing_lock == jobs.PRUNE_KG_ARTIFACTS

    publish_bundle(hub, tmp_path)
    laptop = Machine(tmp_path, "laptop")
    first = graph(hub, laptop, 1)
    rebuild(hub, first)
    for version in (2, 3):
        graph(hub, laptop, version)
    assert_drill_holds(hub)
    assert len(kg_graphs(hub)) == 3

    async def defer():
        pool = await open_pool(make_config(hub.db, tmp_path / "defer", hub.s3))
        try:
            await (await JobQueue.open(pool)).defer(PRUNE_KG_ARTIFACTS, queueing_lock=PRUNE_KG_ARTIFACTS)
        finally:
            await pool.close()

    asyncio.run(defer())
    run_worker(hub, keep=1)  # the worker's job, with EVO_HUB_KG_KEEP_ARTIFACTS=1
    jobs = procrastinate_jobs.c
    statuses = query(hub.db, select(cast(jobs.status, Text)).where(jobs.task_name == PRUNE_KG_ARTIFACTS))
    assert statuses and {row[0] for row in statuses} == {"succeeded"}  # the hourly tick may have run one too
    assert len(kg_graphs(hub)) == 1 and first["artifact_sha256"] not in objects(hub)
    assert_drill_holds(hub)
    builds, trail = tables.kg_builds, tables.audit
    pruned = query(hub.db, select(func.count()).where(builds.c.artifact_pruned_at.is_not(None)))[0][0]
    assert pruned == 3  # two builds shared the first artifact
    assert query(hub.db, select(trail.c.actor_id).where(trail.c.action == "kg.prune")) == [(None,)]  # the hub itself


# Concurrency


def waiting_on_the_blob_lock(hub) -> int:
    waiting = (
        select(func.count())
        .select_from(pg_locks)
        .join(pg_database, pg_database.c.oid == pg_locks.c.database)
        .where(pg_locks.c.locktype == "advisory", ~pg_locks.c.granted, pg_database.c.datname == hub.db.name)
    )
    return query(hub.db, waiting)[0][0]


def test_a_build_that_finishes_while_a_prune_runs_is_kept_and_nothing_dangles(hub, tmp_path, monkeypatch):
    laptop = Machine(tmp_path, "laptop")
    first, second = graph(hub, laptop, 1), graph(hub, laptop, 2)
    laptop.sync("docs", doc("guide", "How the app works, version 3.", rev="3"))
    assert push(hub, laptop).ok  # queues a third build, of new content
    recording, release = threading.Event(), threading.Event()
    original = blob_gc.lock_shared

    async def held(conn):  # the build holds the lock while it writes its row, until the test lets it go
        await original(conn)
        recording.set()
        await asyncio.to_thread(release.wait, 30)

    monkeypatch.setattr(blob_gc, "lock_shared", held)
    answers = []
    with BackgroundWorker(hub.db, hub.s3, hub.worker_dir, concurrency=1):
        assert recording.wait(60), "the build did not reach the point where it records its graph"
        pruning = threading.Thread(target=lambda: answers.append(prune_api(hub, keep=1)), daemon=True)
        pruning.start()
        deadline = time.monotonic() + 10
        while not waiting_on_the_blob_lock(hub):
            assert time.monotonic() < deadline, "the prune did not wait for the build"
            time.sleep(0.05)
        assert pruning.is_alive() and not answers  # the prune waits while the build records
        release.set()
        pruning.join(30)
        deadline = time.monotonic() + 30
        while latest(hub)["status"] != "succeeded":
            assert time.monotonic() < deadline, latest(hub)
            time.sleep(0.1)
    (answer,) = answers
    assert answer.status_code == 200, answer.text
    third = latest(hub)
    assert answer.json()["projects"] == [
        {
            "project": PROJECT,
            "artifacts": 2,
            "kept": 1,
            "pruned": 1,
            "pruned_bytes": first["artifact_size"],
            "builds": 1,
        }
    ]
    # the third build uploaded nothing: its graph is in the tables, and the newest artifact stays
    assert third["graph_kept"] and third["artifact_sha256"] is None and third["artifact_pruned_at"] is None
    assert objects(hub)[second["artifact_sha256"]] == second["artifact_size"]
    assert kg_graphs(hub) == {second["artifact_sha256"]}
    assert_drill_holds(hub)


def test_two_prunes_at_once_delete_each_artifact_once(hub, tmp_path):
    laptop = Machine(tmp_path, "laptop")
    for version in (1, 2, 3, 4):
        graph(hub, laptop, version)
    before = objects(hub)

    async def both():
        config = make_config(hub.db, tmp_path / "prunes", hub.s3)
        pools = [await open_pool(config), await open_pool(config)]
        store = BlobStore.from_config(config)
        try:
            return await asyncio.gather(*(prune(make_engine(pool), store, 1) for pool in pools))
        finally:
            for pool in pools:
                await pool.close()
            store.close()

    reports = asyncio.run(both())
    assert sorted(sum(p.pruned for p in r.projects) for r in reports) == [0, 3]  # one of them marked all three
    assert sum(r.deleted for r in reports) == 3 and all(r.pending == 0 for r in reports)
    assert len(set(before) - set(objects(hub))) == 3
    deletions = tables.blob_deletions
    assert query(hub.db, select(func.count()).where(deletions.c.deleted_at.is_not(None))) == [(3,)]
    assert_drill_holds(hub)


def test_an_artifact_s_bytes_committed_while_the_retention_deletes_them_are_put_back(hub, tmp_path, monkeypatch):
    laptop = Machine(tmp_path, "laptop")
    first = graph(hub, laptop, 1)
    graph(hub, laptop, 2)
    add_beta(hub)
    data = hub.s3.get(blob_key(first["artifact_sha256"]))
    item = {"sha256": first["artifact_sha256"], "size": len(data), "kind": "kg-blob"}
    asked = hub.client.post("/v1/blobs/uploads", json={"project": BETA, "items": [item]}, headers=hub.alice)
    assert asked.status_code == 200, asked.text
    (ticket,) = asked.json()["uploads"]  # beta does not hold it, even though alpha does
    assert put_presigned(ticket["url"], data) == 200
    store = hub.app.state.blobs
    original = store.publish_all
    pruned = []

    def publish_then_prune(uploads, held=frozenset()):
        # The hub holds the hash (alpha's artifact), so the commit skips the copy. Before it writes beta's row, the
        # retention drops alpha's artifact and deletes the object.
        written = original(uploads, held)
        if not pruned:

            async def run():
                pool = await open_pool(make_config(hub.db, tmp_path / "racing", hub.s3))
                own = BlobStore.from_config(make_config(hub.db, tmp_path / "racing", hub.s3))
                try:
                    return await prune(make_engine(pool), own, 1, PROJECT)
                finally:
                    await pool.close()
                    own.close()

            pruned.append(asyncio.run(run()))
            assert hub.s3.get(blob_key(first["artifact_sha256"])) is None  # gone, for now
        return written

    monkeypatch.setattr(store, "publish_all", publish_then_prune)
    committed = hub.client.post(
        "/v1/blobs/commit", json={"project": BETA, "upload_ids": [ticket["upload_id"]]}, headers=hub.alice
    )
    assert committed.status_code == 200, committed.text
    assert pruned and pruned[0].deleted == 1
    assert hub.s3.get(blob_key(first["artifact_sha256"])) == data  # the commit put the bytes back
    blobs, projects, deletions = tables.blobs, tables.projects, tables.blob_deletions
    holders = query(
        hub.db,
        select(projects.c.name, blobs.c.kind)
        .join_from(blobs, projects, projects.c.id == blobs.c.project_id)
        .where(blobs.c.sha256 == first["artifact_sha256"]),
    )
    assert holders == [(BETA, "kg-blob")]
    deleting = select(func.count()).where(deletions.c.sha256 == first["artifact_sha256"])
    assert query(hub.db, deleting) == [(0,)]
    assert_drill_holds(hub)
    # nothing refers to it as an artifact any more, and a later prune leaves beta's blob alone
    assert prune_api(hub, keep=1).json()["deleted"] == 0
    assert hub.s3.get(blob_key(first["artifact_sha256"])) == data


# The command line


def test_the_cli_prunes_as_a_hub_admin_only(hub_db, tmp_path, s3):
    with live.running_hub(hub_db, tmp_path, EVO_HUB_ADMINS=live.ADMIN, **s3.env()) as served:
        admin = Hub(served.url, live.insert_token(hub_db, live.ADMIN))
        admin.call("PUT", f"/v1/projects/{PROJECT}", REGISTRATION)
        admin.call("PUT", f"/v1/admin/projects/{PROJECT}/grants/alice", {"role": "writer", "max_level": "internal"})
        home = sign_in(tmp_path / "admin-home", served.url, live.ADMIN, live.insert_token(hub_db, live.ADMIN))
        env = pg.clean_env(HOME=str(home))

        dry = json.loads(ok(pg.cli(["hub", "kg", "prune", "--dry-run", "--json"], env=env)).stdout)
        assert_json_keys("hub kg prune", dry)
        assert dry == {
            "dry_run": True,
            "keep": 3,
            "graph_files": False,
            "projects": [],
            "deleted": 0,
            "deleted_bytes": 0,
            "pending": 0,
        }
        text = ok(pg.cli(["hub", "kg", "prune", "--project", PROJECT, "--keep", "2"], env=env)).stdout
        assert text.splitlines() == [
            "PROJECT  ARTIFACTS  KEPT  PRUNED  PRUNED SIZE  BUILDS",
            f"{PROJECT}    0          0     0       0 B          0",
            "Kept the artifacts of the 2 newest graph(s) of each project: deleted 0 object(s), 0 B.",
        ]

        alice = sign_in(tmp_path / "alice-home", served.url, "alice", live.insert_token(hub_db, "alice"))
        refused = pg.cli(["hub", "kg", "prune", "--dry-run"], env=pg.clean_env(HOME=str(alice)))
        assert refused.returncode == 1 and "admin" in refused.stderr
        bad = pg.cli(["hub", "kg", "prune", "--keep", "0"], env=env)
        assert bad.returncode == 2 and "at least 1" in bad.stderr

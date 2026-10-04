"""Knowledge graphs on the hub: machines push the logs of their connector runs, the worker merges and builds, the api
answers kg_* from the latest build.

Checked here: pushing a run twice ingests it once; two machines pushing in either order give one content hash,
which ``kg build --verify`` on the worker's store and a local build of both corpora find too; a run labelled above the
hub sink is 422 with no kg_ingests row, no job and no staged object left; committing a run while blobs are missing is
422; three builds queued in a row never run two at once and queue at most one; deleting the worker's cache and
building again gives the same content hash; an artifact whose SHA-256 does not match is never opened; with the blob
store down, kg_* answer from the cached graph. Also the failure paths around them.

The api runs in this process (TestClient), the worker too (procrastinate's worker on this process's loop, the
``hub.kg_build`` task of ``evo_agents.hub.worker``), and the blob store is moto's server in this process
(``tests.hub.s3``). Machines are harnesses with the fake connector of ``tests.kg.fakes`` and a kg home of their own;
they push through ``evo_agents.hub.kg_push`` with ``Hub.call`` routed to the app, and their presigned PUTs go to
the fake S3 over HTTP. The CLI tests run ``hub serve`` as a process of its own. Nothing touches ~/.evo/kg."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.hub import live, pg
from tests.hub.s3 import put_presigned

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient

from evo_agents.hub import kg_build as hub_kg_build
from evo_agents.hub.blobs import BlobStore, blob_key
from evo_agents.hub.client import Hub, HubError
from evo_agents.hub.config import HubConfig
from evo_agents.hub.kg_build import worker_project
from evo_agents.hub.kg_push import Pusher
from evo_agents.hub.server.app import create_app
from evo_agents.hub.worker import HubContext, connector, queue
from evo_agents.kg import serve
from evo_agents.kg.build import build_project
from evo_agents.kg.corpus import Corpus
from evo_agents.kg.project import load_project_at
from evo_agents.kg.serve import Session
from evo_agents.kg.sync import sync_project

PROJECT = "alpha"
AGENT = "claude-code@anthropic"
LEVELS = ["public", "internal", "customer", "secret"]
REGISTRATION = {
    "levels": LEVELS,
    "locations": ["any"],
    "default_label": {"level": "internal"},
    "sinks": [
        {"id": AGENT, "kind": "agent-session", "clearance": {"level": "customer"}},
        {"id": "hub", "kind": "hub", "clearance": {"level": "internal"}},
    ],
    "repos": [],
    "harness": {"name": PROJECT, "workspace": "~/ws", "path": "alpha-harness"},
}
KNOWLEDGE = """\
version: 1
project: {project}
policy:
  levels: [public, internal, customer, secret]
  sinks:
    - {{id: claude-code@anthropic, kind: agent-session, clearance: {{level: customer}}}}
    - {{id: hub, kind: hub, clearance: {{level: internal}}}}
identifiers:
  - {{kind: Requirement, pattern: "KB-[0-9]+"}}
sources:
  - id: docs
    connector: "python:tests.kg.fakes:run"
    config: {{file: "{docs}"}}
    label: {{level: internal, integrity: U}}
    deletion: {{max_removal_ratio: 1.0, min_scope: 0}}
  - id: vault
    connector: "python:tests.kg.fakes:run"
    config: {{file: "{vault}"}}
    label: {{level: secret, integrity: U}}
"""
SECRET_TEXT = "the vault combination is 4-8-15-16-23-42"  # must never reach the hub


def doc(key: str, body: str, rev: str = "1", **extra) -> dict:
    text = f"# {key}\n\n{body}\n\n## Details of {key}\n\nSee KB-01 and KB-02 in {key}.\n"
    return {"key": key, "text": text, "rev": rev, **extra}


class Machine:
    """One machine of a member: a harness of project alpha and a kg home of its own."""

    def __init__(self, base: Path, name: str, project: str = PROJECT):
        self.root = base / name
        self.harness = self.root / f"{project}-harness"
        self.home = self.root / "kg-home"
        self.harness.mkdir(parents=True)
        self.files = {source: self.root / f"{source}.json" for source in ("docs", "vault")}
        for path in self.files.values():
            path.write_text(json.dumps({"items": []}), encoding="utf-8")
        (self.harness / "harness.yaml").write_text(
            f"name: {project}\nrepos: []\nknowledge_file: knowledge.yaml\n", encoding="utf-8"
        )
        (self.harness / "knowledge.yaml").write_text(
            KNOWLEDGE.format(project=project, docs=self.files["docs"], vault=self.files["vault"]), encoding="utf-8"
        )

    def project(self):
        return load_project_at(self.harness, self.home)

    def sync(self, source: str, *items: dict) -> list:
        self.files[source].write_text(json.dumps({"items": list(items)}), encoding="utf-8")
        results = sync_project(self.project(), [source])
        assert all(r.ok for r in results), [r.issues for r in results]
        return [r.run_id for r in results]

    def logs(self) -> list[Path]:
        return sorted((self.home / PROJECT / "log").glob("*.jsonl.gz"))


class InProcessHub:
    """``Hub.call`` through the in-process app, as one member."""

    url = "https://hub.test"

    def __init__(self, client, headers):
        self.client = client
        self.headers = headers

    def call(self, method, path, body=None):
        response = self.client.request(method, path, json=body, headers=self.headers)
        payload = response.json() if response.content else None
        if response.status_code >= 400:
            raise HubError(payload.get("message"), response.status_code, payload.get("error"), payload)
        return payload


def make_config(db, data_dir: Path, s3) -> HubConfig:
    return HubConfig(
        dsn=db.dsn,
        data_dir=data_dir,
        pool_min_size=1,
        pool_max_size=6,
        pool_timeout=10.0,
        admins=frozenset({live.ADMIN}),
        **s3.config(),
    )


def run_jobs(db, s3, data_dir: Path, concurrency: int = 1) -> None:
    """Run every queued job with a worker in this process, until none is left."""
    config = make_config(db, data_dir, s3)

    async def main():
        store = BlobStore.from_config(config)
        try:
            with queue.replace_connector(connector(config, concurrency)):
                await queue.open_async()
                try:
                    context = HubContext(config, queue.connector.pool, store, config.data_dir)
                    await queue.run_worker_async(
                        concurrency=concurrency,
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


class BackgroundWorker:
    """A worker in a thread of this process that waits for jobs until the block ends."""

    def __init__(self, db, s3, data_dir: Path, concurrency: int):
        self.config = make_config(db, data_dir, s3)
        self.concurrency = concurrency
        self.ready = threading.Event()
        self.failure: BaseException | None = None

    async def _main(self):
        self.loop = asyncio.get_running_loop()
        store = BlobStore.from_config(self.config)
        try:
            with queue.replace_connector(connector(self.config, self.concurrency)):
                await queue.open_async()
                try:
                    context = HubContext(self.config, queue.connector.pool, store, self.config.data_dir)
                    self.task = asyncio.ensure_future(
                        queue.run_worker_async(
                            concurrency=self.concurrency,
                            wait=True,
                            install_signal_handlers=False,
                            listen_notify=True,
                            additional_context={"hub": context},
                        )
                    )
                    self.ready.set()
                    try:
                        await self.task
                    except asyncio.CancelledError:
                        pass
                finally:
                    await queue.close_async()
        finally:
            store.close()

    def _run(self):
        try:
            asyncio.run(self._main())
        except BaseException as exc:  # surfaced by __exit__
            self.failure = exc
            self.ready.set()

    def __enter__(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        assert self.ready.wait(30), "the background worker did not start"
        assert self.failure is None, self.failure
        return self

    def __exit__(self, *exc):
        self.loop.call_soon_threadsafe(self.task.cancel)
        self.thread.join(60)
        assert not self.thread.is_alive(), "the background worker did not stop"
        assert self.failure is None, self.failure


def query(db, statement: str, params=()):
    return live.sql(db, statement, params)


def count(db, table: str) -> int:
    return query(db, f"SELECT count(*) FROM {table}")[0][0]


def jobs(db) -> list[tuple]:
    """The kg build jobs; the worker's periodic jobs may come and go."""
    return query(
        db,
        "SELECT id, status::text, lock, queueing_lock FROM procrastinate_jobs WHERE task_name = 'hub.kg_build' "
        "ORDER BY id",
    )


def grant(client, admin: dict, login: str, role: str, max_level: str, project: str = PROJECT) -> None:
    response = client.put(
        f"/v1/admin/projects/{project}/grants/{login}", json={"role": role, "max_level": max_level}, headers=admin
    )
    assert response.status_code in (200, 201), response.text


def open_hub(db, tmp_path: Path, s3, name: str = "api"):
    """A started app on ``db`` with project alpha registered, alice (writer, internal), carol (reader, public) and
    dave (writer, secret); the TestClient context manager and the namespace."""
    app = create_app(make_config(db, tmp_path / name, s3))
    client = TestClient(app)
    client.__enter__()
    admin = live.bearer(live.insert_token(db, live.ADMIN))
    response = client.put(f"/v1/projects/{PROJECT}", json=REGISTRATION, headers=admin)
    assert response.status_code == 200, response.text
    members = {}
    for login, role, level in (
        ("alice", "writer", "internal"),
        ("carol", "reader", "public"),
        ("dave", "writer", "secret"),
    ):
        grant(client, admin, login, role, level)
        members[login] = live.bearer(live.insert_token(db, login))
    hub = SimpleNamespace(
        client=client,
        app=app,
        db=db,
        s3=s3,
        admin=admin,
        worker_dir=tmp_path / f"{name}-worker",
        api_dir=tmp_path / name,
        **members,
    )
    hub.as_alice = InProcessHub(client, members["alice"])
    return client, hub


@pytest.fixture
def hub(hub_db, tmp_path, s3):
    client, found = open_hub(hub_db, tmp_path, s3)
    try:
        yield found
    finally:
        client.__exit__(None, None, None)


def push(hub, machine: Machine, member: str = "alice"):
    return Pusher(InProcessHub(hub.client, getattr(hub, member)), machine.project()).push()


def build_rows(hub) -> list[dict]:
    response = hub.client.get(f"/v1/kg/{PROJECT}/builds?limit=100", headers=hub.alice)
    assert response.status_code == 200, response.text
    return response.json()["builds"]


def latest(hub) -> dict:
    return build_rows(hub)[0]


def call_tool(hub, tool: str, arguments: dict | None = None, *, member: str = "alice", sink: str = AGENT):
    return hub.client.post(
        f"/v1/kg/{PROJECT}/tools/{tool}",
        json={"arguments": arguments or {}, "sink": sink},
        headers=getattr(hub, member),
    )


def tool_ok(hub, tool: str, arguments: dict | None = None, **options) -> dict:
    response = call_tool(hub, tool, arguments, **options)
    assert response.status_code == 200, response.text
    result = response.json()
    assert not result.get("isError"), result
    return result


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Pushing and building


def test_a_pushed_run_is_built_by_the_worker_and_answered_by_kg_tools(hub, tmp_path):
    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the app works."), doc("setup", "Install it."))

    report = push(hub, laptop)
    assert report.ok, report.errors
    assert len(report.pushed) == 1 and report.present == 0 and report.blobs > 0
    assert report.build["status"] == "queued"
    assert count(hub.db, "kg_ingests") == 1 and count(hub.db, "kg_pending_runs") == 0
    (job,) = jobs(hub.db)
    assert job[1:] == ("todo", "kg:alpha", "kg:alpha")

    run_jobs(hub.db, hub.s3, hub.worker_dir)
    build = latest(hub)
    assert build["status"] == "succeeded", build
    assert build["runs"] == 1 and build["nodes"] > 0 and build["edges"] > 0
    assert build["content_hash"].startswith("sha256:") and build["requested_by"] is None
    artifact = hub.s3.get(blob_key(build["artifact_sha256"]))
    assert (
        artifact is not None and sha(artifact) == build["artifact_sha256"] and len(artifact) == build["artifact_size"]
    )
    assert query(hub.db, "SELECT kind, created_by FROM blobs WHERE sha256 = %s", (build["artifact_sha256"],)) == [
        ("kg-graph", None)
    ]

    found = tool_ok(hub, "kg_search", {"query": "guide"})
    assert found["structuredContent"]["project"] == PROJECT
    assert any(node["name"] == "guide" for node in found["structuredContent"]["results"])
    status = tool_ok(hub, "kg_status")["structuredContent"]
    assert status["graph"]["nodes"] == build["nodes"] and status["graph"]["edges"] == build["edges"]
    assert status["hub_build"]["build_id"] == build["id"] and status["hub_build"]["latest"]
    cached = sorted(p.name for p in (hub.api_dir / "kg" / "graphs" / PROJECT).iterdir())
    assert cached == [f"{build['artifact_sha256']}.sqlite"]


def test_pushing_the_same_run_twice_ingests_it_once(hub, tmp_path):
    laptop = Machine(tmp_path, "laptop")
    (run_id,) = laptop.sync("docs", doc("guide", "How the app works."))
    first = push(hub, laptop)
    assert first.ok and first.pushed == [run_id]
    before = jobs(hub.db)

    again = push(hub, laptop)  # the hub lists the run: nothing is sent
    assert again.ok and again.pushed == [] and again.present == 1 and again.blobs == 0
    # the steps by hand, as a client that lost the answers would send them again
    log = laptop.logs()[0]
    state = hub.as_alice.call("POST", f"/v1/kg/{PROJECT}/runs", {"run_id": run_id, "log_sha256": sha(log.read_bytes())})
    assert state == {
        "run_id": run_id,
        "status": "ingested",
        "log_sha256": sha(log.read_bytes()),
        "blobs": 0,
        "missing": [],
    }
    committed = hub.as_alice.call("POST", f"/v1/kg/{PROJECT}/runs/{run_id}/commit")
    assert committed["created"] is False and committed["queued"] is False and committed["build"] is None

    assert query(hub.db, "SELECT run_id::text FROM kg_ingests") == [(run_id,)]
    assert jobs(hub.db) == before  # no second build was queued
    assert query(hub.db, "SELECT action, target FROM audit WHERE action LIKE 'kg.%%' ORDER BY id") == [
        ("kg.config", PROJECT),
        ("kg.ingest", PROJECT),
    ]


def test_two_machines_pushing_in_either_order_give_the_same_content_hash(hub, tmp_path, s3, monkeypatch):
    laptop, desktop = Machine(tmp_path, "laptop"), Machine(tmp_path, "desktop")
    laptop.sync("docs", doc("guide", "Version from the laptop.", rev="1"), doc("setup", "Install it."))
    laptop.sync("docs", doc("guide", "Second edit on the laptop.", rev="2"), doc("setup", "Install it."))
    desktop.sync("docs", doc("guide", "Edited on the desktop.", rev="3"), doc("faq", "Questions."))

    assert push(hub, laptop).ok and push(hub, desktop).ok  # laptop first
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    forward = latest(hub)
    assert forward["status"] == "succeeded" and forward["runs"] == 3

    other_db = pg.create_database()
    try:
        client, other = open_hub(other_db, tmp_path, s3, name="other")
        try:
            assert push(other, desktop).ok
            run_jobs(other_db, s3, other.worker_dir)
            partial = latest(other)
            assert partial["status"] == "succeeded" and partial["content_hash"] != forward["content_hash"]
            assert push(other, laptop).ok  # desktop first
            run_jobs(other_db, s3, other.worker_dir)
            backward = latest(other)
        finally:
            client.__exit__(None, None, None)
        assert pg.wait_no_backends(other_db.name) == 0
    finally:
        pg.drop_database(other_db)
    assert backward["status"] == "succeeded" and backward["runs"] == 3
    assert backward["content_hash"] == forward["content_hash"]

    # build --verify on the worker's store rebuilds into an empty store and finds the same hash
    monkeypatch.setenv("EVO_KG_HOME", str(hub.worker_dir / "kg" / "home"))
    report = build_project(worker_project(hub.worker_dir, PROJECT), verify=True)
    assert report.ok and report.verify["match"] and report.content_hash == forward["content_hash"]
    env = {**pg.clean_env(), "EVO_KG_HOME": str(hub.worker_dir / "kg" / "home")}
    harness = hub.worker_dir / "kg" / "harness" / PROJECT
    verified = pg.cli(["kg", "build", "--verify", "--cold", "--project", str(harness), "--json"], env=env)
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert json.loads(verified.stdout)["verify"]["match"] is True

    # and a machine holding both corpora builds the same graph locally
    both = Machine(tmp_path, "both")
    corpus = Corpus(PROJECT, both.home)
    for log in laptop.logs() + desktop.logs():
        shutil.copy(log, corpus.log_dir / log.name)
        run_id, records = corpus.run_records(log)
        corpus.merge(records, run_id)
    corpus.db.commit()
    for machine in (laptop, desktop):
        shutil.copytree(machine.home / PROJECT / "blobs", corpus.root / "blobs", dirs_exist_ok=True)
    corpus.close()
    local = build_project(both.project())
    assert local.ok and local.content_hash == forward["content_hash"]
    assert (local.nodes, local.edges) == (forward["nodes"], forward["edges"])


def test_a_run_with_a_label_above_the_hub_sink_is_refused_and_leaves_nothing_behind(hub, tmp_path):
    laptop = Machine(tmp_path, "laptop")
    (secret_run,) = laptop.sync("vault", doc("combination", SECRET_TEXT))
    report = push(hub, laptop)  # the client knows the vault is above the hub sink: its run stays here
    assert report.ok and report.pushed == [] and report.kept == {"vault": 1}
    assert "1 run(s) of vault stay here" in report.summary_line()

    # sent anyway, the hub refuses it whole and deletes what was staged
    (log,) = laptop.logs()
    data = log.read_bytes()
    item = {"sha256": sha(data), "size": len(data), "kind": "kg-log"}
    (ticket,) = hub.as_alice.call("POST", "/v1/blobs/uploads", {"project": PROJECT, "items": [item]})["uploads"]
    assert put_presigned(ticket["url"], data) == 200
    refused = hub.client.post(
        f"/v1/kg/{PROJECT}/runs", json={"run_id": secret_run, "log_upload_id": ticket["upload_id"]}, headers=hub.alice
    )
    assert refused.status_code == 422
    assert refused.json()["message"] == (
        "source vault is labelled secret, above what hub sink 'hub' clears: nothing of this run was written"
    )

    # an item raising its own label is refused too, naming the item, never its text
    (raised_run,) = laptop.sync(
        "docs", doc("public-page", "Fine to share."), doc("salaries", SECRET_TEXT, label={"level": "customer"})
    )
    report = push(hub, laptop)
    (error,) = report.errors
    assert error.startswith(f"run {raised_run}: 1 item(s) of run {raised_run} carry labels hub sink 'hub' does not")
    assert "docs:doc:salaries (customer)" in error and SECRET_TEXT not in error

    assert count(hub.db, "kg_ingests") == 0 and count(hub.db, "kg_pending_runs") == 0
    assert jobs(hub.db) == [] and count(hub.db, "kg_builds") == 0
    assert count(hub.db, "blob_uploads") == 0
    assert query(hub.db, "SELECT count(*) FROM blobs WHERE kind = 'kg-log'") == [(0,)]
    assert hub.s3.keys("uploads/") == []  # the staged objects and their sealed copies are gone
    for log in laptop.logs():
        assert hub.s3.get(blob_key(sha(log.read_bytes()))) is None
    assert SECRET_TEXT not in live.table_dump(hub.db)


def test_committing_a_run_while_blobs_are_missing_is_422(hub, tmp_path):
    laptop = Machine(tmp_path, "laptop")
    (run_id,) = laptop.sync("docs", doc("guide", "How the app works."))
    (log,) = laptop.logs()
    data = log.read_bytes()
    asked = hub.as_alice.call(
        "POST",
        "/v1/blobs/uploads",
        {"project": PROJECT, "items": [{"sha256": sha(data), "size": len(data), "kind": "kg-log"}]},
    )
    (ticket,) = asked["uploads"]
    assert put_presigned(ticket["url"], data) == 200
    from evo_agents.hub.kg_ingest import knowledge_config

    hub.as_alice.call("PUT", f"/v1/kg/{PROJECT}/config", knowledge_config(laptop.project()))
    state = hub.as_alice.call(
        "POST", f"/v1/kg/{PROJECT}/runs", {"run_id": run_id, "log_upload_id": ticket["upload_id"]}
    )
    assert state["status"] == "pending" and state["missing"] and len(state["missing"]) == state["blobs"]
    assert hub.as_alice.call("GET", f"/v1/kg/{PROJECT}/runs") == {"ingested": [], "pending": [run_id]}

    refused = hub.client.post(f"/v1/kg/{PROJECT}/runs/{run_id}/commit", headers=hub.alice)
    assert refused.status_code == 422
    body = refused.json()
    assert body["error"] == "invalid_request" and "nothing was written" in body["message"]
    assert sorted(d["sha256"] for d in body["detail"]) == state["missing"]
    assert count(hub.db, "kg_ingests") == 0 and jobs(hub.db) == []

    # blobs/check agrees, and after the blobs are committed the run commits
    check = hub.as_alice.call("POST", f"/v1/kg/{PROJECT}/blobs/check", {"sha256": state["missing"]})
    assert check == {"missing": state["missing"]}
    report = push(hub, laptop)  # finishes the run: its log is held, so it is named by hash
    assert report.ok and report.pushed == [run_id] and report.blobs == len(state["missing"])
    assert hub.as_alice.call("POST", f"/v1/kg/{PROJECT}/blobs/check", {"sha256": state["missing"]}) == {"missing": []}
    assert count(hub.db, "kg_ingests") == 1 and count(hub.db, "kg_pending_runs") == 0


def test_three_builds_queued_in_a_row_never_run_two_at_once_and_queue_at_most_one(hub, tmp_path, monkeypatch):
    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the app works."))
    assert push(hub, laptop).ok
    original = hub_kg_build.build_graph

    def slow_build(project):
        time.sleep(1.5)
        return original(project)

    monkeypatch.setattr(hub_kg_build, "build_graph", slow_build)
    seen: list[tuple[int, int]] = []
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            rows = query(
                hub.db,
                "SELECT count(*) FILTER (WHERE status = 'doing'), count(*) FILTER (WHERE status = 'todo') "
                "FROM procrastinate_jobs WHERE lock = 'kg:alpha'",
            )
            seen.append(rows[0])
            time.sleep(0.05)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    with BackgroundWorker(hub.db, hub.s3, hub.worker_dir, concurrency=3):
        deadline = time.monotonic() + 30
        while not query(hub.db, "SELECT count(*) FROM procrastinate_jobs WHERE status = 'doing' AND lock = 'kg:alpha'")[
            0
        ][0]:
            assert time.monotonic() < deadline, "the build of the pushed run did not start"
            time.sleep(0.05)
        answers = []
        for _ in range(3):  # three in a row while the first build runs
            response = hub.client.post(f"/v1/kg/{PROJECT}/builds", headers=hub.alice)
            assert response.status_code == 202, response.text
            answers.append(response.json())
        deadline = time.monotonic() + 60
        while query(
            hub.db, "SELECT count(*) FROM procrastinate_jobs WHERE status IN ('todo', 'doing') AND lock = 'kg:alpha'"
        )[0][0]:
            assert time.monotonic() < deadline, jobs(hub.db)
            time.sleep(0.1)
    stop.set()
    watcher.join()

    assert seen and max(doing for doing, _ in seen) == 1 and max(todo for _, todo in seen) == 1
    assert [a["queued"] for a in answers] == [True, False, False]
    assert len({a["build"]["id"] for a in answers}) == 1  # the two refused ones name the waiting build
    finished = query(hub.db, "SELECT status, started_at, finished_at FROM kg_builds ORDER BY id")
    assert [row[0] for row in finished] == ["succeeded", "succeeded"]
    (_, _, first_done), (_, second_started, _) = finished
    assert second_started >= first_done  # one after the other
    assert [row[1] for row in jobs(hub.db)] == ["succeeded", "succeeded"]
    events = query(
        hub.db,
        "SELECT e.job_id, e.type::text, e.at FROM procrastinate_events e JOIN procrastinate_jobs j ON j.id = e.job_id "
        "WHERE j.lock = 'kg:alpha' AND e.type IN ('started', 'succeeded') ORDER BY e.at",
    )
    running = 0
    for _, kind, _ in events:
        running += 1 if kind == "started" else -1
        assert running <= 1


def test_deleting_the_worker_cache_and_building_again_gives_the_same_content_hash(hub, tmp_path, caplog):
    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the app works."), doc("setup", "Install it."))
    laptop.sync("docs", doc("guide", "Edited.", rev="2"), doc("setup", "Install it."))
    assert push(hub, laptop).ok
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    first = latest(hub)
    assert first["status"] == "succeeded"

    shutil.rmtree(hub.worker_dir)
    response = hub.client.post(f"/v1/kg/{PROJECT}/builds", headers=hub.alice)
    assert response.status_code == 202 and response.json()["queued"] is True
    with caplog.at_level("INFO", logger="evo_agents.hub.kg_build"):
        run_jobs(hub.db, hub.s3, hub.worker_dir)
    second = latest(hub)
    assert second["id"] != first["id"] and second["status"] == "succeeded"
    assert second["content_hash"] == first["content_hash"]
    assert (second["nodes"], second["edges"]) == (first["nodes"], first["edges"])
    assert second["requested_by"] == "alice"
    (done,) = [r for r in caplog.records if r.getMessage() == "kg build succeeded"]
    assert done.merged == 2  # every log came back from the blob store
    assert len(list((hub.worker_dir / "kg" / "home" / PROJECT / "log").iterdir())) == 2


def test_an_artifact_that_does_not_match_its_sha256_is_never_opened(hub, tmp_path):
    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the app works."))
    assert push(hub, laptop).ok
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    build = latest(hub)
    good = hub.s3.get(blob_key(build["artifact_sha256"]))
    hub.s3.put(blob_key(build["artifact_sha256"]), bytes(reversed(good)))  # same size, other bytes

    refused = call_tool(hub, "kg_search", {"query": "guide"})
    assert refused.status_code == 502
    assert "does not have the SHA-256 the build recorded" in refused.json()["message"]
    graphs = hub.api_dir / "kg" / "graphs" / PROJECT
    assert not graphs.exists() or list(graphs.iterdir()) == []  # neither the file nor a part of it stays

    hub.s3.put(blob_key(build["artifact_sha256"]), good)
    assert tool_ok(hub, "kg_search", {"query": "guide"})["structuredContent"]["results"]


def test_kg_tools_answer_from_the_cached_graph_when_the_blob_store_is_down(hub, tmp_path):
    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the app works."), doc("setup", "Install it."))
    assert push(hub, laptop).ok
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    build = latest(hub)
    before = tool_ok(hub, "kg_search", {"query": "guide"})

    hub.s3.stop()
    assert tool_ok(hub, "kg_search", {"query": "guide"}) == before
    # a newer build whose graph is not cached yet: the cached one answers, and kg_status says so
    query(
        hub.db,
        "INSERT INTO kg_builds (project_id, status, artifact_sha256, artifact_size, content_hash, nodes, edges, "
        "started_at, finished_at) SELECT id, 'succeeded', %s, 10, %s, 1, 0, now(), now() FROM projects WHERE name = %s",
        ("f" * 64, "sha256:" + "f" * 64, PROJECT),
    )
    assert (
        tool_ok(hub, "kg_search", {"query": "guide"})["structuredContent"]["results"]
        == (before["structuredContent"]["results"])
    )
    status = tool_ok(hub, "kg_status")
    assert status["structuredContent"]["hub_build"] == {
        "build_id": build["id"],
        "artifact_sha256": build["artifact_sha256"],
        "content_hash": build["content_hash"],
        "finished_at": status["structuredContent"]["hub_build"]["finished_at"],
        "latest": False,
    }
    assert "the blob store did not answer" in status["content"][0]["text"]

    shutil.rmtree(hub.api_dir / "kg" / "graphs")  # nothing cached and the store down: 503, not a guess
    unavailable = call_tool(hub, "kg_search", {"query": "guide"})
    assert unavailable.status_code == 503 and "no graph of project alpha is cached" in unavailable.json()["message"]


# Failures and refusals


def test_a_failed_build_is_recorded_and_the_worker_goes_on_with_the_next_job(hub, tmp_path, caplog):
    beta = dict(REGISTRATION, harness={"name": "beta", "workspace": "~/ws", "path": "beta-harness"})
    assert hub.client.put("/v1/projects/beta", json=beta, headers=hub.admin).status_code == 200
    grant(hub.client, hub.admin, "alice", "writer", "internal", project="beta")
    queued = hub.client.post("/v1/kg/beta/builds", headers=hub.alice)  # beta has no config: its build must fail
    assert queued.status_code == 202, queued.text
    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the app works."))
    assert push(hub, laptop).ok
    (log,) = laptop.logs()
    hub.s3.client().delete_object(Bucket=hub.s3.bucket, Key=blob_key(sha(log.read_bytes())))

    with caplog.at_level("INFO", logger="evo_agents.hub.kg_build"):
        run_jobs(hub.db, hub.s3, hub.worker_dir)  # returns: the worker survived both failures
    (beta_build,) = hub.as_alice.call("GET", "/v1/kg/beta/builds")["builds"]
    assert beta_build["status"] == "failed" and beta_build["finished_at"]
    assert beta_build["error"] == "project beta has no knowledge config on the hub yet: run `evo-agents hub kg push`"
    alpha_build = latest(hub)
    assert alpha_build["status"] == "failed" and alpha_build["artifact_sha256"] is None
    assert alpha_build["error"] == f"the log of run {log.name[:-9]} {sha(log.read_bytes())} is not in the blob store"
    assert [r.getMessage() for r in caplog.records].count("kg build failed") == 2

    hub.s3.put(blob_key(sha(log.read_bytes())), log.read_bytes())
    assert hub.client.post(f"/v1/kg/{PROJECT}/builds", headers=hub.alice).json()["queued"] is True
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    assert latest(hub)["status"] == "succeeded"
    assert [j[1] for j in jobs(hub.db)] == ["succeeded"] * 3


def test_the_knowledge_config_must_fit_the_registered_project(hub, tmp_path):
    from evo_agents.hub.kg_ingest import knowledge_config

    config = knowledge_config(Machine(tmp_path, "laptop").project())
    path = f"/v1/kg/{PROJECT}/config"
    assert config["knowledge"]["sources"][0] == {
        "id": "docs",
        "connector": "python:tests.kg.fakes:run",
        "label": {"level": "internal", "integrity": "U"},
    }  # config and deletion stay on the machine
    saved = hub.client.put(path, json=config, headers=hub.alice)
    assert saved.status_code == 200 and saved.json()["changed"] is True
    again = hub.client.put(path, json=config, headers=hub.alice)
    assert again.json() == {"digest": saved.json()["digest"], "changed": False}
    assert query(hub.db, "SELECT count(*) FROM audit WHERE action = 'kg.config'") == [(1,)]

    def refused(body, member="alice", status=422) -> str:
        response = hub.client.put(path, json=body, headers=getattr(hub, member))
        assert response.status_code == status, response.text
        return response.json()["message"]

    assert "needs the writer role" in refused(config, "carol", 403)
    ladder = json.loads(json.dumps(config))
    ladder["knowledge"]["policy"]["levels"] = ["public", "secret"]
    assert "differ from the registered" in refused(ladder)
    leaked = json.loads(json.dumps(config))
    leaked["knowledge"]["sources"][0]["config"] = {"token_env": "LARK_TOKEN"}
    assert "config must stay on the machine" in refused(leaked)
    bad = json.loads(json.dumps(config))
    bad["knowledge"]["identifiers"] = [{"kind": "Requirement", "pattern": "KB-("}]
    assert "pattern does not compile" in refused(bad)

    # a source the hub holds runs of may not be raised above the hub sink afterwards
    laptop = Machine(tmp_path, "desk")
    laptop.sync("docs", doc("guide", "How the app works."))
    assert push(hub, laptop).ok
    raised = json.loads(json.dumps(config))
    raised["knowledge"]["sources"][0]["label"] = {"level": "customer"}
    assert "sources docs would be labelled above" in refused(raised)

    # a project without a hub sink takes no push at all
    gamma = dict(REGISTRATION, sinks=REGISTRATION["sinks"][:1], harness={"name": "g", "workspace": "~/ws", "path": "g"})
    assert hub.client.put("/v1/projects/gamma", json=gamma, headers=hub.admin).status_code == 200
    grant(hub.client, hub.admin, "alice", "writer", "internal", project="gamma")
    response = hub.client.put(
        "/v1/kg/gamma/config",
        json=dict(config, knowledge=dict(config["knowledge"], project="gamma")),
        headers=hub.alice,
    )
    assert response.status_code == 422 and "declares no sink of kind hub" in response.json()["message"]


def test_logs_that_are_not_a_run_of_the_project_are_refused_and_discarded(hub, tmp_path):
    laptop = Machine(tmp_path, "laptop")
    (run_id,) = laptop.sync("docs", doc("guide", "How the app works."))
    from evo_agents.hub.kg_ingest import knowledge_config

    hub.as_alice.call("PUT", f"/v1/kg/{PROJECT}/config", knowledge_config(laptop.project()))
    (log,) = laptop.logs()

    def staged(data: bytes, kind: str = "kg-log") -> str:
        item = {"sha256": sha(data), "size": len(data), "kind": kind}
        (ticket,) = hub.as_alice.call("POST", "/v1/blobs/uploads", {"project": PROJECT, "items": [item]})["uploads"]
        assert put_presigned(ticket["url"], data) == 200
        return ticket["upload_id"]

    def refused(run: str, upload_id: str) -> str:
        response = hub.client.post(
            f"/v1/kg/{PROJECT}/runs", json={"run_id": run, "log_upload_id": upload_id}, headers=hub.alice
        )
        assert response.status_code == 422, response.text
        return response.json()["message"]

    assert "not a complete jsonl.gz file" in refused(run_id, staged(b"not gzip at all"))
    assert "it is cut short" in refused(run_id, staged(log.read_bytes()[:-20]))
    other = "0190f5e0-0000-7000-8000-000000000001"
    assert f"is the log of run {run_id}, not of run {other}" in refused(other, staged(log.read_bytes()))
    lines = [json.loads(line) for line in gzip.decompress(log.read_bytes()).splitlines()]
    lines[0]["project"] = "beta"
    foreign = gzip.compress(b"".join(json.dumps(line).encode() + b"\n" for line in lines))
    assert "a run of project beta, not of alpha" in refused(run_id, staged(foreign))
    assert "unknown uploads" in refused(run_id, staged(log.read_bytes() + b"\n", kind="kg-blob"))
    both = hub.client.post(
        f"/v1/kg/{PROJECT}/runs",
        json={"run_id": run_id, "log_upload_id": staged(log.read_bytes()), "log_sha256": "0" * 64},
        headers=hub.alice,
    )
    assert both.status_code == 422 and both.json()["detail"][0]["msg"].endswith(
        "give exactly one of log_upload_id and log_sha256"
    )
    unheld = hub.client.post(
        f"/v1/kg/{PROJECT}/runs", json={"run_id": run_id, "log_sha256": "0" * 64}, headers=hub.alice
    )
    assert unheld.status_code == 422 and "holds no blob" in unheld.json()["message"]

    assert count(hub.db, "kg_pending_runs") == 0 and count(hub.db, "kg_ingests") == 0 and jobs(hub.db) == []
    # every refused log upload was deleted; the kg-blob one and the last kg-log one were never committed
    assert query(hub.db, "SELECT kind FROM blob_uploads ORDER BY kind") == [("kg-blob",), ("kg-log",)]
    assert len(hub.s3.keys("uploads/")) == 2
    assert push(hub, laptop).ok  # the real log still goes through


def test_reading_follows_the_grant_and_the_sink(hub, tmp_path, monkeypatch):
    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the app works."), doc("setup", "Install it."))
    assert push(hub, laptop).ok
    run_jobs(hub.db, hub.s3, hub.worker_dir)

    assert tool_ok(hub, "kg_search", {"query": "guide"})["structuredContent"]["results"]
    # carol's grant stops at public, the docs are internal: she sees nothing, exactly as if there were nothing
    assert tool_ok(hub, "kg_search", {"query": "guide"}, member="carol")["structuredContent"]["results"] == []
    undeclared = call_tool(hub, "kg_search", {"query": "guide"}, sink="codex@openai").json()
    assert undeclared["isError"] and "not declared" in undeclared["content"][0]["text"]
    cli = tool_ok(hub, "kg_status", sink="cli")  # no cli sink declared: read through the hub sink
    assert cli["structuredContent"]["sink"]["clearance"]["level"] == "internal"
    stranger = hub.client.post(
        f"/v1/kg/{PROJECT}/tools/kg_search",
        json={"arguments": {"query": "x"}},
        headers=live.bearer(live.insert_token(hub.db, "eve")),
    )
    assert stranger.status_code == 404
    unknown = hub.client.post(f"/v1/kg/{PROJECT}/tools/kg_reveal", json={}, headers=hub.alice)
    assert unknown.status_code == 422

    # kg_more continues a cut result across requests, for its owner only
    monkeypatch.setattr(serve, "CAP_CHARS", 300)
    cut = tool_ok(hub, "kg_context", {"query": "guide", "budget_tokens": 4000})
    handle = cut["structuredContent"]["handle"]
    other = call_tool(hub, "kg_more", {"handle": handle}, member="dave").json()
    assert other["isError"] and "unknown or expired handle" in other["content"][0]["text"]
    parts = [cut["content"][0]["text"]]
    while True:
        more = tool_ok(hub, "kg_more", {"handle": handle})
        parts.append(more["content"][0]["text"])
        if not more["structuredContent"]["remaining"]:
            break
    assert call_tool(hub, "kg_more", {"handle": handle}).json()["isError"]  # read to the end, then gone


def test_hub_tools_give_the_local_server_s_answers(hub, tmp_path):
    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the app works."), doc("setup", "Install it, see guide."))
    assert push(hub, laptop).ok
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    report = build_project(laptop.project())
    assert report.content_hash == latest(hub)["content_hash"]
    local = Session(laptop.project(), AGENT)

    def same(tool: str, arguments: dict) -> dict:
        here = local.call(tool, dict(arguments))
        there = tool_ok(hub, tool, arguments)
        assert {k: v for k, v in there["structuredContent"].items() if k != "build"} == {
            k: v for k, v in here["structuredContent"].items() if k != "build"
        }
        return there

    found = same("kg_search", {"query": "guide"})
    first = found["structuredContent"]["results"][0]["id"]
    same("kg_node", {"id": first})
    same("kg_context", {"ids": [first]})
    same("kg_path", {"from": first})
    same("kg_impact", {"ids": [first]})
    status = tool_ok(hub, "kg_status")["structuredContent"]["graph"]
    assert (status["nodes"], status["edges"], status["content_hash"]) == (
        report.nodes,
        report.edges,
        report.content_hash,
    )


def test_tools_of_a_project_without_a_build_say_how_to_get_one(hub):
    result = call_tool(hub, "kg_search", {"query": "x"}).json()
    assert result["isError"] and "has no graph on the hub yet" in result["content"][0]["text"]


def test_runs_pushed_while_a_build_waits_join_that_build_and_carry_no_content(hub, tmp_path, caplog):
    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "Body text that stays in the blob store."))
    laptop.sync("docs", doc("guide", "Second body text that stays in the blob store.", rev="2"))
    with caplog.at_level("INFO"):
        report = push(hub, laptop)
    assert report.ok and len(report.pushed) == 2
    assert count(hub.db, "kg_ingests") == 2
    (job,) = jobs(hub.db)  # the second commit found the first build waiting: refused defer, transaction intact
    (build,) = query(hub.db, "SELECT id, job_id, status FROM kg_builds")
    assert build[1] == job[0] and report.build["id"] == build[0] and report.build["status"] == "queued"
    with caplog.at_level("INFO"):
        run_jobs(hub.db, hub.s3, hub.worker_dir)
    assert latest(hub)["runs"] == 2
    dump = live.table_dump(hub.db)
    logged = "\n".join(r.getMessage() + json.dumps(r.__dict__, default=str) for r in caplog.records)
    for text in ("Body text that stays", "Second body text"):
        assert text not in dump and text not in logged
    assert query(hub.db, "SELECT DISTINCT target FROM audit WHERE action LIKE 'kg.%%'") == [(PROJECT,)]


# The CLI against `hub serve`


def sign_in(home: Path, url: str, login: str, token: str) -> Path:
    directory = home / ".evo" / "hub"
    directory.mkdir(parents=True)
    (directory / "token").write_text(token + "\n", encoding="utf-8")
    (directory / "config.json").write_text(json.dumps({"url": url, "login": login}), encoding="utf-8")
    return home


def mcp(args: list[str], env: dict, *messages: dict) -> list[dict]:
    """``evo-agents ARGS`` fed ``messages`` as MCP over stdin; the answers."""
    stdin = "".join(json.dumps(m) + "\n" for m in messages)
    result = subprocess.run(
        [sys.executable, "-m", "evo_agents", *args], env=env, input=stdin, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def ok(result: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def test_the_cli_pushes_builds_and_reads_through_hub_serve(hub_db, tmp_path, s3):
    laptop = Machine(tmp_path, "laptop")
    laptop.files["docs"].write_text(
        json.dumps({"items": [doc("guide", "How the app works."), doc("setup", "Install it.")]}), encoding="utf-8"
    )
    with live.running_hub(hub_db, tmp_path, EVO_HUB_ADMINS=live.ADMIN, **s3.env()) as served:
        admin = Hub(served.url, live.insert_token(hub_db, live.ADMIN))
        admin.call("PUT", f"/v1/projects/{PROJECT}", REGISTRATION)
        admin.call("PUT", f"/v1/admin/projects/{PROJECT}/grants/alice", {"role": "writer", "max_level": "internal"})
        home = sign_in(tmp_path / "home", served.url, "alice", live.insert_token(hub_db, "alice"))
        env = pg.clean_env(HOME=str(home), EVO_KG_HOME=str(laptop.home))
        harness = str(laptop.harness)

        synced = ok(pg.cli(["kg", "sync", "--project", harness, "--build", "--push", "--json"], env=env))
        pushed = json.loads(synced.stdout)["push"]
        assert pushed["ok"] and len(pushed["projects"][0]["pushed"]) == 1
        again = ok(pg.cli(["hub", "kg", "push", "--project", harness], env=env))
        assert again.stdout == (
            "project alpha: pushed 0 run(s) and 0 blob(s); 1 of 2 run(s) were on the hub already; 1 run(s) of vault "
            "stay here, above what the hub sink clears\n"
        )
        listed = json.loads(ok(pg.cli(["hub", "kg", "builds", "--project", PROJECT, "--json"], env=env)).stdout)
        assert [b["status"] for b in listed["builds"]] == ["queued"] and listed["jobs"][0]["status"] == "todo"

        with BackgroundWorker(hub_db, s3, tmp_path / "worker", concurrency=1):
            built = ok(pg.cli(["hub", "kg", "build", "--project", PROJECT, "--wait", "--json"], env=env))
        answer = json.loads(built.stdout)
        assert answer["build"]["status"] == "succeeded" and answer["build"]["runs"] == 1
        table = ok(pg.cli(["hub", "kg", "builds", "--project", PROJECT], env=env)).stdout
        assert "succeeded" in table and answer["build"]["content_hash"][:19] in table

        # the hub answers kg_status with the counts of the graph this machine built from the same corpus
        local = json.loads(pg.cli(["kg", "status", "--project", harness, "--json"], env=env).stdout)["graph"]
        remote = ok(pg.cli(["kg", "query", "kg_status", "--backend", "hub", "--project", PROJECT, "--json"], env=env))
        remote = json.loads(remote.stdout)
        assert remote["backend"] == "hub" and (remote["graph"]["nodes"], remote["graph"]["edges"]) == (
            local["nodes"],
            local["edges"],
        )
        assert remote["graph"]["content_hash"] == local["content_hash"] == answer["build"]["content_hash"]

        # kg serve: the same seven tools whichever backend answers
        hello = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
        tools = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        status = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kg_status", "arguments": {}}}
        search = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "kg_search", "arguments": {"query": "guide"}},
        }
        served_hub = mcp(["kg", "serve", "--backend", "hub", "--project", PROJECT], env, hello, tools, status, search)
        assert [t["name"] for t in served_hub[1]["result"]["tools"]] == [t["name"] for t in serve.TOOLS]
        assert served_hub[2]["result"]["structuredContent"]["backend"] == "hub"
        served_local = mcp(
            ["kg", "serve", "--backend", "local", "--project", harness], env, hello, tools, status, search
        )
        assert "backend" not in served_local[2]["result"]["structuredContent"]
        assert (
            served_hub[3]["result"]["structuredContent"]["results"]
            == served_local[3]["result"]["structuredContent"]["results"]
        )
        auto = mcp(["kg", "serve", "--project", harness], env, status)  # signed in, and the hub has a graph
        assert auto[0]["result"]["structuredContent"]["backend"] == "hub"
        signed_out = pg.clean_env(HOME=str(tmp_path / "nobody"), EVO_KG_HOME=str(laptop.home))
        auto = mcp(["kg", "serve", "--project", harness], signed_out, status)
        assert "backend" not in auto[0]["result"]["structuredContent"]

        # --all pushes what the hub takes and says why it skipped the rest
        zeta = Machine(tmp_path, "zeta", project="zeta")
        zeta_env = dict(env)
        ok(pg.cli(["kg", "sync", "--project", str(zeta.harness)], env=zeta_env))
        everything = ok(pg.cli(["hub", "kg", "push", "--all"], env=env))
        assert "project alpha: pushed 0 run(s)" in everything.stdout
        assert "project zeta: skipped, not on the hub, or not visible to you" in everything.stdout
        due = ok(pg.cli(["kg", "sync", "--due", "--all", "--build", "--push"], env=env))
        assert "push project alpha: pushed 0 run(s)" in due.stdout

        served.proc.terminate()
        served.proc.wait(timeout=30)
        down = mcp(["kg", "serve", "--backend", "hub", "--project", PROJECT], env, search)
        assert down[0]["result"]["isError"] and served.url in down[0]["result"]["content"][0]["text"]
        auto = mcp(["kg", "serve", "--project", harness], env, status)  # the hub does not answer: local
        assert "backend" not in auto[0]["result"]["structuredContent"]
    for secret in (s3.secret_access_key, s3.access_key_id):
        assert secret not in served.log()


def test_a_build_whose_worker_died_is_failed_and_queued_again(hub, tmp_path):
    from evo_agents.hub import jobs as hub_jobs
    from evo_agents.hub.db import open_pool
    from evo_agents.hub.jobs import JobQueue
    from evo_agents.hub.kg_build import STOPPED, recover_stalled

    periodic = {p.task.name: p for p in queue.periodic_registry.periodic_tasks.values()}
    assert periodic[hub_jobs.RECOVER_KG_BUILDS].cron == "*/5 * * * *"
    assert queue.tasks[hub_jobs.RECOVER_KG_BUILDS].queueing_lock == hub_jobs.RECOVER_KG_BUILDS

    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the app works."))
    assert push(hub, laptop).ok
    ((job_id, *_),) = jobs(hub.db)
    # a worker took the build and was killed: the job stays doing, held by no live worker, with the project's lock
    query(hub.db, "UPDATE procrastinate_jobs SET status = 'doing' WHERE id = %s", (job_id,))
    query(hub.db, "UPDATE kg_builds SET status = 'running', started_at = now() WHERE job_id = %s", (job_id,))

    async def recover():
        pool = await open_pool(make_config(hub.db, tmp_path / "recover", hub.s3))
        try:
            job_queue = await JobQueue.open(pool)
            return await recover_stalled(SimpleNamespace(pool=pool), job_queue._manager)
        finally:
            await pool.close()

    assert asyncio.run(recover()) == {"failed": 1, "queued": [PROJECT]}
    first, second = query(hub.db, "SELECT status, error, job_id FROM kg_builds ORDER BY id")
    assert first == ("failed", STOPPED, job_id)
    assert second[0] == "queued" and [row[1] for row in jobs(hub.db)] == ["failed", "todo"]
    assert asyncio.run(recover()) == {"failed": 0, "queued": []}
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    assert latest(hub)["status"] == "succeeded"


def test_a_build_the_worker_stops_is_recorded_and_queued_again(hub, tmp_path, monkeypatch):
    from evo_agents.hub.db import open_pool
    from evo_agents.hub.jobs import JobQueue
    from evo_agents.hub.kg_build import STOPPED, run_build

    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the app works."))
    assert push(hub, laptop).ok
    ((job_id, *_),) = jobs(hub.db)
    ((build_id,),) = query(hub.db, "SELECT id FROM kg_builds")
    query(hub.db, "UPDATE procrastinate_jobs SET status = 'doing' WHERE id = %s", (job_id,))  # a worker took it
    original = hub_kg_build.build_graph
    monkeypatch.setattr(hub_kg_build, "build_graph", lambda project: (time.sleep(1.0), original(project))[1])

    async def stop_during_the_build():
        config = make_config(hub.db, hub.worker_dir, hub.s3)
        pool = await open_pool(config)
        store = BlobStore.from_config(config)
        try:
            job_queue = await JobQueue.open(pool)
            context = HubContext(config, pool, store, config.data_dir)
            task = asyncio.ensure_future(run_build(context, PROJECT, build_id, job_id, job_queue._manager))
            await asyncio.sleep(0.5)
            task.cancel()  # what procrastinate does to a job still running when the grace period ends
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await pool.close()
            store.close()

    asyncio.run(stop_during_the_build())
    stopped, again = query(hub.db, "SELECT status, error FROM kg_builds ORDER BY id")
    assert stopped == ("failed", STOPPED) and again == ("queued", None)
    query(hub.db, "UPDATE procrastinate_jobs SET status = 'aborted' WHERE id = %s", (job_id,))  # as the worker ends it
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    assert latest(hub)["status"] == "succeeded"

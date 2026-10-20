"""The routes the worker on duty pushes a project's graph through (``evo_agents.hub.server.kg_worker``), the projects
the heartbeat names to it (``kg_duties``), the report it keeps on the worker (workers.kg_sync, schema 0053) and in the
night's figures, against a real hub: Postgres, moto's S3 and the hub's build job in this process.

Checked: only the worker that serves the project and that its night shift names reaches the routes, a machine token
gets the guard's 403, and a worker uploads kg blobs alone and sends no config; the heartbeat answer names the duty
projects only to a daemon that says kg_sync; the report shows on GET /v1/workers/{id} until a heartbeat sends none and
in the night's figures with the sync that failed; and a member's laptop and the worker pushing one git source in turn,
the laptop once from a stale clone, leave the hub's built corpus with the files of the newest commit alone, the
worker's runs pushed as its owner with its token."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from evo_agents.hub import kg_push, review, runs, tables
from evo_agents.hub.server import collect
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.collect import kg_sync_figures
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import MACHINE, Principal
from evo_agents.kg.corpus import Corpus
from evo_agents.kg.project import load_project_at
from evo_agents.kg.sync import sync_source
from evo_agents.worker.kg_sync import KgSync
from tests.hub.test_kg import InProcessHub, make_config, run_jobs
from tests.hub.test_runs import HOST, PROTOCOL
from tests.worker.test_kg_sync import HARNESS, KNOWLEDGE, PROJECT, T, Upstream, git

OTHER = "elsewhere"
OWNER, LAPTOP, OUTSIDER = "otto", "wendy", "ursula"


def registration(name: str) -> dict:
    return {
        "levels": ["public", "internal"],
        "locations": ["any"],
        "default_label": {"level": "internal"},
        "sinks": [{"id": "hub", "kind": "hub", "clearance": {"level": "internal"}}],
        "repos": [],
        "harness": {"name": name, "workspace": "~/ws", "path": f"{name}-harness"},
    }


def ok(response, status: int = 200) -> dict:
    assert response.status_code == status, response.text
    return response.json()


def add_worker(client, headers: dict, name: str, projects: list[str]) -> dict:
    answer = ok(client.post("/v1/workers", json={"name": name, "projects": projects, **HOST}, headers=headers), 201)
    return {
        "id": answer["worker"]["id"],
        "token_id": answer["token_id"],
        "headers": {**live.bearer(answer["token"]), **PROTOCOL},
    }


def beat(client, worker: dict, **extra) -> dict:
    body = {"runtimes": {}, "checkouts": {}, "free_slots": 1, "runs": [], **extra}
    return ok(client.post("/v1/worker/heartbeat", json=body, headers=worker["headers"]))


def ids(db, table, column, value) -> int:
    return live.sql(db, select(table.c.id).where(column == value))[0][0]


@pytest.fixture
def hub(hub_db, s3, tmp_path):
    app = create_app(make_config(hub_db, tmp_path / "api", s3))
    with TestClient(app) as client:
        admin = live.bearer(live.insert_token(hub_db, live.ADMIN))
        for name in (PROJECT, OTHER):
            ok(client.put(f"/v1/projects/{name}", json=registration(name), headers=admin))
        members = {login: live.bearer(live.insert_token(hub_db, login)) for login in (OWNER, LAPTOP, OUTSIDER)}
        for name in (PROJECT, OTHER):
            for login in (OWNER, LAPTOP):
                grant = {"role": "writer", "max_level": "internal"}
                ok(client.put(f"/v1/admin/projects/{name}/grants/{login}", json=grant, headers=admin))
        duty = add_worker(client, members[OWNER], "night-mac", [PROJECT])
        spare = add_worker(client, members[OWNER], "spare-mac", [PROJECT])
        outsider = add_worker(client, members[OWNER], "other-mac", [OTHER])
        schedules = tables.schedules
        project_id = ids(hub_db, tables.projects, tables.projects.c.name, PROJECT)
        owner_id = ids(hub_db, tables.users, tables.users.c.login, OWNER)
        values = {"project_id": project_id, "kind": "night_shift", "owner_id": owner_id, "worker_id": duty["id"]}
        live.sql(hub_db, insert(schedules).values(**values))
        yield SimpleNamespace(
            client=client, db=hub_db, s3=s3, tmp=tmp_path, admin=admin, duty=duty, spare=spare, outsider=outsider,
            project_id=project_id, owner_id=owner_id, **members,
        )  # fmt: skip


def runs_path(project: str = PROJECT) -> str:
    return f"/v1/worker/kg/{project}/runs"


def test_kg_api_worker_routes_take_only_the_worker_on_duty(hub):
    client = hub.client
    assert ok(client.get(runs_path(), headers=hub.duty["headers"])) == {"ingested": [], "pending": []}
    refused = client.get(runs_path(), headers=hub.spare["headers"])
    assert refused.status_code == 403 and "is not on duty for the night shift" in refused.json()["message"]
    refused = client.get(runs_path(), headers=hub.outsider["headers"])
    assert refused.status_code == 403 and "does not serve project" in refused.json()["message"]
    refused = client.get(runs_path(OTHER), headers=hub.outsider["headers"])
    assert refused.status_code == 403 and "is not on duty" in refused.json()["message"], "it serves it, not on duty"
    assert client.get(runs_path(), headers={**hub.otto, **PROTOCOL}).status_code == 403, "a machine token"
    assert client.get(f"/v1/kg/{PROJECT}/runs", headers=hub.duty["headers"]).status_code == 403, "outside /v1/worker"

    item = {"sha256": "a" * 64, "size": 10, "kind": "skill-bundle"}
    body = {"project": PROJECT, "items": [item]}
    refused = client.post(f"/v1/worker/kg/{PROJECT}/blobs/uploads", json=body, headers=hub.duty["headers"])
    assert refused.status_code == 422 and "kg-blob and kg-log only" in refused.json()["message"]
    body = {"project": OTHER, "items": [{**item, "kind": "kg-blob"}]}
    refused = client.post(f"/v1/worker/kg/{PROJECT}/blobs/uploads", json=body, headers=hub.duty["headers"])
    assert refused.status_code == 422 and "names project elsewhere" in refused.json()["message"]
    config = {"knowledge": {}, "ontology": None}
    assert client.put(f"/v1/worker/kg/{PROJECT}/config", json=config, headers=hub.duty["headers"]).status_code in (
        404,
        405,
    ), "a worker never sends the knowledge config"
    missing = client.get(f"{runs_path()}/01a15407-64e6-7c30-a9e3-4f2bef7f8f09/log", headers=hub.duty["headers"])
    assert missing.status_code == 404


REPORT = {
    "projects": [
        {
            "project": PROJECT,
            "checked_at": "2026-10-19T01:00:00+00:00",
            "sources": [
                {"source": "app", "status": "failed", "error": "fetch failed, reading origin/main as the clone has it"},
                {"source": "app-tag", "status": "skipped", "error": "its ref 'v1' is not origin/<branch>"},
            ],
        }
    ]
}


def test_kg_api_heartbeat_names_the_duty_projects_and_keeps_the_report(hub):
    client = hub.client
    features = [runs.KG_SYNC]
    assert beat(client, hub.duty, features=features)["kg_sync"] == [{"project": PROJECT, "received_at": None}]
    assert beat(client, hub.duty)["kg_sync"] == [], "a daemon that does not say kg_sync"
    assert beat(client, hub.spare, features=features)["kg_sync"] == []
    assert beat(client, hub.outsider, features=features)["kg_sync"] == []

    beat(client, hub.duty, features=features, kg_sync=REPORT)
    shown = ok(client.get(f"/v1/workers/{hub.duty['id']}", headers=hub.otto))
    source = shown["kg_sync"]["projects"][0]["sources"][0]
    assert {key: source[key] for key in ("source", "status", "error", "pulled", "pushed")} == {
        **REPORT["projects"][0]["sources"][0],
        "pulled": 0,
        "pushed": 0,
    }

    async def figures():
        async with client.app.state.engine.begin() as conn:
            return await kg_sync_figures(conn, hub.duty["id"], PROJECT), await kg_sync_figures(
                conn, hub.duty["id"], OTHER
            )

    found, other = client.portal.call(figures)
    assert found["worker"] == "night-mac" and found["failed"] == 1 and other is None
    assert [source["status"] for source in found["sources"]] == ["failed", "skipped"]

    beat(client, hub.duty, features=features)
    assert ok(client.get(f"/v1/workers/{hub.duty['id']}", headers=hub.otto))["kg_sync"] is None
    too_long = {"projects": [{**REPORT["projects"][0], "error": "x" * (runs.KG_SYNC_ERROR_CHARS + 1)}]}
    body = {"runtimes": {}, "checkouts": {}, "free_slots": 1, "runs": [], "kg_sync": too_long}
    assert client.post("/v1/worker/heartbeat", json=body, headers=hub.duty["headers"]).status_code == 422


class Stage:
    """The git origins of the app and the harness, a laptop's clones and the worker's checkouts."""

    def __init__(self, tmp, monkeypatch):
        self.app = Upstream(tmp, "app", {"a.md": "# A\n\none\n", "b.md": "# B\n\none\n", "c.md": "# C\n\none\n"})
        self.harness = Upstream(tmp, "harness", {"harness.yaml": HARNESS, "knowledge.yaml": KNOWLEDGE})
        self.laptop_app = self.app.clone(tmp / "laptop" / "app")
        self.laptop = load_project_at(self.harness.clone(tmp / "laptop" / "harness"), tmp / "laptop" / "kg-home")
        self.checkouts = {
            "app": self.app.clone(tmp / "worker" / "ws" / "app"),
            "harness": self.harness.clone(tmp / "worker" / "ws" / "harness"),
        }


def built_files(hub) -> dict[str, str]:
    """Run the queued builds, then ``app:file:<path>`` -> rev of what the hub's built corpus holds alive."""
    data_dir = hub.tmp / "jobs"
    run_jobs(hub.db, hub.s3, data_dir)
    builds = ok(hub.client.get(f"/v1/kg/{PROJECT}/builds", headers=hub.otto))["builds"]
    assert builds[0]["status"] == "succeeded", builds[0]
    corpus = Corpus(PROJECT, data_dir / "kg" / "home")
    try:
        return {r.item_id: r.rev for r in corpus.items("app") if r.item_id.startswith("app:file:")}
    finally:
        corpus.close()


def test_kg_api_worker_and_laptop_pushing_in_turn_leave_the_built_graph_at_the_newest_commit(hub, monkeypatch):
    stage = Stage(hub.tmp / "git", monkeypatch)
    laptop = InProcessHub(hub.client, hub.wendy)
    worker = KgSync(hub.tmp / "worker-home", InProcessHub(hub.client, hub.duty["headers"]))

    assert sync_source(stage.laptop, "app").ok
    assert kg_push.Pusher(laptop, stage.laptop).push().ok  # C1 and the knowledge config, from the laptop
    assert built_files(hub) == stage.app.files()

    stage.app.commit({"a.md": "# A\n\ntwo\n", "b.md": None, "d.md": "# D\n\none\n"}, T[1])  # C2
    state = worker.look(PROJECT, stage.checkouts)
    assert state.error is None and state.sources["app"].status == "ok", state
    assert state.sources["app"].pulled == 1 and state.sources["app"].pushed == 1
    c2 = stage.app.files()
    assert built_files(hub) == c2

    git(stage.laptop_app, "remote", "set-url", "origin", str(stage.app.origin) + "-gone")  # offline: stale C1
    assert sync_source(stage.laptop, "app", force=True).ok
    assert kg_push.Pusher(laptop, stage.laptop).push().ok
    assert "app:file:b.md" in built_files(hub), "the laptop's stale run brought b back"
    state = worker.look(PROJECT, stage.checkouts)
    assert state.sources["app"].pulled == 1 and state.sources["app"].pushed == 1
    assert built_files(hub) == c2, "the worker read C2 again: nothing of C1 is left"

    i, a = tables.kg_ingests, tables.audit  # the worker's pushes went in as its owner, with its token
    pushers = {row[0] for row in live.sql(hub.db, select(i.c.pushed_by))}
    assert pushers == {hub.owner_id, ids(hub.db, tables.users, tables.users.c.login, LAPTOP)}
    tokens = {row[0] for row in live.sql(hub.db, select(a.c.token_id).where(a.c.action == "kg.ingest"))}
    assert hub.duty["token_id"] in tokens

    again = worker.look(PROJECT, stage.checkouts)
    assert again.sources["app"].pushed == 0 and again.sources["app"].pulled == 0, "nothing new: nothing pushed"


def test_kg_api_the_night_figures_name_the_sync_that_failed(hub):
    client = hub.client
    beat(client, hub.duty, features=[runs.KG_SYNC], kg_sync=REPORT)
    schedule_id = live.sql(hub.db, select(tables.schedules.c.id))[0][0]
    owner = Principal(user_id=hub.owner_id, login=OWNER, admin=False, token_id=0, kind=MACHINE, token_hash="")
    due = SimpleNamespace(
        id=schedule_id,
        project_id=hub.project_id,
        project=PROJECT,
        worker_id=hub.duty["id"],
        body={},
        owner_id=hub.owner_id,
    )

    async def counted():
        async with client.app.state.engine.begin() as conn:
            access = await project_access(conn, owner, PROJECT)
            return (await collect.ensure_figures(conn, due, date(2026, 10, 19), access)).figures

    figures = client.portal.call(counted)
    assert figures["kg_sync"]["worker"] == "night-mac" and figures["kg_sync"]["failed"] == 1
    assert figures["kg_sync"]["sources"][0]["error"].startswith("fetch failed")
    assert "kg_sync" in review.FIGURE_KEYS and '"kg_sync"' in review._figures_text(figures)

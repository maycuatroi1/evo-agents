"""The flow after an upload, followed to its end (plan kg-zip-to-answers, step 4, criteria a2 and a5): the phases
derived from the rows (``evo_agents.hub.kg_pipeline``), GET /v1/kg/{project}/uploads/{id}/pipeline, ``hub kg upload``
waiting until the graph is ready or a phase failed, ``hub kg pipeline``, and the notice kg_pipeline to the sender and
the project's admins, once."""

import argparse
import json
from functools import partial
from types import SimpleNamespace

import pytest

from evo_agents.hub import kg_coverage, kg_pipeline, kg_upload_cli
from evo_agents.hub.client import HubError
from tests.hub import pg

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)

READ = {"state": "extracted", "files": 12, "build_id": 7, "uploaded_at": "t"}
BUILT = {"id": 7, "status": "succeeded", "nodes": 40, "edges": 90, "coverage": {"sources": 1}}
# as kg_builds.coverage keeps them: build_coverage drops the step's "enabled"
ENTITIES = kg_coverage.build_coverage(
    {"entities": {"enabled": True, "nodes": 30, "edges": 55, "stopped": None, "failed": 0, "deferred": 0}},
    None,
    skipped=False,
)["entities"]


def states(found: dict) -> dict:
    return {p["name"]: p["state"] for p in found["phases"]}


def test_the_phases_of_an_upload_still_moving():
    found = kg_pipeline.derive({"state": "uploading"})
    assert (found["state"], found["phase"]) == ("running", "uploading")
    assert [p["name"] for p in found["phases"]] == list(kg_pipeline.PHASES)
    found = kg_pipeline.derive(READ, {**BUILT, "status": "running"})
    assert (found["state"], found["phase"]) == ("running", "building")
    assert states(found)["extracting"] == "done" and states(found)["curator"] == "pending"


def test_entities_follow_the_curator_whose_rebuild_runs_them_and_the_graph_is_ready_after_it():
    init = {"id": 3, "state": "running", "agent_run_id": 11, "body": {"questions": {"state": "done", "version": 2}}}
    found = kg_pipeline.derive(READ, BUILT, init)
    assert [p["name"] for p in found["phases"]][3:] == ["questions", "curator", "entities"]
    assert (found["state"], found["phase"]) == ("running", "curator")
    done = {**init, "state": "done", "body": {**init["body"], "applied": {"change": {"build_id": 8}}}}
    found = kg_pipeline.derive(READ, BUILT, done, rebuild={"id": 8, "status": "running"})
    assert (found["state"], found["phase"]) == ("running", "entities")
    rebuilt = {"id": 8, "status": "succeeded", "coverage": {"entities": ENTITIES}}
    found = kg_pipeline.derive(READ, BUILT, done, rebuild=rebuilt)
    assert (found["state"], found["phase"], found["error"]) == ("ready", None, None)
    assert found["phases"][-1]["detail"] == "30 entities, 55 relations"


def test_entities_run_in_the_build_when_the_strategy_has_them_on_already():
    built = {**BUILT, "coverage": {"entities": ENTITIES}}
    init = {"id": 3, "state": "done", "body": {"questions": {"state": "not_needed"}}}
    found = kg_pipeline.derive(READ, built, init)
    assert [p["name"] for p in found["phases"]] == list(kg_pipeline.PHASES)
    assert found["state"] == "ready" and states(found)["questions"] == "skipped"


def test_an_entities_step_with_no_key_fails_the_flow_and_items_deferred_to_claude_code_skip_it():
    stopped = {**ENTITIES, "nodes": 0, "edges": 0, "stopped": "no key: set OPENROUTER_API_KEY"}
    init = {"id": 3, "state": "done", "body": {}}
    found = kg_pipeline.derive(READ, {**BUILT, "coverage": {"entities": stopped}}, init)
    assert (found["state"], found["phase"], found["error"]) == ("failed", "entities", stopped["stopped"])
    deferred = {**stopped, "deferred": 4, "stopped": "claude is not installed on this machine: 4 item(s) wait"}
    found = kg_pipeline.derive(READ, {**BUILT, "coverage": {"entities": deferred}}, init)
    assert found["state"] == "ready" and states(found)["entities"] == "skipped"


def test_a_failed_phase_names_itself_and_its_reason():
    found = kg_pipeline.derive({"state": "failed", "error": "the store holds 3 bytes"})
    assert (found["state"], found["phase"], found["error"]) == ("failed", "uploading", "the store holds 3 bytes")
    found = kg_pipeline.derive({**READ, "state": "failed", "error": "corrupt archive"})
    assert (found["state"], found["phase"]) == ("failed", "extracting")
    found = kg_pipeline.derive(READ, {**BUILT, "status": "failed", "error": "no knowledge config"})
    assert (found["state"], found["phase"], found["error"]) == ("failed", "building", "no knowledge config")
    found = kg_pipeline.derive(READ, BUILT, {"id": 3, "state": "failed", "error": "ended without a decision"})
    assert (found["state"], found["phase"], found["error"]) == ("failed", "curator", "ended without a decision")


def test_a_build_with_no_curator_run_after_it_is_ready_unless_one_from_before_is_in_flight():
    found = kg_pipeline.derive(READ, BUILT, None)
    assert found["state"] == "ready" and states(found)["curator"] == "skipped"
    found = kg_pipeline.derive(READ, BUILT, None, waiting={"id": 2})
    assert (found["state"], found["phase"]) == ("running", "questions")
    found = kg_pipeline.derive({**READ, "build_id": None, "replaced_by": 9})
    assert (found["state"], found["phase"]) == ("replaced", None)


class Answers:
    """A hub that answers the flow's reads with ``answers`` in turn."""

    url = "https://hub.example.org"

    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def call(self, method, path, body=None, *, timeout=None):
        self.calls.append((method, path))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def flow(state, phase, error=None, phases=()):
    return {"upload_id": 5, "state": state, "phase": phase, "error": error, "phases": list(phases)}


def test_follow_prints_each_phase_once_then_ready():
    building = [{"name": "building", "state": "running", "detail": "build #7 running"}]
    hub = Answers([flow("running", "building", phases=building)] * 2 + [flow("ready", None, phases=building)])
    said = []
    found = kg_upload_cli.follow(hub, "northwind", 5, said.append, sleep=lambda _: None)
    assert found["state"] == "ready" and len(hub.calls) == 3
    assert said == [
        "Knowledge graph build in progress: building: build #7 running",
        "Knowledge graph ready (building running)",
    ]
    assert hub.calls[0] == ("GET", "/v1/kg/northwind/uploads/5/pipeline")


def test_follow_reads_again_through_a_redeploy_and_raises_what_is_not_transient():
    building = [{"name": "building", "state": "running", "detail": "build #7 running"}]
    gone = HubError("the hub at https://hub.example.org answered HTTP 502", status=502)
    hub = Answers([flow("running", "building", phases=building), gone, gone, flow("ready", None, phases=building)])
    said = []
    found = kg_upload_cli.follow(hub, "northwind", 5, said.append, sleep=lambda _: None)
    assert found["state"] == "ready" and len(hub.calls) == 4 and len(said) == 3
    assert said[1].startswith("the hub did not answer (the hub at https://hub.example.org answered HTTP 502)")
    with pytest.raises(HubError):
        kg_upload_cli.follow(Answers([HubError("no", status=403)]), "northwind", 5, said.append, sleep=lambda _: None)


if pg.DSN:
    from fastapi.testclient import TestClient
    from sqlalchemy import func, insert, select, update

    from evo_agents.hub import tables
    from evo_agents.hub.kg_build import STOPPED
    from evo_agents.hub.server import kg_pipeline as served
    from evo_agents.hub.server.app import create_app
    from tests.hub import live
    from tests.hub.test_blobs import make_config, member

PROJECT = "northwind"


class AppHub:
    """``Hub.call`` answered by the app in this process, as the CLI calls it."""

    url = "https://hub.example.org"

    def __init__(self, client, headers: dict):
        self.client, self.headers = client, headers

    def call(self, method: str, path: str, body=None, *, timeout=None):
        response = self.client.request(method, path, json=body, headers=self.headers)
        if not 200 <= response.status_code < 300:
            raise HubError(response.json().get("message") or response.text, response.status_code)
        return response.json()


def user_id(db, login: str) -> int:
    users = tables.users
    ((found,),) = live.sql(db, select(users.c.id).where(users.c.login == login))
    return found


@pytest.fixture
def world(hub_db, tmp_path):
    project_id = live.add_project(hub_db, PROJECT)
    sender = member(hub_db, project_id, "lumen-writer", "writer")
    admin = member(hub_db, project_id, "lumen-admin", "admin")
    outsider = member(hub_db, project_id, "lumen-reader", "reader")
    g = tables.grants
    for login in ("lumen-writer", "lumen-admin"):
        live.sql(hub_db, update(g).where(g.c.user_id == user_id(hub_db, login)).values(max_level="secret"))
    b, u = tables.kg_builds, tables.kg_uploads
    ((build_id,),) = live.sql(hub_db, insert(b).values(project_id=project_id, status="queued").returning(b.c.id))
    row = {
        "project_id": project_id,
        "source": "handbook",
        "label": {"level": "internal"},
        "name": "handbook.zip",
        "size": 10,
        "sha256": "0" * 64,
        "part_size": 10,
        "object_key": "kg-archives/x",
        "state": "extracted",
        "report": {"files": 3},
        "build_id": build_id,
        "created_by": user_id(hub_db, "lumen-writer"),
    }
    ((upload_id,),) = live.sql(hub_db, insert(u).values(row).returning(u.c.id))
    app = create_app(make_config(hub_db, tmp_path))
    with TestClient(app) as client:
        yield SimpleNamespace(
            db=hub_db,
            client=client,
            project_id=project_id,
            build_id=build_id,
            upload_id=upload_id,
            sender=sender,
            admin=admin,
            outsider=outsider,
            path=f"/v1/kg/{PROJECT}/uploads/{upload_id}/pipeline",
        )


def told(world) -> list:
    n = tables.notifications
    return live.sql(
        world.db, select(n.c.user_id, n.c.notice_kind, n.c.title, n.c.link).where(n.c.notice_kind == "kg_pipeline")
    )


def tell(world) -> dict:
    return world.client.portal.call(partial(served.tell, world.client.app.state.engine))


def set_build(world, build_id: int, **values) -> None:
    b = tables.kg_builds
    if values.get("status") == "succeeded":
        values = {
            "content_hash": "sha256:" + "c" * 64,
            "artifact_sha256": "e" * 64,
            "artifact_size": 10,
            "nodes": 4,
            "edges": 6,
            **values,
        }
    if values.get("status") in ("succeeded", "failed"):
        values = {"started_at": func.now(), "finished_at": func.now(), **values}
    elif values.get("status") == "running":
        values = {"started_at": func.now(), **values}
    live.sql(world.db, update(b).where(b.c.id == build_id).values(**values))


def init_run(world, **values) -> int:
    t = tables.kg_init_runs
    row = {"project_id": world.project_id, "via": "first_build", "why": ["first build"], "body": {}, **values}
    ((found,),) = live.sql(world.db, insert(t).values(row).returning(t.c.id))
    return found


@needs_pg
def test_the_route_follows_an_upload_to_ready_and_its_sender_and_admins_are_told_once(world):
    read = world.client.get(world.path, headers=world.sender).json()
    assert (read["state"], read["phase"], read["build_id"]) == ("running", "building", world.build_id)
    assert tell(world) == {} and told(world) == []
    set_build(world, world.build_id, status="succeeded", nodes=4, edges=6, coverage={"sources": 1})
    body = {"questions": {"state": "running", "agent_run_id": None}}
    init_id = init_run(world, state="queued", build_id=world.build_id, body=body)
    read = world.client.get(world.path, headers=world.admin).json()
    assert (read["phase"], read["init_id"]) == ("questions", init_id)
    b, t = tables.kg_builds, tables.kg_init_runs
    ((rebuild,),) = live.sql(world.db, insert(b).values(project_id=world.project_id, status="queued").returning(b.c.id))
    set_build(world, rebuild, status="running")
    decided = {"questions": {"state": "done", "version": 1}, "applied": {"change": {"build_id": rebuild}}}
    live.sql(world.db, update(t).where(t.c.id == init_id).values(state="done", body=decided))
    read = world.client.get(world.path, headers=world.sender).json()
    assert (read["state"], read["phase"], read["rebuild_id"]) == ("running", "entities", rebuild)
    entities = {"nodes": 9, "edges": 12, "stopped": None, "failed": 0, "deferred": 0}
    set_build(world, rebuild, status="succeeded", coverage={"entities": entities})
    read = world.client.get(world.path, headers=world.sender).json()
    assert (read["state"], read["phase"], read["told_at"]) == ("ready", None, None)
    assert {p["name"]: p["state"] for p in read["phases"]}["entities"] == "done"

    assert tell(world) == {"ready": 1}
    found = told(world)
    assert sorted(r[0] for r in found) == sorted([user_id(world.db, "lumen-writer"), user_id(world.db, "lumen-admin")])
    assert {(r[2], r[3]) for r in found} == {(f"Knowledge graph of {PROJECT} ready", f"/p/{PROJECT}/kg")}
    assert tell(world) == {} and len(told(world)) == 2, "told once"
    assert world.client.get(world.path, headers=world.sender).json()["told_at"] is not None


@needs_pg
def test_a_rebuild_its_worker_stopped_is_followed_by_the_build_queued_in_its_place(world):
    """Run on agents.omelet.tech 2026-11-05 (plan kg-zip-to-answers, step 7): the hub restarted under the Curator's
    rebuild, which failed as STOPPED and queued another; the flow said failed though the new build went on."""
    set_build(world, world.build_id, status="succeeded", nodes=4, edges=6, coverage={"sources": 1})
    b = tables.kg_builds
    ((stopped,),) = live.sql(world.db, insert(b).values(project_id=world.project_id, status="queued").returning(b.c.id))
    decided = {"questions": {"state": "done", "version": 1}, "applied": {"change": {"build_id": stopped}}}
    init_run(world, state="done", build_id=world.build_id, body=decided)
    set_build(world, stopped, status="failed", error=STOPPED)
    ((again,),) = live.sql(world.db, insert(b).values(project_id=world.project_id, status="queued").returning(b.c.id))
    set_build(world, again, status="running")
    read = world.client.get(world.path, headers=world.sender).json()
    assert (read["state"], read["phase"], read["rebuild_id"]) == ("running", "entities", again)
    assert read["phases"][-1]["detail"] == f"rebuild #{again} with the Curator's settings running"
    assert tell(world) == {} and told(world) == [], "no notice while the build queued in its place runs"
    entities = {"nodes": 9, "edges": 12, "stopped": None, "failed": 0, "deferred": 0}
    set_build(world, again, status="succeeded", coverage={"entities": entities})
    read = world.client.get(world.path, headers=world.sender).json()
    assert (read["state"], read["phases"][-1]["state"]) == ("ready", "done")

    no_artifact = {"artifact_sha256": None, "artifact_size": None, "content_hash": None}
    set_build(world, again, status="failed", error="no knowledge config on the hub yet", **no_artifact)
    read = world.client.get(world.path, headers=world.sender).json()
    assert (read["state"], read["phase"]) == ("failed", "entities"), "any other failure still ends the flow"


@needs_pg
def test_the_newest_kg_init_run_counts_so_a_rerun_after_a_failed_one_ends_the_flow_ready(world):
    """Run on a second hub 2026-11-05 (plan kg-zip-to-answers): the first kg_init run of an upload's build failed,
    a manual one asked again ended done, and the flow still read the first, failed run."""
    set_build(world, world.build_id, status="succeeded", nodes=4, edges=6, coverage={"sources": 1})
    ended = {"questions": {"state": "failed", "ended": "the run ended failed without a question set"}}
    first = init_run(world, state="failed", build_id=world.build_id, body=ended, error="no decision")
    read = world.client.get(world.path, headers=world.sender).json()
    assert (read["state"], read["phase"], read["init_id"]) == ("failed", "curator", first)

    again = init_run(world, via="manual", state="queued", build_id=world.build_id, body={})
    read = world.client.get(world.path, headers=world.sender).json()
    assert (read["state"], read["phase"], read["init_id"]) == ("running", "questions", again), "the rerun in flight"

    decided = {"questions": {"state": "done", "version": 1}, "decision": {"strategy": {"use": "documents"}}}
    t = tables.kg_init_runs
    live.sql(world.db, update(t).where(t.c.id == again).values(state="done", body=decided))
    read = world.client.get(world.path, headers=world.sender).json()
    assert (read["state"], read["phase"], read["error"], read["init_id"]) == ("ready", None, None, again)
    phases = {p["name"]: p["state"] for p in read["phases"]}
    assert (phases["questions"], phases["curator"]) == ("done", "done")

    b = tables.kg_builds
    ((later,),) = live.sql(world.db, insert(b).values(project_id=world.project_id, status="queued").returning(b.c.id))
    set_build(world, later, status="succeeded", coverage={"sources": 1})
    newest = init_run(world, via="profile", state="running", build_id=later, body={"questions": {"state": "done"}})
    read = world.client.get(world.path, headers=world.sender).json()
    assert (read["state"], read["phase"], read["init_id"]) == ("running", "curator", newest), "only the newest counts"


@needs_pg
def test_a_failed_build_is_told_with_its_phase_and_reason(world):
    set_build(world, world.build_id, status="failed", error="no knowledge config on the hub yet")
    assert tell(world) == {"failed": 1}
    titles = {r[2] for r in told(world)}
    assert titles == {f"Knowledge graph of {PROJECT} failed at building"}


@needs_pg
def test_a_grant_that_does_not_see_the_label_reads_no_flow(world):
    response = world.client.get(world.path, headers=world.outsider)
    assert response.status_code == 404
    missing = world.client.get(f"/v1/kg/{PROJECT}/uploads/999999/pipeline", headers=world.sender)
    assert missing.status_code == 404


@needs_pg
def test_upload_waits_until_ready_and_exits_0_and_a_failed_phase_exits_1(world, monkeypatch, capsys, tmp_path):
    hub = AppHub(world.client, world.sender)
    monkeypatch.setattr(kg_upload_cli, "signed_in", lambda: (hub, None))
    done = {"id": world.upload_id, "size": 10, "source": "handbook", "state": "extracting"}
    monkeypatch.setattr(kg_upload_cli, "upload", lambda *a, **k: (done, 1, 0))
    reads = []

    def ready_after_a_poll(_):
        reads.append(1)
        set_build(world, world.build_id, status="succeeded", nodes=4, edges=6, coverage={"sources": 1})

    monkeypatch.setattr(kg_upload_cli.time, "sleep", ready_after_a_poll)
    archive = tmp_path / "handbook.zip"
    archive.write_bytes(b"x" * 10)
    args = dict(file=str(archive), project=PROJECT, new_project=None, source=None, label=None)
    assert kg_upload_cli.cmd_upload(argparse.Namespace(**args, no_wait=False, json=False)) == 0
    out = capsys.readouterr().out
    assert "Knowledge graph build in progress: building: build #" in out and len(reads) == 1
    assert "Knowledge graph ready" in out and f"the graph: {hub.url}/p/{PROJECT}/kg" in out

    assert kg_upload_cli.cmd_upload(argparse.Namespace(**args, no_wait=True, json=True)) == 0
    assert json.loads(capsys.readouterr().out)["pipeline"] is None, "--no-wait returns once the parts are joined"

    no_artifact = {"artifact_sha256": None, "artifact_size": None, "content_hash": None}
    set_build(world, world.build_id, status="failed", error="the archive held no file the hub reads", **no_artifact)
    assert kg_upload_cli.cmd_upload(argparse.Namespace(**args, no_wait=False, json=False)) == 1
    assert "Knowledge graph build failed at building: the archive held no file the hub reads" in capsys.readouterr().out
    pipeline = argparse.Namespace(project=PROJECT, upload=None, json=False)
    assert kg_upload_cli.cmd_pipeline(pipeline) == 1
    out = capsys.readouterr().out
    assert out.startswith(f"upload {world.upload_id}: Knowledge graph build failed at building")
    assert "  building   failed" in out
    assert kg_upload_cli.cmd_pipeline(argparse.Namespace(project=PROJECT, upload=world.upload_id, json=True)) == 1
    assert json.loads(capsys.readouterr().out)["phase"] == "building"

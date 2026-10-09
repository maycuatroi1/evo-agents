"""The plan of an author run on the hub: PUT /v1/worker/runs/{id}/plan, which the agent's `evo-agents worker put` calls,
GET /v1/worker/runs/{id}/plan, which `evo-agents worker plan` reads, and an author run dispatched on a plan
(``plan_id``, `evo-agents hub run author --plan`).

The checks step 2 of the hub-plan-authoring plan names on the hub: the plan is stored in the run's project as the member
who dispatched the run, with the checks of PUT /v1/projects/{p}/plans/{id} (422 for plan.schema.json, 413 for size, the
label the hub sink must clear, the warnings of plan_semantics in the answer); the dispatcher's writer role is checked
at the write (403 once lost); without if_revision a plan is never replaced (409); the history of the plan and the run
name each other; an author run on a plan reads it with its revision and writes it with if_revision, 409 when it
changed in between; a write that changes the plan's id or the progress the hub holds is 422; a worker token is still
refused outside /v1/worker/, and the route takes only an author run the worker holds."""

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import copy

from fastapi.testclient import TestClient
from sqlalchemy import select

from evo_agents.hub import author, tables
from evo_agents.hub.plans import MAX_PLAN_BYTES
from evo_agents.hub.server.app import create_app
from tests.hub import live
from tests.hub.live import ADMIN, sql
from tests.hub.test_author_runs import author_worker, dispatched_author, publish_skill
from tests.hub.test_runs import (
    OWNER,
    PLAN,
    PROJECT,
    READ_ONLY,
    add_worker,
    audit_rows,
    claim,
    dispatched,
    members,
    moved,
    of_run,
    plan_body,
)

NEW = "wait-helper"


@pytest.fixture
def client(hub_db, tmp_path, github, s3):
    config = live.hub_config(hub_db, tmp_path, github, **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github) -> dict:
    """The project, members and plan of ``tests.hub.test_runs.hub``."""
    return members(client, github)


def new_plan(**extra) -> dict:
    return {
        "id": NEW,
        "title": "A wait helper",
        "goal": "Tests wait for a condition instead of sleeping.",
        "repos": [{"repo": "evo-agents", "branch": "feat/wait"}],
        "steps": [
            {"id": 1, "title": "Helper", "repo": "evo-agents", "what": "the helper", "status": "pending"},
            {"id": 2, "title": "Use it", "repo": "evo-agents", "what": "the tests", "depends_on": [1]},
        ],
        **extra,
    }


def put(client, worker: dict, run_id: int, body: dict, **extra):
    return client.put(f"/v1/worker/runs/{run_id}/plan", json={"body": body, **extra}, headers=worker["headers"])


def read(client, worker: dict, run_id: int):
    return client.get(f"/v1/worker/runs/{run_id}/plan", headers=worker["headers"])


def running_author(client, hub, tmp_path, **extra) -> tuple[dict, int]:
    """An author run of the owner's, claimed by the owner's worker and running: (worker, run id)."""
    publish_skill(client, hub, tmp_path, "The author mode.")
    worker = author_worker(client, hub["owner"], "mac-mini")
    return worker, started(client, hub, worker, **extra)


def started(client, hub, worker: dict, **extra) -> int:
    """Another author run of the owner's on ``worker``, claimed and running: its id."""
    run_id = dispatched_author(client, hub["owner"], worker["id"], **extra)["id"]
    spec = claim(client, worker)
    assert spec["id"] == run_id
    moved(client, worker, run_id, "running")
    return run_id


def history(client, headers, plan_id: str) -> list[dict]:
    response = client.get(f"/v1/projects/{PROJECT}/plans/{plan_id}/revisions", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def held_plans(db) -> list[tuple]:
    plans = tables.plans
    return sql(db, select(plans.c.plan_id, plans.c.revision).order_by(plans.c.plan_id))


# A new plan


def test_an_author_run_puts_a_new_plan_as_its_dispatcher_and_the_history_names_the_run(client, hub, hub_db, tmp_path):
    worker, run_id = running_author(client, hub, tmp_path)
    missing = read(client, worker, run_id)
    assert missing.status_code == 404 and "has put none on the hub yet" in missing.json()["message"]

    elsewhere = {"id": 3, "repo": "elsewhere", "what": "x"}
    created = put(client, worker, run_id, new_plan(steps=[*new_plan()["steps"], elsewhere]))
    assert created.status_code == 200, created.text
    written = created.json()
    assert (written["plan_id"], written["revision"], written["created"], written["changed"]) == (NEW, 1, True, True)
    assert written["updated_by"] == OWNER and written["project"] == PROJECT
    assert written["label"]["level"] == "internal", "the project's default label, as a put without one"
    assert any("elsewhere" in item["message"] for item in written["warnings"]), "plan_semantics warns, and stores"

    run = client.get(f"/v1/projects/{PROJECT}/runs/{run_id}", headers=hub["owner"]).json()
    assert (run["plan_id"], run["plan_revision"]) == (NEW, 1), "the run points to the plan it wrote"
    events = client.get(f"/v1/projects/{PROJECT}/runs/{run_id}/events", headers=hub["owner"]).json()["events"]
    assert any(event["body"].get("text") == f"Created plan {NEW}: revision 1" for event in events)
    assert read(client, worker, run_id).json()["revision"] == 1

    # without if_revision, the plan the run created is never replaced
    again = put(client, worker, run_id, new_plan(title="Another title"))
    assert again.status_code == 409 and again.json()["error"] == "revision_conflict"
    assert again.json()["current"]["revision"] == 1
    revised = put(client, worker, run_id, new_plan(title="Another title"), if_revision=1)
    assert revised.status_code == 200 and revised.json()["revision"] == 2 and not revised.json()["created"]
    same = put(client, worker, run_id, new_plan(title="Another title"), if_revision=2)
    assert same.status_code == 200 and same.json()["changed"] is False

    revisions = history(client, hub["reader"], NEW)
    assert [(item["revision"], item["actor"], item["run_id"]) for item in revisions] == [
        (2, OWNER, run_id),
        (1, OWNER, run_id),
    ]
    one = client.get(f"/v1/projects/{PROJECT}/plans/{NEW}/revisions/2", headers=hub["owner"]).json()
    assert one["run_id"] == run_id and one["body"]["title"] == "Another title"
    assert [item["run_id"] for item in history(client, hub["owner"], PLAN)] == [None], "a member's put names no run"
    assert [row for row in audit_rows(hub_db, "plan") if NEW in row[1]] == [
        ("plan.create", f"{PROJECT}/{NEW}@1", OWNER, PROJECT),
        ("plan.put", f"{PROJECT}/{NEW}@2", OWNER, PROJECT),
    ]
    assert of_run(hub_db, run_id, "plan_id", "plan_revision") == [(NEW, 2)]

    done = moved(client, worker, run_id, "verifying", "done", summary="Wrote plan wait-helper.")
    assert done["plan_id"] == NEW
    after = put(client, worker, run_id, new_plan(title="Late"), if_revision=2)
    assert after.status_code == 404, "an author run that ended writes nothing"


def test_an_author_put_is_checked_as_a_plan_put_schema_size_and_label(client, hub, hub_db, tmp_path):
    worker, run_id = running_author(client, hub, tmp_path)
    before = held_plans(hub_db)

    schema = put(client, worker, run_id, {**new_plan(), "steps": "one step"})
    assert schema.status_code == 422 and "does not match plan.schema.json" in schema.json()["message"]
    assert schema.json()["detail"], "the problems, path by path"
    no_id = put(client, worker, run_id, {key: value for key, value in new_plan().items() if key != "id"})
    assert no_id.status_code == 422 and "the plan's id is None" in no_id.json()["message"]
    large = put(client, worker, run_id, new_plan(context="x" * (MAX_PLAN_BYTES + 1)))
    assert large.status_code == 413 and f"more than the {MAX_PLAN_BYTES} the hub keeps" in large.json()["message"]
    above = put(client, worker, run_id, new_plan(), label={"level": "customer"})
    assert above.status_code == 422 and "not cleared by hub sink" in above.json()["message"]
    curator = put(client, worker, run_id, new_plan(id="curator-7-x"))
    assert curator.status_code == 409 and "belong to the Curator" in curator.json()["message"]
    taken = put(client, worker, run_id, {**plan_body(), "title": "Mine now"})
    assert taken.status_code == 409 and taken.json()["error"] == "revision_conflict", "an existing plan stays"
    stomp = put(client, worker, run_id, {**plan_body(), "title": "Mine now"}, if_revision=1)
    assert stomp.status_code == 422 and "writes a new plan, so it replaces none" in stomp.json()["message"]
    assert held_plans(hub_db) == before
    assert of_run(hub_db, run_id, "plan_id", "plan_revision") == [(None, None)]


def test_author_put_security_the_dispatchers_role_is_checked_at_the_write_and_worker_tokens_stay_on_their_routes(
    client, hub, hub_db, tmp_path
):
    worker, run_id = running_author(client, hub, tmp_path)
    for method, path in (
        ("PUT", f"/v1/projects/{PROJECT}/plans/{NEW}"),
        ("GET", f"/v1/projects/{PROJECT}/plans/{PLAN}"),
        ("GET", f"/v1/projects/{PROJECT}/runs/{run_id}"),
    ):
        response = client.request(method, path, json={"body": new_plan()}, headers=worker["headers"])
        assert response.status_code == 403, (path, response.text)
        assert "only on /v1/worker/*" in response.json()["message"]

    theirs = add_worker(client, hub["other"], "their-mac")
    assert put(client, theirs, run_id, new_plan()).status_code == 404, "only the worker that holds the run"
    step_run = dispatched(client, hub["owner"], [2])[0]["id"]
    refused = put(client, worker, step_run, new_plan())
    assert refused.status_code == 404

    grants = f"/v1/admin/projects/{PROJECT}/grants/{OWNER}"
    assert client.put(grants, json=READ_ONLY, headers=hub["admin"]).status_code == 200
    reader = put(client, worker, run_id, new_plan())
    assert reader.status_code == 403 and "no longer has the writer role" in reader.json()["message"]
    assert client.delete(grants, headers=hub["admin"]).status_code == 204
    gone = put(client, worker, run_id, new_plan())
    assert (
        gone.status_code == 403
        and f"{OWNER}, who dispatched run {run_id}, no longer has a grant" in (gone.json()["message"])
    )
    assert NEW not in [plan_id for plan_id, _ in held_plans(hub_db)]
    assert ADMIN not in [row[2] for row in audit_rows(hub_db, "plan")]


# An existing plan


def test_an_author_run_on_a_plan_reads_its_revision_and_writes_it_with_if_revision_409_when_it_changed(
    client, hub, hub_db, tmp_path
):
    worker, run_id = running_author(client, hub, tmp_path, plan_id=PLAN)
    run = client.get(f"/v1/projects/{PROJECT}/runs/{run_id}", headers=hub["owner"]).json()
    assert (run["kind"], run["plan_id"], run["plan_revision"]) == ("author", PLAN, 1)
    current = read(client, worker, run_id)
    assert current.status_code == 200 and current.json()["revision"] == 1

    body = copy.deepcopy(current.json()["body"])
    body["goal"] = "Ship the run queue, and say why."
    other_id = put(client, worker, run_id, {**body, "id": NEW})
    assert other_id.status_code == 422 and f"writes plan {PLAN}, not {NEW}" in other_id.json()["message"]
    blind = put(client, worker, run_id, body)
    assert blind.status_code == 409 and "pass if_revision 1 to replace it" in blind.json()["message"]

    # the owner changes the plan while the agent works on revision 1
    changed = {**plan_body(), "title": "Rollout"}
    pushed = client.put(
        f"/v1/projects/{PROJECT}/plans/{PLAN}", json={"body": changed, "if_revision": 1}, headers=hub["owner"]
    )
    assert pushed.status_code == 200 and pushed.json()["revision"] == 2
    stale = put(client, worker, run_id, body, if_revision=1)
    assert stale.status_code == 409 and stale.json()["error"] == "revision_conflict"
    assert stale.json()["current"]["revision"] == 2 and stale.json()["current"]["body"]["title"] == "Rollout"

    again = read(client, worker, run_id).json()
    assert again["revision"] == 2
    body = {**again["body"], "goal": "Ship the run queue, and say why."}
    written = put(client, worker, run_id, body, if_revision=2)
    assert written.status_code == 200, written.text
    assert (written.json()["revision"], written.json()["created"]) == (3, False)
    assert [(item["revision"], item["run_id"]) for item in history(client, hub["owner"], PLAN)] == [
        (3, run_id),
        (2, None),
        (1, None),
    ]
    assert of_run(hub_db, run_id, "plan_id", "plan_revision") == [(PLAN, 3)]


def test_an_author_run_on_a_plan_is_dispatched_with_a_prompt_that_names_its_revision(client, hub, tmp_path):
    publish_skill(client, hub, tmp_path, "The author mode.")
    worker = author_worker(client, hub["owner"], "mac-mini")
    missing = client.post(
        f"/v1/projects/{PROJECT}/author-runs",
        json={"request": "Revise it.", "worker_id": worker["id"], "plan_id": "no-such-plan"},
        headers=hub["owner"],
    )
    assert missing.status_code == 404
    run_id = dispatched_author(client, hub["owner"], worker["id"], plan_id=PLAN)["id"]
    prompt = claim(client, worker)["prompt"]
    assert f"This run revises plan {PLAN} of the project, at revision 1" in prompt
    assert f"`{author.PUT_COMMAND} FILE --if-revision REVISION`" in prompt and author.READ_COMMAND in prompt
    assert f"{author.PUT_COMMAND} FILE`, FILE being its YAML" not in prompt
    assert run_id
    # an author run on the plan is not a run of its steps: they stay ready
    ready = client.get(f"/v1/projects/{PROJECT}/plans/{PLAN}/ready-steps", headers=hub["owner"]).json()
    assert ready["plan_run"] is None and all(item["active_run"] is None for item in ready["steps"])
    plan_run = client.post(f"/v1/projects/{PROJECT}/plan-runs", json={"plan_id": PLAN}, headers=hub["owner"])
    assert plan_run.status_code == 201, plan_run.text


def test_an_author_run_never_changes_the_progress_of_steps_or_repos(client, hub, hub_db, tmp_path):
    worker, run_id = running_author(client, hub, tmp_path, plan_id=PLAN)
    held = read(client, worker, run_id).json()["body"]

    def changed(edit) -> dict:
        body = copy.deepcopy(held)
        edit(body)
        return body

    refusals = {
        "step 1: status": lambda b: b["steps"][0].update(status="pending"),
        "step 1: evidence": lambda b: b["steps"][0].update(evidence="something else"),
        "step 2: done_at": lambda b: b["steps"][1].update(done_at="2026-10-09"),
        "step 3: status": lambda b: b["steps"][2].update(status="in_progress"),
        "step 1: status done -> unset": lambda b: b["steps"].pop(0),
        "repo evo-agents: merged_at": lambda b: b["repos"][0].update(merged_at="2026-10-09"),
        "repo evo-agents: status": lambda b: b["repos"][0].update(status="merged"),
        "step 9: status": lambda b: b["steps"].append({"id": 9, "repo": "evo-agents", "what": "x", "status": "done"}),
    }
    for shown, edit in refusals.items():
        refused = put(client, worker, run_id, changed(edit), if_revision=1)
        assert refused.status_code == 422, (shown, refused.text)
        message = refused.json()["message"]
        assert "an author run writes what a plan says, never its progress" in message and shown in message, message
    assert held_plans(hub_db) == [(PLAN, 1)]

    def rewrite(body) -> None:
        body["goal"] = "Ship the run queue to every worker."
        body["steps"][1]["what"] = "the queue, with its claim"
        body["steps"][1]["status"] = "pending"  # pending, as held
        body["steps"][3].pop("status")  # unset reads as pending: no progress either way
        body["steps"].append({"id": 6, "title": "Release", "repo": "evo-agents", "what": "the release"})

    kept = put(client, worker, run_id, changed(rewrite), if_revision=1)
    assert kept.status_code == 200, kept.text
    assert kept.json()["revision"] == 2 and kept.json()["body"]["steps"][0]["status"] == "done"

    moved(client, worker, run_id, "verifying", "done", summary="Revised.")
    new_run = started(client, hub, worker)
    done_step = new_plan()
    done_step["steps"][0]["status"] = "done"
    refused = put(client, worker, new_run, done_step)
    assert refused.status_code == 422 and "none for a new plan" in refused.json()["message"]


def test_the_author_progress_check_as_a_function():
    held = plan_body()
    assert author.progress_problem(held, copy.deepcopy(held)) is None
    assert author.progress_problem(None, {"id": "x", "steps": [{"id": 1, "status": "pending"}]}) is None
    moved_on = copy.deepcopy(held)
    moved_on["steps"][1]["status"] = "done"
    problem = author.progress_problem(held, moved_on)
    assert "step 2: status unset -> done" in problem

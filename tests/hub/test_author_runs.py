"""Author runs on the hub: dispatching one (POST /v1/projects/{p}/author-runs), who may, on which worker and with what
request and runtime; the claim of one, with the skill create-exec-plan the hub holds at that moment; how one ends.

The checks step 1 of the hub-plan-authoring plan names on the hub: a writer dispatches an author run to a worker of
their own, with a request of at most 16 KiB of UTF-8 (422 over it), claude-code (422 for another runtime, saying why)
and a model; a reader gets 403, and so does a dispatch to another member's worker; only that worker claims it, and only
with a checkout of the project's harness and a daemon that says it runs author runs, so an older daemon never does; the
claim hands the latest global version of create-exec-plan with a presigned GET of its bundle, and the prompt names
that version and the author mode; a hub without the skill fails the run at its claim, saying so; the run ends done
without verify results and writes no plan."""

import hashlib

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from evo_agents.hub import author, runs, skills, tables
from evo_agents.hub.server.app import create_app
from tests.hub.live import ADMIN, sql
from tests.hub.test_runs import (
    OWNER,
    PROJECT,
    add_worker,
    audit_rows,
    claim,
    count_runs,
    members,
    moved,
    moves,
    of_run,
    report,
    state_of,
)
from tests.hub.test_skills_api import download, publish

HARNESS = "evo-agents-harness"  # the harness path tests.hub.test_plans.registration gives the project
CHECKOUTS = {
    f"{PROJECT}/{name}": {"path": f"/src/{name}", "branch": "main"} for name in (HARNESS, "evo-agents", "agent-skills")
}
NO_HARNESS = {key: value for key, value in CHECKOUTS.items() if not key.endswith(HARNESS)}
REQUEST = "Members write plans on the hub with an agent.\n\nThe agent asks in a chat, never in a terminal."
KINDS = list(runs.RUN_KINDS)
OLD_KINDS = ["step", "plan", "review", "judge"]  # a daemon of 0.8.0


@pytest.fixture
def client(hub_db, tmp_path, github, s3):
    config = live.hub_config(hub_db, tmp_path, github, **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github) -> dict:
    """The project, members and plan of ``tests.hub.test_runs.hub``."""
    return members(client, github)


def beat(client, worker: dict, kinds: list[str] | None, checkouts: dict | None = None) -> None:
    """A heartbeat of ``worker`` whose daemon says it runs ``kinds`` (None: an old daemon that names none)."""
    body = {
        "runtimes": worker["runtimes"],
        "checkouts": worker["checkouts"] if checkouts is None else checkouts,
        "free_slots": 1,
        "runs": [],
    }
    if kinds is not None:
        body["run_kinds"] = kinds
    response = client.post("/v1/worker/heartbeat", json=body, headers=worker["headers"])
    assert response.status_code == 200, response.text


def author_worker(client, headers, name: str, *, kinds=KINDS, checkouts=CHECKOUTS) -> dict:
    worker = add_worker(client, headers, name, checkouts=checkouts)
    beat(client, worker, kinds)
    return worker


def dispatch_author(client, headers, worker_id: int, **extra):
    body = {"request": REQUEST, "worker_id": worker_id, **extra}
    return client.post(f"/v1/projects/{PROJECT}/author-runs", json=body, headers=headers)


def dispatched_author(client, headers, worker_id: int, **extra) -> dict:
    response = dispatch_author(client, headers, worker_id, **extra)
    assert response.status_code == 201, response.text
    return response.json()


def skill_bundle(tmp_path, text: str) -> bytes:
    directory = tmp_path / f"skill-{hashlib.sha256(text.encode()).hexdigest()[:8]}" / author.AUTHOR_SKILL
    directory.mkdir(parents=True)
    body = f"---\nname: {author.AUTHOR_SKILL}\ndescription: Use when writing an execution plan\n---\n{text}\n"
    (directory / "SKILL.md").write_text(body, encoding="utf-8")
    return skills.pack(directory).data


def publish_skill(client, hub, tmp_path, text: str) -> tuple[bytes, dict]:
    data = skill_bundle(tmp_path, text)
    return data, publish(client, {ADMIN: hub["admin"]}, ADMIN, data, author.AUTHOR_SKILL, None)


def author_runs(db) -> int:
    return count_runs(db, tables.runs.c.kind == "author")[0][0]


# Dispatching


def test_a_writer_dispatches_an_author_run_to_their_own_worker_and_a_reader_gets_403(client, hub, hub_db):
    worker = author_worker(client, hub["owner"], "mac-mini")
    run = dispatched_author(client, hub["owner"], worker["id"], model="opus", timeout_h=4)
    assert (run["kind"], run["plan_id"], run["step_key"], run["plan_revision"], run["repo"]) == (
        "author",
        "",
        None,
        None,
        None,
    )
    assert run["repos"] == [
        {"repo": HARNESS, "branch": None},
        {"repo": "agent-skills", "branch": None},
        {"repo": "evo-agents", "branch": None},
    ], "the harness first, then the project's repos the worker has a checkout of, in name order"
    assert (run["requested_runtime"], run["runtime"], run["model"], run["mode"], run["approval"]) == (
        "claude-code",
        "claude-code",
        "opus",
        "headless",
        "auto",
    )
    assert (run["timeout_min"], run["pinned_worker_id"], run["dispatched_by"], run["state"]) == (
        240,
        worker["id"],
        OWNER,
        "queued",
    )
    assert run["title"] == "Plan from: Members write plans on the hub with an agent."
    assert run["request"] == REQUEST and run["dispatched_via"] == "machine"

    reader = dispatch_author(client, hub["reader"], worker["id"])
    assert reader.status_code == 403
    assert reader.json()["message"] == (
        f"dispatching runs in project {PROJECT} needs the writer role on it; you hold reader"
    )
    elsewhere = dispatch_author(client, hub["other"], worker["id"])
    assert elsewhere.status_code == 403
    assert "a run goes only to a worker of the member who dispatches it" in elsewhere.json()["message"]
    assert author_runs(hub_db) == 1

    (row,) = [row for row in audit_rows(hub_db, "run") if row[0] == "run.dispatch_author"]
    assert row == ("run.dispatch_author", f"{PROJECT}/author run:{run['id']}", OWNER, PROJECT)
    assert all("Members write plans" not in str(item) for item in audit_rows(hub_db, "run"))

    listed = client.get(f"/v1/projects/{PROJECT}/runs", headers=hub["reader"]).json()
    assert [item["id"] for item in listed["runs"] if item["kind"] == "author"] == [run["id"]]
    shown = client.get(f"/v1/projects/{PROJECT}/runs/{run['id']}", headers=hub["reader"])
    assert shown.status_code == 200 and shown.json()["request"] == REQUEST
    stranger = client.get(f"/v1/projects/{PROJECT}/runs/{run['id']}", headers=hub["stranger"])
    assert stranger.status_code == 404

    client.post(f"/v1/projects/{PROJECT}/runs/{run['id']}/cancel", headers=hub["owner"])
    rerun = client.post(f"/v1/projects/{PROJECT}/runs/{run['id']}/rerun", headers=hub["owner"])
    assert rerun.status_code == 409 and "is an author run: dispatch a new one" in rerun.json()["message"]


def test_an_author_run_on_another_runtime_or_with_a_request_over_16_kib_is_422_saying_why(client, hub, hub_db):
    worker = author_worker(client, hub["owner"], "mac-mini")
    for runtime in ("opencode", "codex"):
        refused = dispatch_author(client, hub["owner"], worker["id"], runtime=runtime)
        assert refused.status_code == 422, refused.text
        assert refused.json()["message"].startswith(f"an author run runs on claude-code only, not {runtime}: ")
    over = "é" * (author.MAX_REQUEST_BYTES // 2 + 1)
    refused = dispatch_author(client, hub["owner"], worker["id"], request=over)
    assert refused.status_code == 422
    assert refused.json()["message"] == (
        f"the request is {len(over.encode())} bytes of UTF-8, over the 16384 an author run takes; nothing was "
        "dispatched"
    )
    assert dispatch_author(client, hub["owner"], worker["id"], request=" \n").status_code == 422
    assert author_runs(hub_db) == 0

    whole = dispatched_author(client, hub["owner"], worker["id"], request="x" * author.MAX_REQUEST_BYTES, runtime="any")
    assert whole["runtime"] == "claude-code" and len(whole["request"]) == author.MAX_REQUEST_BYTES


def test_an_author_run_needs_a_worker_with_the_harness_and_a_daemon_that_runs_author_runs(client, hub, hub_db):
    without_harness = author_worker(client, hub["owner"], "no-harness", checkouts=NO_HARNESS)
    refused = dispatch_author(client, hub["owner"], without_harness["id"])
    assert refused.status_code == 409
    assert f"it has no checkout of {PROJECT}/{HARNESS}" in refused.json()["message"]

    old_daemon = author_worker(client, hub["owner"], "old-daemon", kinds=OLD_KINDS)
    refused = dispatch_author(client, hub["owner"], old_daemon["id"])
    assert refused.status_code == 409
    assert "does not say it runs author runs: upgrade it and restart its daemon" in refused.json()["message"]

    sql(
        hub_db,
        update(tables.projects)
        .values(cluster=None, workspace=None, harness_path=None)
        .where(tables.projects.c.name == PROJECT),
    )
    good = author_worker(client, hub["owner"], "mac-mini")
    refused = dispatch_author(client, hub["owner"], good["id"])
    assert refused.status_code == 409
    assert f"project {PROJECT} was registered without its harness" in refused.json()["message"]
    assert author_runs(hub_db) == 0


# The claim


def test_only_the_owners_author_daemon_with_the_harness_claims_an_author_run_with_the_latest_skill(
    client, hub, hub_db, tmp_path
):
    publish_skill(client, hub, tmp_path, "Version one.")
    data, published = publish_skill(client, hub, tmp_path, "Version two: the author mode.")
    assert published["latest"]["version"] == 2
    mine = author_worker(client, hub["owner"], "mac-mini")
    theirs = author_worker(client, hub["other"], "their-mac")
    run_id = dispatched_author(client, hub["owner"], mine["id"])["id"]

    assert claim(client, theirs) is None, "another member's worker never gets the run"
    beat(client, mine, None)  # the daemon of 0.8.0 names no kind
    assert claim(client, mine) is None
    beat(client, mine, OLD_KINDS)
    assert claim(client, mine) is None
    beat(client, mine, KINDS, NO_HARNESS)
    assert claim(client, mine) is None, "no claim without a checkout of the harness"
    assert state_of(hub_db, run_id) == "queued"

    beat(client, mine, KINDS)
    spec = claim(client, mine)
    assert (spec["id"], spec["kind"], spec["runtime"], spec["plan"]) == (run_id, "author", "claude-code", None)
    (skill,) = spec["skills"]
    assert (skill["name"], skill["scope"], skill["version"], skill["size"]) == (
        author.AUTHOR_SKILL,
        "global",
        2,
        len(data),
    )
    assert skill["sha256"] == hashlib.sha256(data).hexdigest()
    downloaded, headers = download(skill["url"])
    assert downloaded == data and "create-exec-plan-v2.tar.gz" in headers["Content-Disposition"]
    prompt = spec["prompt"]
    assert "(EVO_RUN_KIND=author)" in prompt and "wrote version 2 of it" in prompt
    assert "Do not run `evo-agents hub plan`" in prompt and "AskUserQuestion" in prompt
    assert prompt.endswith(f"# The request of {OWNER}\n{REQUEST}\n")
    assert f"- {HARNESS}: {HARNESS}/" in prompt


def test_an_author_run_claimed_while_the_hub_has_no_create_exec_plan_skill_fails_saying_so(client, hub, hub_db):
    worker = author_worker(client, hub["owner"], "mac-mini")
    run_id = dispatched_author(client, hub["owner"], worker["id"])["id"]
    assert claim(client, worker) is None
    state, error, worker_id = of_run(hub_db, run_id, "state", "error", "worker_id")[0]
    assert (state, worker_id) == ("failed", None)
    assert error.startswith("the hub holds no global skill create-exec-plan, which the agent of an author run writes")
    assert "evo-agents hub skills publish" in error
    assert [move[1:] for move in moves(hub_db, run_id)] == [("queued", "failed", "reaper")]


def test_an_author_run_ends_done_without_verify_and_writes_no_plan(client, hub, hub_db, tmp_path):
    publish_skill(client, hub, tmp_path, "The author mode.")
    worker = author_worker(client, hub["owner"], "mac-mini")
    run_id = dispatched_author(client, hub["owner"], worker["id"])["id"]
    assert claim(client, worker)["id"] == run_id
    plans = tables.plans
    before = sql(hub_db, select(plans.c.plan_id, plans.c.revision).order_by(plans.c.plan_id))
    running = report(client, worker, run_id, "running")
    assert running.status_code == 200, running.text
    moved(client, worker, run_id, "verifying")
    refused = report(client, worker, run_id, "review")
    assert refused.status_code == 409 and "is an author run: report done or failed" in refused.json()["message"]
    step = client.post(f"/v1/worker/runs/{run_id}/steps/1", json={"status": "pending"}, headers=worker["headers"])
    assert step.status_code == 404 and f"run {run_id} is an author run, which has no plan" in step.json()["message"]
    done = moved(client, worker, run_id, "done", summary="Wrote plan wait-helper.")
    assert done["state"] == "done" and done["verify"] is None
    assert sql(hub_db, select(plans.c.plan_id, plans.c.revision).order_by(plans.c.plan_id)) == before

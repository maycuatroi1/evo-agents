"""Plan runs on the hub: dispatching a whole plan to one worker, the claim of a plan run, the worker reading the plan
and reporting each step, how a plan run ends, and how it and the runs of single steps exclude each other.

The checks step 3 of the plan-runs-and-decisions plan names: a reader gets 403, and so does a dispatch to another
member's worker; of two plan runs dispatched at once only one gets in; a step is not dispatched while its plan has a
plan run, nor a plan run while a step has a run (the tests named exclusive); a worker without a checkout of one of the
run's repos does not claim it; a step reported done with a verify command that exited other than 0 gets 422; a step
report from a worker that does not hold the run gets 404; a plan run that fails sets the steps it left in progress back
to pending."""

import threading
import time

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient

from evo_agents.hub import runs
from evo_agents.hub.server import runs as run_routes
from evo_agents.hub.server.app import create_app
from tests.hub.live import sql
from tests.hub.test_runs import (
    OWNER,
    PASSED,
    PROJECT,
    PROTOCOL,
    SHA,
    add_worker,
    audit_rows,
    claim,
    control,
    expire,
    members,
    moved,
    moves,
    recover,
    report,
    state_of,
    today,
)

PLAN = "fleet"
REPO_NAMES = ("evo-agents", "agent-skills")
CHECKOUTS = {f"{PROJECT}/{name}": {"path": f"/src/{name}", "branch": "main"} for name in REPO_NAMES}
REPOS = [{"repo": "evo-agents", "branch": "feat/plan-runs"}, {"repo": "agent-skills", "branch": "main"}]
FAILING = [{"command": "python -m pytest -q", "exit_code": 0}, {"command": "ruff check .", "exit_code": 1}]


def plan_body(plan_id: str = PLAN) -> dict:
    return {
        "id": plan_id,
        "title": "Run the fleet",
        "goal": "Hand a whole plan to one worker.",
        "repos": [
            {"repo": "evo-agents", "branch": "feat/plan-runs"},
            {"repo": "agent-skills", "branch": "main"},
            {"repo": "evo-cli", "branch": "feat/old"},
        ],
        "steps": [
            {"id": 1, "title": "Model", "repo": "evo-agents", "what": "the model", "status": "done", "evidence": "ok"},
            {"id": 2, "title": "API", "repo": "evo-agents", "what": "the api", "status": "pending", "depends_on": [1]},
            {
                "id": 3,
                "title": "Skill",
                "repo": "agent-skills",
                "what": "the skill",
                "status": "pending",
                "depends_on": [2],
            },
            {
                "id": 4,
                "title": "Docs",
                "repo": "evo-agents",
                "what": "the docs",
                "status": "in_progress",
                "note": "elsewhere",
            },
            {"id": 5, "title": "CLI", "repo": "evo-cli", "what": "the cli", "status": "done"},
        ],
    }


@pytest.fixture
def client(hub_db, tmp_path, github):
    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github)), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github) -> dict:
    """The project and members of test_runs' ``hub``, with the plan fleet pushed by owner: five steps over three repos,
    two of them done, one in progress elsewhere, and two pending."""
    headers = members(client, github)
    push(client, headers["owner"], plan_body())
    return headers


def push(client, headers, body: dict) -> dict:
    response = client.put(f"/v1/projects/{PROJECT}/plans/{body['id']}", json={"body": body}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def plan_run(client, headers, plan_id: str = PLAN, **extra):
    return client.post(f"/v1/projects/{PROJECT}/plan-runs", json={"plan_id": plan_id, **extra}, headers=headers)


def dispatched_plan(client, headers, plan_id: str = PLAN, **extra) -> dict:
    response = plan_run(client, headers, plan_id, **extra)
    assert response.status_code == 201, response.text
    return response.json()


def dispatch_steps(client, headers, steps, plan_id: str = PLAN):
    return client.post(f"/v1/projects/{PROJECT}/runs", json={"plan_id": plan_id, "steps": steps}, headers=headers)


def fleet_worker(client, headers, name: str, **extra) -> dict:
    """A worker with a checkout of both repos of the plan run, unless ``extra`` says otherwise."""
    return add_worker(client, headers, name, **{"checkouts": CHECKOUTS, **extra})


def step_report(client, worker: dict, run_id: int, key, status: str, **body):
    path = f"/v1/worker/runs/{run_id}/steps/{key}"
    return client.post(path, json={"status": status, **body}, headers=worker["headers"])


def reported(client, worker: dict, run_id: int, key, status: str, **body) -> dict:
    response = step_report(client, worker, run_id, key, status, **body)
    assert response.status_code == 200, response.text
    return response.json()


def held_plan(client, headers, plan_id: str = PLAN) -> dict:
    response = client.get(f"/v1/projects/{PROJECT}/plans/{plan_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def fleet_step(client, headers, key: int) -> dict:
    return next(item for item in held_plan(client, headers)["body"]["steps"] if item["id"] == key)


def ready(client, headers) -> dict:
    response = client.get(f"/v1/projects/{PROJECT}/plans/{PLAN}/ready-steps", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def started(client, hub, name: str = "mac-mini", **dispatch) -> tuple[dict, dict]:
    """A worker of owner with both checkouts, and a plan run of fleet it claimed and started."""
    worker = fleet_worker(client, hub["owner"], name)
    run = dispatched_plan(client, hub["owner"], **dispatch)
    assert claim(client, worker)["id"] == run["id"]
    moved(client, worker, run["id"], "running")
    return worker, run


def wait_for_lock_waiters(db, count: int, timeout: float = 10.0) -> None:
    """Return once ``count`` sessions wait for an advisory lock; fail after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    query = "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
    while sql(db, query)[0][0] < count:
        assert time.monotonic() < deadline, f"{count} requests never waited for the plan's lock"
        time.sleep(0.05)


def race(client, db, *calls) -> list:
    """Run ``calls`` at once while the plan's dispatch lock is held, release it once each waits for it, and return
    their answers in order: whichever gets the lock first, the others see its run."""
    project_id = sql(db, "SELECT id FROM projects WHERE name = %s", (PROJECT,))[0][0]
    key = run_routes.plan_lock_key(project_id, PLAN)
    answers = [None] * len(calls)

    def call(index):
        answers[index] = calls[index]()

    with pg.admin(db.admin_dsn) as conn:
        conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (key,))
        threads = [threading.Thread(target=call, args=(index,)) for index in range(len(calls))]
        for thread in threads:
            thread.start()
        wait_for_lock_waiters(db, len(calls))
        conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))
        for thread in threads:
            thread.join(30)
    return answers


# Dispatching a plan run


def test_a_plan_run_needs_the_writer_role_and_a_worker_of_the_callers_own(client, hub, hub_db):
    for who, status in (("reader", 403), ("admin", 403), ("stranger", 404)):
        response = plan_run(client, hub[who])
        assert response.status_code == status, (who, response.text)
    assert "writer role" in plan_run(client, hub["reader"]).json()["message"]
    mine = fleet_worker(client, hub["owner"], "mac-mini")
    refused = plan_run(client, hub["other"], worker_id=mine["id"])
    assert refused.status_code == 403 and "worker of the member who dispatches" in refused.json()["message"]
    nobodys = plan_run(client, hub["other"], worker_id=999999)
    assert nobodys.status_code == 403
    assert nobodys.json()["message"].replace("999999", str(mine["id"])) == refused.json()["message"]
    assert sql(hub_db, "SELECT count(*) FROM runs") == [(0,)]
    assert client.post(f"/v1/workers/{mine['id']}/revoke", headers=hub["owner"]).status_code == 200
    revoked = plan_run(client, hub["owner"], worker_id=mine["id"])
    assert revoked.status_code == 409 and "revoked" in revoked.json()["message"]
    for extra in ({"timeout_h": 3}, {"timeout_h": 48}, {"model": "opus\nfast"}, {"model": ""}, {"runtime": "gemini"}):
        assert plan_run(client, hub["owner"], **extra).status_code == 422, extra
    assert plan_run(client, hub["owner"], plan_id="nothing").status_code == 404
    assert sql(hub_db, "SELECT count(*) FROM runs") == [(0,)]


def test_a_plan_run_holds_the_repos_of_the_steps_not_done_with_their_branches(client, hub, hub_db):
    mine = fleet_worker(client, hub["owner"], "mac-mini")
    run = dispatched_plan(
        client, hub["owner"], worker_id=mine["id"], runtime="claude-code", model="claude-opus-4-1", timeout_h=8
    )
    assert {key: run[key] for key in ("kind", "step_key", "repo", "branch", "repos", "title", "state")} == {
        "kind": "plan",
        "step_key": None,
        "repo": None,
        "branch": None,
        "repos": REPOS,  # evo-cli has no step left to do; agent-skills keeps the branch the plan names
        "title": "Run the fleet",
        "state": "queued",
    }
    assert {
        key: run[key] for key in ("model", "timeout_min", "run_seconds", "approval", "mode", "pinned_worker_id")
    } == {
        "model": "claude-opus-4-1",
        "timeout_min": 8 * 60,
        "run_seconds": 0,
        "approval": "auto",
        "mode": "headless",
        "pinned_worker_id": mine["id"],
    }
    assert (run["requested_runtime"], run["dispatched_by"]) == ("claude-code", OWNER)
    assert run["plan_revision"] == held_plan(client, hub["owner"])["revision"]
    assert audit_rows(hub_db, "run") == [("run.dispatch_plan", f"{PROJECT}/{PLAN} run:{run['id']}", OWNER, PROJECT)]
    shown = client.get(f"/v1/projects/{PROJECT}/runs/{run['id']}", headers=hub["reader"]).json()
    assert shown == run
    listed = client.get(f"/v1/projects/{PROJECT}/runs", params={"plan_id": PLAN}, headers=hub["reader"]).json()
    assert [(item["id"], item["kind"], item["repos"], item["model"]) for item in listed["runs"]] == [
        (run["id"], "plan", REPOS, "claude-opus-4-1")
    ]
    default = sql(hub_db, "SELECT timeout_s FROM runs WHERE id = %s", (run["id"],))
    assert default == [(8 * 3600,)]


def test_a_plan_without_a_pending_step_or_with_a_step_without_a_repo_gets_409(client, hub, hub_db):
    quiet = plan_body("quiet")
    for item in quiet["steps"]:
        item["status"] = "in_progress" if item["status"] == "pending" else item["status"]
    push(client, hub["owner"], quiet)
    refused = plan_run(client, hub["owner"], plan_id="quiet")
    assert refused.status_code == 409 and "has no pending step" in refused.json()["message"]
    loose = plan_body("loose")
    del loose["steps"][2]["repo"]  # step 3, not done, names no repo, and the plan lists three
    push(client, hub["owner"], loose)
    unnamed = plan_run(client, hub["owner"], plan_id="loose")
    assert (
        unnamed.status_code == 409 and "step 3 of plan loose is not done and names no repo" in unnamed.json()["message"]
    )
    assert sql(hub_db, "SELECT count(*) FROM runs") == [(0,)]
    single = plan_body("single")
    single["repos"] = [{"repo": "evo-agents", "branch": "feat/one"}]
    for item in single["steps"]:
        item.pop("repo")
    push(client, hub["owner"], single)
    assert dispatched_plan(client, hub["owner"], plan_id="single")["repos"] == [
        {"repo": "evo-agents", "branch": "feat/one"}
    ]


def test_two_plan_runs_dispatched_at_once_give_one_run(client, hub, hub_db):
    answers = race(client, hub_db, lambda: plan_run(client, hub["owner"]), lambda: plan_run(client, hub["other"]))
    assert sorted(answer.status_code for answer in answers) == [201, 409], [answer.text for answer in answers]
    winner = next(answer.json() for answer in answers if answer.status_code == 201)
    loser = next(answer.json() for answer in answers if answer.status_code == 409)
    assert f"plan {PLAN} has plan run #{winner['id']}, queued" in loser["message"]
    assert sql(hub_db, "SELECT count(*) FROM runs WHERE kind = 'plan'") == [(1,)]
    # without the lock in the way, the second still gets 409, from the active run it sees
    again = plan_run(client, hub["owner"])
    assert again.status_code == 409 and f"plan run #{winner['id']}" in again.json()["message"]


# A plan run and the runs of its steps exclude each other


def test_exclusive_a_step_is_not_dispatched_while_its_plan_has_a_plan_run_and_the_other_way(client, hub, hub_db):
    run = dispatched_plan(client, hub["owner"])
    for who in ("owner", "other"):
        refused = dispatch_steps(client, hub[who], [2])
        assert refused.status_code == 409, refused.text
        assert f"plan {PLAN} has plan run #{run['id']}, queued, dispatched by {OWNER}" in refused.json()["message"]
    shown = ready(client, hub["reader"])
    assert shown["plan_run"] == {"id": run["id"], "state": "queued", "dispatched_by": OWNER}
    steps = {item["key"]: item for item in shown["steps"]}
    assert steps["2"]["ready"] is False and steps["2"]["reason"].startswith(f"plan {PLAN} has plan run #{run['id']}")
    assert steps["3"]["reason"] == "it waits for step 2 (pending)"  # a reason of its own comes first
    other_plan = dispatch_steps(client, hub["owner"], [2], plan_id="rollout")  # another plan: no lock in the way
    assert other_plan.status_code == 201, other_plan.text

    assert control(client, hub["owner"], run["id"], "cancel").json()["state"] == "cancelled"
    assert ready(client, hub["reader"])["plan_run"] is None
    step_run = dispatch_steps(client, hub["owner"], [2])
    assert step_run.status_code == 201, step_run.text
    step_id = step_run.json()[0]["id"]
    for who in ("owner", "other"):
        refused = plan_run(client, hub[who])
        assert refused.status_code == 409, refused.text
        assert f"step 2 of plan {PLAN} has run #{step_id}, queued" in refused.json()["message"]
    assert sql(hub_db, "SELECT count(*) FROM runs WHERE plan_id = %s AND kind = 'plan'", (PLAN,)) == [(1,)]


def test_exclusive_a_step_dispatch_and_a_plan_run_racing_for_the_lock_never_both_get_in(client, hub, hub_db):
    answers = race(
        client, hub_db, lambda: dispatch_steps(client, hub["owner"], [2]), lambda: plan_run(client, hub["owner"])
    )
    steps, plan = answers
    assert sorted((steps.status_code, plan.status_code)) == [201, 409], (steps.text, plan.text)
    if steps.status_code == 201:
        assert f"step 2 of plan {PLAN} has run #{steps.json()[0]['id']}" in plan.json()["message"]
    else:
        assert f"plan {PLAN} has plan run #{plan.json()['id']}" in steps.json()["message"]
    active = sql(hub_db, "SELECT kind FROM runs WHERE plan_id = %s AND state = 'queued'", (PLAN,))
    assert len(active) == 1


# The claim


def test_a_plan_run_is_claimed_only_by_a_worker_with_a_checkout_of_every_repo(client, hub, hub_db):
    half = fleet_worker(
        client, hub["owner"], "half-box", checkouts={f"{PROJECT}/evo-agents": CHECKOUTS[f"{PROJECT}/evo-agents"]}
    )
    run = dispatched_plan(client, hub["owner"], model="claude-opus-4-1")
    assert claim(client, half) is None and state_of(hub_db, run["id"]) == "queued"
    # the plan moves on after the dispatch: the claim hands out the plan as the hub holds it now
    revision = held_plan(client, hub["owner"])["revision"]
    patch = {"section": "steps", "step": 4, "updates": {"note": "edited after the dispatch"}, "if_revision": revision}
    assert client.patch(f"/v1/projects/{PROJECT}/plans/{PLAN}", json=patch, headers=hub["owner"]).status_code == 200
    full = fleet_worker(client, hub["owner"], "full-box")
    spec = claim(client, full)
    assert spec["id"] == run["id"] and state_of(hub_db, run["id"]) == "leased"
    assert {key: spec[key] for key in ("kind", "step_key", "repo", "branch", "repos", "model", "title")} == {
        "kind": "plan",
        "step_key": None,
        "repo": None,
        "branch": None,
        "repos": REPOS,
        "model": "claude-opus-4-1",
        "title": "Run the fleet",
    }
    assert (spec["approval"], spec["timeout_min"], spec["plan_revision"]) == ("auto", 4 * 60, revision)
    current = held_plan(client, hub["owner"])
    assert spec["plan"] == {"revision": revision + 1, "body": current["body"]}
    prompt = spec["prompt"]
    assert prompt == runs.build_plan_prompt(current["body"], REPOS)
    assert prompt.startswith(f"You are running the plan {PLAN} from the evo-agents hub, as one plan run on a worker")
    assert "- evo-agents: branch feat/plan-runs, worktree evo-agents" in prompt
    assert "- agent-skills: branch main, worktree agent-skills" in prompt
    assert "# Steps not done yet: 3 of 5" in prompt


def test_a_run_of_one_step_is_still_claimed_by_a_worker_with_its_repo_alone(client, hub):
    half = fleet_worker(
        client, hub["owner"], "half-box", checkouts={f"{PROJECT}/evo-agents": CHECKOUTS[f"{PROJECT}/evo-agents"]}
    )
    (run,) = dispatch_steps(client, hub["owner"], [2]).json()
    spec = claim(client, half)
    assert spec["id"] == run["id"] and spec["kind"] == "step" and spec["plan"] is None and spec["repos"] is None
    assert (spec["step_key"], spec["repo"], spec["branch"], spec["model"]) == (
        "2",
        "evo-agents",
        "feat/plan-runs",
        None,
    )


# The worker's side of a plan run


def test_the_worker_reads_the_plan_of_the_plan_run_it_holds_and_of_no_other_run(client, hub):
    worker, run = started(client, hub)
    answer = client.get(f"/v1/worker/runs/{run['id']}/plan", headers=worker["headers"])
    assert answer.status_code == 200, answer.text
    current = held_plan(client, hub["owner"])
    assert {key: answer.json()[key] for key in ("plan_id", "revision", "body", "updated_by")} == {
        "plan_id": PLAN,
        "revision": current["revision"],
        "body": current["body"],
        "updated_by": OWNER,
    }
    other = fleet_worker(client, hub["owner"], "linux-box")
    assert client.get(f"/v1/worker/runs/{run['id']}/plan", headers=other["headers"]).status_code == 404
    assert client.get("/v1/worker/runs/999999/plan", headers=worker["headers"]).status_code == 404
    (single,) = dispatch_steps(client, hub["owner"], [2], plan_id="rollout").json()
    assert claim(client, other)["id"] == single["id"]
    one_step = client.get(f"/v1/worker/runs/{single['id']}/plan", headers=other["headers"])
    assert one_step.status_code == 404 and "run of one step" in one_step.json()["message"]
    # a member's own token is no worker's
    as_member = {**hub["owner"], **PROTOCOL}
    assert client.get(f"/v1/worker/runs/{run['id']}/plan", headers=as_member).status_code == 403


def test_step_reports_write_the_plan_as_the_member_who_dispatched_the_run(client, hub, hub_db):
    worker, run = started(client, hub)
    run_id = run["id"]
    before = held_plan(client, hub["owner"])["revision"]
    first = reported(client, worker, run_id, 2, "in_progress", repo="evo-agents")
    assert first == {
        "run_id": run_id,
        "plan_id": PLAN,
        "step_key": "2",
        "status": "in_progress",
        "revision": before + 1,
        "written": True,
    }
    assert fleet_step(client, hub["owner"], 2)["note"] == f"run #{run_id} on worker mac-mini"
    failing = step_report(client, worker, run_id, 2, "done", verify=FAILING, commit_sha=SHA)
    assert failing.status_code == 422, failing.text
    assert "`ruff check .` exited 1" in failing.json()["message"]
    unverified = step_report(client, worker, run_id, 2, "done", commit_sha=SHA)
    assert unverified.status_code == 422 and "verify commands" in unverified.json()["message"]
    assert fleet_step(client, hub["owner"], 2)["status"] == "in_progress"
    done = reported(client, worker, run_id, 2, "done", verify=PASSED, commit_sha=SHA, evidence="Added the routes.")
    assert (done["status"], done["revision"], done["written"]) == ("done", before + 2, True)
    finished = fleet_step(client, hub["owner"], 2)
    expected = (
        f"run #{run_id} on worker mac-mini (claude-code, attempt 1): evo-agents@{SHA[:12]} on feat/plan-runs.\n"
        "verify: `python -m pytest -q` exit 0; `ruff check .` exit 0.\nAdded the routes."
    )
    assert (finished["status"], finished["done_at"], finished["evidence"]) == ("done", today(), expected)
    resend = reported(client, worker, run_id, 2, "done", verify=PASSED, commit_sha=SHA, evidence="Added the routes.")
    assert (resend["status"], resend["revision"], resend["written"]) == ("done", before + 2, False)
    back = step_report(client, worker, run_id, 2, "in_progress")
    assert back.status_code == 409 and "done already" in back.json()["message"]
    # the repo comes from the plan when the report names none; a step handed back keeps what was done of it
    handed = reported(client, worker, run_id, 3, "pending", evidence="Half of the skill is written.")
    assert (handed["status"], handed["written"]) == ("pending", True)
    third = fleet_step(client, hub["owner"], 3)
    assert third["note"] == f"run #{run_id} on worker mac-mini handed it back"
    assert third["evidence"].endswith("Half of the skill is written.")
    unknown = step_report(client, worker, run_id, 99, "in_progress")
    assert unknown.status_code == 404 and "no step '99'" in unknown.json()["message"]
    elsewhere = step_report(client, worker, run_id, 3, "in_progress", repo="evo-cli")
    assert (
        elsewhere.status_code == 422
        and "works in evo-agents, agent-skills, not in evo-cli" in elsewhere.json()["message"]
    )
    assert step_report(client, worker, run_id, 3, "blocked").status_code == 422
    assert step_report(client, worker, run_id, 3, "done", verify=PASSED, commit_sha="abc").status_code == 422

    revisions = client.get(f"/v1/projects/{PROJECT}/plans/{PLAN}/revisions", headers=hub["reader"]).json()
    assert [(r["revision"], r["actor"], r["summary"]) for r in revisions[:3]] == [
        (before + 3, OWNER, "step 3: set note, evidence"),
        (before + 2, OWNER, "step 2: status in_progress -> done; set done_at, evidence"),
        (before + 1, OWNER, "step 2: status pending -> in_progress; set note"),
    ]
    assert sql(hub_db, "SELECT DISTINCT token_id FROM audit WHERE action = 'plan.patch'") == [(worker["token_id"],)]
    assert [row for row in audit_rows(hub_db, "run") if row[0] == "run.step_report"] == [
        ("run.step_report", f"{PROJECT}/{PLAN}#2 run:{run_id} status=in_progress", OWNER, PROJECT),
        ("run.step_report", f"{PROJECT}/{PLAN}#2 run:{run_id} status=done", OWNER, PROJECT),
        ("run.step_report", f"{PROJECT}/{PLAN}#3 run:{run_id} status=pending", OWNER, PROJECT),
    ]
    events = sql(hub_db, "SELECT body FROM run_events WHERE run_id = %s AND kind = 'system' ORDER BY seq", (run_id,))
    assert [body[0]["text"] for body in events] == [
        "step 2: in_progress",
        f"step 2: done (evo-agents@{SHA[:12]})",
        "step 3: pending",
    ]
    assert events[1][0]["step_report"] == {"step": "2", "status": "done", "repo": "evo-agents", "commit_sha": SHA}


def test_a_step_report_from_a_worker_that_does_not_hold_the_run_gets_404(client, hub):
    worker, run = started(client, hub)
    mine = fleet_worker(client, hub["owner"], "linux-box")
    theirs = fleet_worker(client, hub["other"], "their-box")
    for other in (mine, theirs):
        refused = step_report(client, other, run["id"], 2, "in_progress")
        assert refused.status_code == 404 and "does not hold run" in refused.json()["message"]
    assert step_report(client, worker, 999999, 2, "in_progress").status_code == 404
    (single,) = dispatch_steps(client, hub["owner"], [2], plan_id="rollout").json()
    assert claim(client, mine)["id"] == single["id"]
    one_step = step_report(client, mine, single["id"], 2, "in_progress")
    assert one_step.status_code == 404 and "run of one step" in one_step.json()["message"]
    moved(client, worker, run["id"], "failed", error="the agent stopped")
    assert step_report(client, worker, run["id"], 2, "in_progress").status_code == 404  # it holds the run no more
    assert fleet_step(client, hub["owner"], 2)["status"] == "pending"


def test_a_step_report_the_plan_cannot_take_gets_its_error_and_keeps_nothing(client, hub, hub_db):
    worker, run = started(client, hub)
    grants = f"/v1/admin/projects/{PROJECT}/grants/{OWNER}"
    assert client.put(grants, json={"role": "reader", "max_level": "internal"}, headers=hub["admin"]).status_code == 200
    refused = step_report(client, worker, run["id"], 2, "in_progress")
    assert refused.status_code == 403, refused.text
    assert refused.json()["message"] == (
        f"step 2 of plan {PLAN} was not written: {OWNER} no longer holds the writer role on {PROJECT}"
    )
    assert fleet_step(client, hub["owner"], 2)["status"] == "pending"
    assert sql(hub_db, "SELECT count(*) FROM run_events WHERE run_id = %s AND kind = 'system'", (run["id"],)) == [(0,)]
    assert [row for row in audit_rows(hub_db, "run") if row[0] == "run.step_report"] == []


# How a plan run ends


def test_a_plan_run_that_fails_sets_the_steps_it_left_in_progress_back_to_pending(client, hub, hub_db):
    worker, run = started(client, hub)
    run_id = run["id"]
    assert fleet_step(client, hub["owner"], 2)["status"] == "pending"  # starting a plan run writes no step
    reported(client, worker, run_id, 2, "in_progress")
    reported(client, worker, run_id, 2, "done", verify=PASSED, commit_sha=SHA)
    reported(client, worker, run_id, 3, "in_progress")
    failed = moved(client, worker, run_id, "failed", error="the agent stopped")
    assert failed["state"] == "failed"
    back = fleet_step(client, hub["owner"], 3)
    assert (back["status"], back["note"]) == ("pending", f"run #{run_id} failed: the agent stopped")
    assert fleet_step(client, hub["owner"], 2)["status"] == "done"  # done is never set back
    # step 4 was in progress before the run, and the run never reported it: it stays as it was
    assert {key: fleet_step(client, hub["owner"], 4)[key] for key in ("status", "note")} == {
        "status": "in_progress",
        "note": "elsewhere",
    }

    # a cancelled plan run does the same, with the note of a cancel
    second = dispatched_plan(client, hub["owner"])
    assert claim(client, worker)["id"] == second["id"]
    moved(client, worker, second["id"], "running")
    reported(client, worker, second["id"], 3, "in_progress")
    asked = control(client, hub["owner"], second["id"], "cancel")
    assert asked.status_code == 200 and asked.json()["state"] == "running"
    moved(client, worker, second["id"], "cancelled")
    back = fleet_step(client, hub["owner"], 3)
    assert (back["status"], back["note"]) == (
        "pending",
        f"run #{second['id']} was cancelled: its owner asked to cancel it",
    )


def test_a_plan_run_ends_done_without_verify_results_and_writes_no_step(client, hub, hub_db):
    worker, run = started(client, hub)
    run_id = run["id"]
    reported(client, worker, run_id, 2, "in_progress")
    moved(client, worker, run_id, "verifying")
    review = report(client, worker, run_id, "review")
    assert review.status_code == 409 and "plan run: report done or failed" in review.json()["message"]
    failing = report(client, worker, run_id, "done", verify=FAILING)
    assert failing.status_code == 409 and "exited 0" in failing.json()["message"]
    revision = held_plan(client, hub["owner"])["revision"]
    done = moved(client, worker, run_id, "done", summary="Step 2 is left in progress for the next run.")
    assert done["state"] == "done" and done["finished_at"] is not None
    assert done["evidence"] == (
        f"run #{run_id} on worker mac-mini (claude-code, attempt 1).\nStep 2 is left in progress for the next run."
    )
    assert held_plan(client, hub["owner"])["revision"] == revision  # the end of a plan run writes no step
    assert fleet_step(client, hub["owner"], 2)["status"] == "in_progress"
    assert moves(hub_db, run_id)[-1][1:] == ("verifying", "done", "worker")
    rerun = control(client, hub["owner"], run_id, "rerun")
    assert rerun.status_code == 409 and "is a plan run" in rerun.json()["message"]


def test_a_lost_plan_run_is_tried_again_as_a_plan_run_and_the_last_attempt_gives_its_steps_back(client, hub, hub_db):
    worker, run = started(client, hub, model="claude-opus-4-1")
    first = run["id"]
    reported(client, worker, first, 2, "in_progress")
    expire(hub_db, first)
    assert recover(client) == {"lost": 1, "failed": 0, "cancelled": 0}
    ((second,),) = sql(hub_db, "SELECT id FROM runs WHERE parent_run_id = %s", (first,))
    again = client.get(f"/v1/projects/{PROJECT}/runs/{second}", headers=hub["owner"]).json()
    assert {key: again[key] for key in ("kind", "step_key", "repos", "model", "attempt", "state", "timeout_min")} == {
        "kind": "plan",
        "step_key": None,
        "repos": REPOS,
        "model": "claude-opus-4-1",
        "attempt": 2,
        "state": "queued",
        "timeout_min": 4 * 60,
    }
    assert fleet_step(client, hub["owner"], 2)["status"] == "in_progress"  # the plan run goes on
    assert ready(client, hub["reader"])["plan_run"]["id"] == second
    spec = claim(client, worker)
    assert (spec["id"], spec["kind"], spec["attempt"]) == (second, "plan", 2)
    expire(hub_db, second)
    assert recover(client)["lost"] == 1
    ((third,),) = sql(hub_db, "SELECT id FROM runs WHERE parent_run_id = %s", (second,))
    assert claim(client, worker)["attempt"] == 3
    expire(hub_db, third)
    assert recover(client) == {"lost": 0, "failed": 1, "cancelled": 0}
    ((error,),) = sql(hub_db, "SELECT error FROM runs WHERE id = %s", (third,))
    back = fleet_step(client, hub["owner"], 2)  # reported by the first attempt, given back by the last
    assert (back["status"], back["note"]) == ("pending", f"run #{third} failed: {error}")
    assert ready(client, hub["reader"])["plan_run"] is None

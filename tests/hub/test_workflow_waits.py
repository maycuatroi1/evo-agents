"""A plan run waits on a run of a GitHub Actions workflow (``evo_agents.hub.server.workflow_waits``): the hub reads the
run on each pass of the reaper and records ``github.workflow_run`` when a job of it fails or it completes.

The checks: a wait that names the run alone wakes on its first failed job, before the run completes, with the failed
job in the message; the wait again for the end alone sleeps through that failure and wakes when the run completes; the
repo is the project's (by its name or owner/name) and the run one GitHub knows, else 422; and an installation that
does not grant actions read refuses the wait with what to do instead. GitHub is the fake of ``tests.hub.fake_github``.
"""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import hub_events, tables
from evo_agents.hub.server import workflow_waits
from tests.hub.live import sql
from tests.hub.test_credentials_api import (  # noqa: F401 (app_key, client, config, hub: fixtures)
    MINE,
    THEIRS,
    app_key,
    claimable,
    client,
    config,
    hub,
    install,
    plan_run,
    reap,
    worker_of,
)
from tests.hub.test_decisions import SESSION, decision, inbox, run_view
from tests.hub.test_runs import claim, moved

READ_ACTIONS = {"contents": "write", "pull_requests": "write", "metadata": "read", "actions": "read"}
NO_ACTIONS = {"contents": "write", "pull_requests": "write", "metadata": "read"}
REASON = "CI of v0.58.0: fix forward on a red job, else go on once it published."
TEST_JOB = "ci / test (ubuntu-latest, 3.12)"
WINDOWS_JOB = "ci / test-windows (3.12)"


def ask_wait(client, worker: dict, run_id: int, contains: dict):
    body = {"reason": REASON, "on": workflow_waits.KIND, "contains": contains, "step_key": "1"}
    return client.post(f"/v1/worker/runs/{run_id}/waits", json=body, headers=worker["headers"])


def waited(client, worker: dict, run_id: int, contains: dict) -> dict:
    response = ask_wait(client, worker, run_id, contains)
    assert response.status_code == 201, response.text
    assert moved(client, worker, run_id, "waiting")["state"] == "waiting"
    return response.json()


def ci_run(github, status: str = "in_progress", conclusion: str | None = None, jobs=()):
    """A run of release.yml for v0.58.0 on evo-agents, which the fake holds as a repo."""
    github.add_repo(MINE, "evo-agents")
    return github.workflow_run(
        MINE,
        "evo-agents",
        workflow="release.yml",
        sha="a" * 40,
        tag="v0.58.0",
        status=status,
        conclusion=conclusion,
        jobs=jobs,
    )


def running(*names: str) -> list[dict]:
    return [{"name": name, "status": "in_progress", "conclusion": None} for name in names]


def failed(name: str) -> dict:
    return {"name": name, "conclusion": "failure", "step": "Run pytest"}


def events(db) -> list[dict]:
    e = tables.hub_events
    return [
        payload for (payload,) in sql(db, select(e.c.payload).where(e.c.kind == workflow_waits.KIND).order_by(e.c.id))
    ]


def a_running_plan_run(client, hub, github) -> tuple[dict, int]:
    claimable(client)
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id = plan_run(client, hub["owner"], worker)
    moved(client, worker, run_id, "running", session_id=SESSION)
    return worker, run_id


def test_a_wait_on_a_workflow_run_wakes_on_its_first_failed_job_and_one_for_its_end_alone_when_it_completes(
    client, hub, github, hub_db
):
    install(github, "evo-agents", permissions=READ_ACTIONS)
    run = ci_run(github, jobs=running(TEST_JOB, WINDOWS_JOB))
    worker, run_id = a_running_plan_run(client, hub, github)
    unknown = ask_wait(client, worker, run_id, {"repo": "evo-agents", "run_id": run.id + 1})
    assert unknown.status_code == 422 and f"GitHub knows no workflow run {run.id + 1}" in unknown.text

    wait = waited(client, worker, run_id, {"repo": "evo-agents", "run_id": str(run.id)})
    assert wait["wake_on"] == {
        "kind": "github.workflow_run",
        "contains": {"repo": f"{MINE}/evo-agents", "run_id": run.id},
    }
    reap(client)
    assert decision(client, hub["owner"], wait["id"])["state"] == "open"  # every job still runs
    assert run_view(client, hub["owner"], run_id)["state"] == "parked"
    assert events(hub_db) == []

    github.attempt(run.id, status="in_progress", conclusion=None, jobs=[*running(TEST_JOB), failed(WINDOWS_JOB)])
    reap(client)
    (payload,) = events(hub_db)
    assert payload == {
        "v": 1,
        "repo": f"{MINE}/evo-agents",
        "run_id": run.id,
        "attempt": 1,
        "workflow": "release",
        "head_branch": "v0.58.0",
        "head_sha": "a" * 40,
        "status": "in_progress",
        "conclusion": None,
        "failed": True,
        "failed_jobs": [WINDOWS_JOB],
        "url": f"https://github.com/{MINE}/evo-agents/actions/runs/{run.id}",
    }
    woke = decision(client, hub["owner"], wait["id"])
    assert woke["state"] == "answered", "the first failed job wakes it, before the run completes"
    assert "event github.workflow_run #" in woke["answer_text"] and WINDOWS_JOB in woke["answer_text"]
    assert workflow_waits.WOKE_HINT in woke["answer_text"]
    new_id = woke["answer_run_id"]
    assert claim(client, worker)["id"] == new_id
    moved(client, worker, new_id, "running")
    (message,) = inbox(client, worker, new_id)
    assert message["decision_id"] == wait["id"]

    # the agent waits again, for the end alone: the failure it woke on does not wake it again
    contains = {"repo": f"{MINE}/EVO-AGENTS", "run_id": run.id, "status": "completed"}
    end = waited(client, worker, new_id, contains)
    assert end["wake_on"]["contains"]["repo"] == f"{MINE}/evo-agents"
    reap(client)
    reap(client)
    assert decision(client, hub["owner"], end["id"])["state"] == "open"
    assert [item["status"] for item in events(hub_db)] == ["in_progress", "in_progress"]  # one more, after this wait

    github.attempt(run.id, status="completed", conclusion="failure", jobs=[{"name": TEST_JOB}, failed(WINDOWS_JOB)])
    reap(client)
    last = events(hub_db)[-1]
    assert (last["status"], last["conclusion"], last["failed"]) == ("completed", "failure", True)
    woke = decision(client, hub["owner"], end["id"])
    assert woke["state"] == "answered" and '"status": "completed"' in woke["answer_text"]
    reap(client)
    assert len(events(hub_db)) == 3, "a moment of an attempt is recorded once"


def test_a_wait_on_a_workflow_run_the_hub_cannot_read_gets_422(client, hub, github, hub_db):
    install(github, "evo-agents", permissions=NO_ACTIONS)
    run = ci_run(github)
    worker, run_id = a_running_plan_run(client, hub, github)
    for contains, says in (
        ({"repo": "evo-agents"}, "names its run: --contains run_id=ID"),
        ({"repo": "evo-agents", "run_id": "latest"}, "run_id of a workflow run is a positive integer"),
        (
            {"run_id": run.id},
            f"name the run's repo with --contains repo=OWNER/NAME: the project's are {THEIRS}/lumen-identity, "
            f"{MINE}/evo-agents",
        ),
        ({"repo": "northwind/evo-agents", "run_id": run.id}, "is not a GitHub repo of the project"),
        ({"repo": "evo-agents", "run_id": run.id}, "actions_permission: the App's installation on"),
    ):
        response = ask_wait(client, worker, run_id, contains)
        assert response.status_code == 422 and says in response.text, (contains, response.text)
    assert "wait with --until alone" in response.text
    d = tables.decisions
    assert sql(hub_db, select(d.c.id).where(d.c.category == "wait")) == []
    assert hub_events.WORKFLOW_RUN in hub_events.KINDS

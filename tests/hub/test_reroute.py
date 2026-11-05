"""A run whose worker lacks a program (``evo_agents.hub.server.runs.service.reroute``): it goes to the next worker with
no failure and no thread, and when none is left its triage agent stands on a worker it was tried on, installs the
program there and runs it again (``triage._install_worker``, ``triage_actions``), on its agent's pool: nobody picks
the machine of the rerun (plan agent-dedicated-pools)."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import tables
from tests.hub import live
from tests.hub.live import sql
from tests.hub.test_chat_runs import chat_worker
from tests.hub.test_runs import PLAN, PROJECT, claim, client, dispatched, hub, moved, notices, report, set_run  # noqa: F401
from tests.hub.test_shared_workers import share
from tests.hub.test_triage import count, root_of, row_of
from tests.hub.test_triage_actions import agent_of, rerun

LACKS = "preflight: step 2: verify command `pytest -q` calls pytest, which is not on this worker's PATH"
PROSE = (
    "preflight: step 2: verify command `tests pass` is not runnable as a shell command: it names no program: "
    "rewrite the verify as a command"
)


def workers(client, headers, *names: str) -> list[dict]:
    """Workers of the member ``headers`` names, nodes of the pool of the project's default agent, whose runs these
    are."""
    return [chat_worker(client, headers, name, slots=4, pool=PROJECT) for name in names]


def state_of(hub_db, run_id: int):
    r = tables.runs
    return sql(hub_db, select(r.c.state, r.c.worker_id, r.c.failure_cause).where(r.c.id == run_id))[0]


def next_of(hub_db, run_id: int):
    """(id, state, pinned worker, attempt) of the run that goes on from run ``run_id``."""
    r = tables.runs
    query = select(r.c.id, r.c.state, r.c.pinned_worker_id, r.c.attempt).where(r.c.parent_run_id == run_id)
    return sql(hub_db, query)


def lacks(client, worker: dict, run_id: int, error: str = LACKS) -> None:
    said = report(client, worker, run_id, "failed", error=error, failure_cause="missing_tool")
    assert said.status_code == 200, said.text


def started(client, worker: dict, headers, *, pinned: bool = False) -> int:
    """A run of step 2, as the member of ``headers``, that ``worker`` claimed (pinned to it, with ``pinned``); its
    id."""
    run_id = dispatched(client, headers, [2])[0]["id"]
    if pinned:  # a dispatch names no worker: the pin a run may carry, such as a chat run's, is set on its row
        set_run(live.database_of(client), run_id, pinned_worker_id=worker["id"])
    assert claim(client, worker)["id"] == run_id
    return run_id


def install_worker(hub_db, run_id: int) -> int:
    """The worker the chat run of the triage of ``run_id`` is pinned to."""
    r = tables.runs
    ((_, _, _, _, agent_run),) = row_of(hub_db, run_id)
    return sql(hub_db, select(r.c.pinned_worker_id).where(r.c.id == agent_run))[0][0]


def test_a_run_no_one_pinned_goes_to_the_next_worker_with_no_failure_notice_or_thread(client, hub, hub_db):
    first, second = workers(client, hub["owner"], "mac-mini", "mac-book")
    run_id = started(client, first, hub["owner"])
    lacks(client, first, run_id)
    assert state_of(hub_db, run_id) == ("lost", first["id"], "missing_tool")
    ((next_id, state, pinned, attempt),) = next_of(hub_db, run_id)
    assert (state, pinned, attempt) == ("queued", second["id"], 2)
    assert count(hub_db, tables.run_triage) == 0 and notices(hub_db, run_id) == []
    assert claim(client, first) is None
    assert claim(client, second)["id"] == next_id
    moved(client, second, next_id, "running")


def test_with_every_worker_tried_the_run_fails_and_its_agent_stands_on_a_worker_it_was_tried_on(client, hub, hub_db):
    first, second = workers(client, hub["owner"], "mac-mini", "mac-book")
    run_id = started(client, first, hub["owner"])
    lacks(client, first, run_id)
    ((next_id, *_),) = next_of(hub_db, run_id)
    assert claim(client, second)["id"] == next_id
    lacks(client, second, next_id)
    assert state_of(hub_db, next_id) == ("failed", second["id"], "missing_tool")
    assert next_of(hub_db, next_id) == []
    assert install_worker(hub_db, next_id) == first["id"]
    ((chat_id, root_id, *_),) = row_of(hub_db, next_id)
    body = root_of(client, hub, chat_id, root_id)["body"]
    assert "the run was tried on mac-mini, mac-book" in body and "The agent of this thread runs on mac-mini" in body
    assert "evo-agents worker triage rerun" in body
    agent = agent_of(client, first, next_id, hub_db)
    done = rerun(client, first, agent)
    assert done.status_code == 201, done.text
    r = tables.runs
    query = select(r.c.parent_run_id, r.c.state, r.c.pinned_worker_id).where(r.c.id == done.json()["run_id"])
    assert sql(hub_db, query) == [(next_id, "queued", None)]  # on the agent's pool, pinned to no worker


def test_the_agent_names_no_worker_for_the_rerun(client, hub, hub_db):
    first, second = workers(client, hub["owner"], "mac-mini", "mac-book")
    run_id = started(client, first, hub["owner"])
    lacks(client, first, run_id)
    ((next_id, *_),) = next_of(hub_db, run_id)
    assert claim(client, second)["id"] == next_id
    lacks(client, second, next_id)
    agent = agent_of(client, first, next_id, hub_db)
    refused = rerun(client, first, agent, worker="mac-book")
    assert refused.status_code == 422 and "worker" in str(refused.json()["detail"]), refused.text


def test_a_run_a_person_pinned_fails_there_and_its_agent_installs_on_that_worker(client, hub, hub_db):
    first, _ = workers(client, hub["owner"], "mac-mini", "mac-book")
    run_id = started(client, first, hub["owner"], pinned=True)
    lacks(client, first, run_id)
    assert state_of(hub_db, run_id)[0] == "failed" and next_of(hub_db, run_id) == []
    assert install_worker(hub_db, run_id) == first["id"]


def test_a_verify_that_is_no_command_does_not_move_since_it_fails_on_every_worker(client, hub, hub_db):
    first, _ = workers(client, hub["owner"], "mac-mini", "mac-book")
    run_id = started(client, first, hub["owner"])
    lacks(client, first, run_id, PROSE)
    assert state_of(hub_db, run_id)[0] == "failed" and next_of(hub_db, run_id) == []
    ((chat_id, root_id, *_),) = row_of(hub_db, run_id)
    assert "No worker with the program is left" not in root_of(client, hub, chat_id, root_id)["body"]


def test_a_program_missing_after_the_agent_started_does_not_move(client, hub, hub_db):
    first, _ = workers(client, hub["owner"], "mac-mini", "mac-book")
    run_id = started(client, first, hub["owner"])
    moved(client, first, run_id, "running")
    lacks(client, first, run_id)
    assert state_of(hub_db, run_id)[0] == "failed" and next_of(hub_db, run_id) == []


def test_another_cause_does_not_move(client, hub, hub_db):
    first, _ = workers(client, hub["owner"], "mac-mini", "mac-book")
    run_id = started(client, first, hub["owner"])
    said = report(client, first, run_id, "failed", error="no credential", failure_cause="credentials")
    assert said.status_code == 200 and next_of(hub_db, run_id) == []


def test_the_last_attempt_fails_with_workers_left(client, hub, hub_db):
    pool = workers(client, hub["owner"], "one", "two", "three", "four")
    run_id = started(client, pool[0], hub["owner"])
    held, last = pool[0], run_id
    for _ in range(2):
        lacks(client, held, last)
        ((last, _, pinned, _),) = next_of(hub_db, last)
        held = next(worker for worker in pool if worker["id"] == pinned)
        assert claim(client, held)["id"] == last
    lacks(client, held, last)
    assert state_of(hub_db, last)[0] == "failed" and next_of(hub_db, last) == []


def test_the_owners_own_worker_goes_before_a_shared_one_and_takes_the_install(client, hub, hub_db):
    own = chat_worker(client, hub["other"], "own", slots=4, pool=PROJECT)
    lab = chat_worker(client, hub["owner"], "lab", slots=4, pool=PROJECT)
    assert share(client, hub["owner"], lab).status_code == 200
    run_id = started(client, own, hub["other"])
    lacks(client, own, run_id)
    ((next_id, _, pinned, _),) = next_of(hub_db, run_id)
    assert pinned == lab["id"]
    assert claim(client, lab)["id"] == next_id
    lacks(client, lab, next_id)
    assert state_of(hub_db, next_id)[0] == "failed"
    assert install_worker(hub_db, next_id) == own["id"]


def test_the_agent_runs_a_plan_run_that_lacked_a_program_again_on_its_agents_pool(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "mac-mini", slots=4, pool=PROJECT)
    made = client.post(f"/v1/projects/{PROJECT}/plan-runs", json={"plan_id": PLAN}, headers=hub["owner"]).json()
    assert claim(client, worker)["id"] == made["id"]
    lacks(client, worker, made["id"])
    agent = agent_of(client, worker, made["id"], hub_db)
    done = rerun(client, worker, agent)
    assert done.status_code == 201, done.text
    r = tables.runs
    query = select(r.c.kind, r.c.state, r.c.pinned_worker_id, r.c.attempt, r.c.parent_run_id, r.c.plan_id)
    assert sql(hub_db, query.where(r.c.id == done.json()["run_id"])) == [
        ("plan", "queued", None, 1, made["id"], PLAN)  # on the agent's pool, pinned to no worker
    ]
    again = rerun(client, worker, agent)
    assert again.status_code in (409, 422), again.text


def test_the_agent_runs_a_plan_run_the_hub_answered_5xx_again(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "mac-mini", slots=4, pool=PROJECT)
    made = client.post(f"/v1/projects/{PROJECT}/plan-runs", json={"plan_id": PLAN}, headers=hub["owner"]).json()
    assert claim(client, worker)["id"] == made["id"]
    error = "the hub answered HTTP 500 to GET /v1/worker/runs/1/plan"
    assert report(client, worker, made["id"], "failed", error=error, failure_cause="hub_5xx").status_code == 200
    agent = agent_of(client, worker, made["id"], hub_db)
    done = rerun(client, worker, agent)
    assert done.status_code == 201, done.text
    r = tables.runs
    query = select(r.c.kind, r.c.state, r.c.parent_run_id).where(r.c.id == done.json()["run_id"])
    assert sql(hub_db, query) == [("plan", "queued", made["id"])]


def test_a_plan_run_that_failed_of_another_cause_is_still_the_owners_to_dispatch(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "mac-mini", slots=4, pool=PROJECT)
    made = client.post(f"/v1/projects/{PROJECT}/plan-runs", json={"plan_id": PLAN}, headers=hub["owner"]).json()
    assert claim(client, worker)["id"] == made["id"]
    assert report(client, worker, made["id"], "failed", error="reset", failure_cause="network").status_code == 200
    agent = agent_of(client, worker, made["id"], hub_db)
    refused = rerun(client, worker, agent)
    assert refused.status_code == 422 and "ask the owner to dispatch the plan again" in refused.json()["message"]

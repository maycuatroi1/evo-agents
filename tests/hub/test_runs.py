"""The run queue on the hub: ready steps, dispatch, claim, heartbeat, the worker's state reports, the owner's cancel,
approve and rerun, the reaper and the pruning of run events.

The checks step 5 of the worker-fleet plan names: two workers claiming at once get the run once; another member's
worker on the same project never gets one's run; another writer dispatching to one's worker gets 403, and so does a
reader dispatching at all; a step that is not ready gets 409; once the owner's writer grant is gone, claims skip the
run; an expired lease makes the run lost and queues it again, the third attempt fails and the step goes back to
pending; a revision conflict on the plan is tried again; approval auto writes a revision with the evidence; approval
review waits for the owner's approve. Around them: the claim's other conditions, the long poll waking on NOTIFY, the
heartbeat's control, cancel and rerun, revoking a pinned worker, and the periodic jobs.

And step 10 of the worker-credentials plan: each run records the credential it was dispatched with, a worker set to
take runs dispatched from the web only claims no other, and a token pinning a run to it gets 403."""

import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import extract, func, insert, literal, select, update

from evo_agents.harness import plan_digest
from evo_agents.hub import jobs, runs, tables
from evo_agents.hub.config import ConfigError, load_config
from evo_agents.hub.server import run_state
from evo_agents.hub.server import runs as run_routes
from evo_agents.hub.server.app import create_app
from evo_agents.hub.worker import queue
from tests.hub.fake_github import Account
from tests.hub.live import ADMIN, bearer, sql
from tests.hub.test_plans import registration
from tests.hub.test_web_auth import cookie, csrf_for, web_sign_in

PROJECT = "evo-agents"
PLAN = "rollout"
OWNER, OTHER, READER, STRANGER = "owner", "someone-else", "reader", "stranger"
PROTOCOL = {"X-Evo-Worker-Protocol": "1"}
HOST = {"hostname": "mac-mini.local", "os": "darwin", "arch": "arm64", "agent_version": "0.4.0"}
RUNTIMES = {"claude-code": {"available": True, "version": "2.1.289"}}
CHECKOUTS = {f"{PROJECT}/evo-agents": {"path": "/src/evo-agents", "branch": "main"}}
WRITER = {"role": "writer", "max_level": "internal"}
READ_ONLY = {"role": "reader", "max_level": "internal"}
SHA = "a" * 40
PASSED = [{"command": "python -m pytest -q", "exit_code": 0}, {"command": "ruff check .", "exit_code": 0}]


def plan_body() -> dict:
    return {
        "id": PLAN,
        "goal": "Ship the run queue.",
        "repos": [{"repo": "evo-agents", "branch": "feat/queue", "status": "pending"}],
        "steps": [
            {
                "id": 1,
                "title": "Model",
                "repo": "evo-agents",
                "what": "the run model",
                "status": "done",
                "evidence": "evo-agents@abc1234: 40 tests pass",
            },
            {
                "id": 2,
                "title": "Queue",
                "repo": "evo-agents",
                "what": "the queue",
                "verify": "test -f feature.txt",  # a program on any worker's PATH: the preflight checks it
                "status": "pending",
                "depends_on": [1],
            },
            {
                "id": 3,
                "title": "Daemon",
                "repo": "evo-agents",
                "what": "the daemon",
                "status": "pending",
                "depends_on": [2],
            },
            {"id": 4, "title": "Docs", "repo": "evo-agents", "what": "the docs", "status": "pending"},
            {"id": 5, "title": "Web", "repo": "evo-agents", "what": "the web", "status": "pending"},
        ],
    }


@pytest.fixture
def client(hub_db, tmp_path, github):
    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github)), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github) -> dict:
    """Project evo-agents with a hub sink, where owner and someone-else are writers and reader a reader, and the
    plan rollout pushed by owner: the headers of each member, a stranger and a hub admin without a grant."""
    return members(client, github)


LOGINS = {"admin": ADMIN, "owner": OWNER, "other": OTHER, "reader": READER, "stranger": STRANGER}
GITHUB_IDS = {login: number for number, login in enumerate(LOGINS.values(), start=601)}


def members(client, github) -> dict:
    """What the ``hub`` fixture sets up, on ``client``."""
    headers = {
        name: bearer(live.sign_in(client, github, login, GITHUB_IDS[login])["token"]) for name, login in LOGINS.items()
    }
    assert client.put(f"/v1/projects/{PROJECT}", json=registration(), headers=headers["admin"]).status_code == 200
    for login, grant in ((OWNER, WRITER), (OTHER, WRITER), (READER, READ_ONLY)):
        response = client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=grant, headers=headers["admin"])
        assert response.status_code == 200, response.text
    pushed = client.put(f"/v1/projects/{PROJECT}/plans/{PLAN}", json={"body": plan_body()}, headers=headers["owner"])
    assert pushed.status_code == 200, pushed.text
    return headers


def add_worker(client, headers, name: str, *, slots: int = 1, runtimes=None, checkouts=None) -> dict:
    """A worker of the member ``headers`` names, registered with its machine token, after its first heartbeat."""
    body = {"name": name, "projects": [PROJECT], "slots": slots, **HOST}
    response = client.post("/v1/workers", json=body, headers=headers)
    assert response.status_code == 201, response.text
    answer = response.json()
    worker = {
        "id": answer["worker"]["id"],
        "name": name,
        "token_id": answer["token_id"],
        "headers": {**bearer(answer["token"]), **PROTOCOL},
        "runtimes": RUNTIMES if runtimes is None else runtimes,
        "checkouts": CHECKOUTS if checkouts is None else checkouts,
    }
    beat(client, worker)
    return worker


def beat(client, worker: dict, runs_held=(), free_slots: int = 1) -> dict:
    body = {
        "runtimes": worker["runtimes"],
        "checkouts": worker["checkouts"],
        "free_slots": free_slots,
        "runs": list(runs_held),
    }
    response = client.post("/v1/worker/heartbeat", json=body, headers=worker["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def dispatch(client, headers, steps, **extra):
    return client.post(f"/v1/projects/{PROJECT}/runs", json={"plan_id": PLAN, "steps": steps, **extra}, headers=headers)


def dispatched(client, headers, steps, **extra) -> list[dict]:
    response = dispatch(client, headers, steps, **extra)
    assert response.status_code == 201, response.text
    return response.json()


def claim(client, worker: dict, wait: float = 0) -> dict | None:
    response = client.post("/v1/worker/claim", json={"wait_s": wait}, headers=worker["headers"])
    assert response.status_code == 200, response.text
    return response.json()["run"]


def report(client, worker: dict, run_id: int, state: str, **body):
    return client.post(f"/v1/worker/runs/{run_id}/state", json={"state": state, **body}, headers=worker["headers"])


def moved(client, worker: dict, run_id: int, *states: str, **body) -> dict:
    for state in states:
        response = report(client, worker, run_id, state, **(body if state == states[-1] else {}))
        assert response.status_code == 200, response.text
    return response.json()


def control(client, headers, run_id: int, action: str):
    return client.post(f"/v1/projects/{PROJECT}/runs/{run_id}/{action}", headers=headers)


def plan(client, headers) -> dict:
    response = client.get(f"/v1/projects/{PROJECT}/plans/{PLAN}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def step(client, headers, key: int) -> dict:
    return next(item for item in plan(client, headers)["body"]["steps"] if item["id"] == key)


def ready(client, headers) -> dict:
    response = client.get(f"/v1/projects/{PROJECT}/plans/{PLAN}/ready-steps", headers=headers)
    assert response.status_code == 200, response.text
    return {item["key"]: item for item in response.json()["steps"]}


def set_run(db, run_id: int, **values) -> None:
    """Write ``values`` into the columns of run ``run_id``, straight into the database."""
    sql(db, update(tables.runs).values(**values).where(tables.runs.c.id == run_id))


def of_run(db, run_id: int, *names: str) -> list[tuple]:
    """The columns ``names`` of run ``run_id``."""
    return sql(db, select(*(tables.runs.c[name] for name in names)).where(tables.runs.c.id == run_id))


def count_runs(db, *conditions) -> list[tuple]:
    """[(n,)]: how many runs match every one of ``conditions``."""
    return sql(db, select(func.count()).select_from(tables.runs).where(*conditions))


def expire(db, run_id: int) -> None:
    set_run(db, run_id, lease_expires_at=func.now() - timedelta(seconds=1))


def steady(db, worker: dict, seconds: int = runs.STEADY_SECONDS + 1) -> None:
    """As if the worker's heartbeats had come for ``seconds``, none late, the last one now: the clock of the steady
    rule, moved in the database rather than waited for."""
    w = tables.workers
    beaten = update(w).values(steady_since=func.now() - timedelta(seconds=seconds), last_heartbeat_at=func.now())
    sql(db, beaten.where(w.c.id == worker["id"]))


def worker_clock(db, worker: dict, **ages: float) -> None:
    """Set ``steady_since`` and ``last_heartbeat_at`` of the worker to that many seconds ago (``ages``)."""
    w = tables.workers
    values = {name: func.now() - timedelta(seconds=age) for name, age in ages.items()}
    sql(db, update(w).values(**values).where(w.c.id == worker["id"]))


def seconds_from_now(db, value: str | None) -> float | None:
    """How many seconds after the database's now() the timestamp ``value`` of an answer is."""
    if value is None:
        return None
    return float(sql(db, select(extract("epoch", literal(datetime.fromisoformat(value)) - func.now())))[0][0])


def recover(client) -> dict:
    return client.portal.call(run_state.recover_runs, client.app.state.engine)


def state_of(db, run_id: int) -> str:
    return of_run(db, run_id, "state")[0][0]


def moves(db, run_id: int) -> list[tuple]:
    events = tables.run_events
    query = select(events.c.seq, events.c.body).where(events.c.run_id == run_id, events.c.kind == "state")
    rows = sql(db, query.order_by(events.c.seq))
    return [(seq, body["from"], body["to"], body["actor"]) for seq, body in rows]


def audit_rows(db, family: str) -> list[tuple]:
    audit, users, projects = tables.audit, tables.users, tables.projects
    query = (
        select(audit.c.action, audit.c.target, users.c.login, projects.c.name)
        .join_from(audit, users, users.c.id == audit.c.actor_id)
        .outerjoin(projects, projects.c.id == audit.c.project_id)
        .where(audit.c.action.like(f"{family}.%"))
        .order_by(audit.c.id)
    )
    return sql(db, query)


def today() -> str:
    return datetime.now(UTC).date().isoformat()


# Ready steps


def test_ready_steps_lists_every_step_and_says_why_one_is_not_ready(client, hub):
    shown = ready(client, hub["reader"])
    assert list(shown) == ["1", "2", "3", "4", "5"]
    assert shown["2"] == {
        "key": "2",
        "title": "Queue",
        "repo": "evo-agents",
        "status": "pending",
        "ready": True,
        "reason": None,
        "active_run": None,
    }
    assert (shown["1"]["ready"], shown["1"]["reason"]) == (False, "its status is done, not pending")
    assert (shown["3"]["ready"], shown["3"]["reason"]) == (False, "it waits for step 2 (pending)")
    run = dispatched(client, hub["owner"], [2])[0]
    busy = ready(client, hub["reader"])["2"]
    assert busy["ready"] is False and busy["active_run"] == {"id": run["id"], "state": "queued", "dispatched_by": OWNER}
    assert busy["reason"] == f"it has run #{run['id']}, queued, dispatched by {OWNER}"
    # reading needs a grant: a stranger gets the 404 of a project it cannot see, a hub admin without one 403
    path = f"/v1/projects/{PROJECT}/plans/{PLAN}/ready-steps"
    assert client.get(path, headers=hub["stranger"]).status_code == 404
    assert client.get(path, headers=hub["admin"]).status_code == 403
    assert client.get(f"/v1/projects/{PROJECT}/plans/nothing/ready-steps", headers=hub["reader"]).status_code == 404


# Dispatch


def test_dispatch_needs_the_writer_role_and_a_hub_admin_never_dispatches(client, hub, hub_db):
    for who, status in (("reader", 403), ("admin", 403), ("stranger", 404)):
        response = dispatch(client, hub[who], [2])
        assert response.status_code == status, (who, response.text)
    assert "writer role" in dispatch(client, hub["reader"], [2]).json()["message"]
    assert count_runs(hub_db) == [(0,)]


def test_a_step_that_is_not_ready_or_has_an_active_run_is_409_and_nothing_is_queued(client, hub, hub_db):
    waiting = dispatch(client, hub["owner"], [2, 3])  # step 3 waits for step 2: neither is queued
    assert waiting.status_code == 409 and "step 3" in waiting.json()["message"]
    assert "waits for step 2" in waiting.json()["message"]
    done = dispatch(client, hub["owner"], [1])
    assert done.status_code == 409 and "its status is done" in done.json()["message"]
    unknown = dispatch(client, hub["owner"], [99])
    assert unknown.status_code == 422 and "no step '99'" in unknown.json()["message"]
    assert count_runs(hub_db) == [(0,)]

    (run,) = dispatched(client, hub["owner"], ["2", 2])  # one run per step, however it is named
    assert {key: run[key] for key in ("step_key", "state", "requested_runtime", "runtime", "mode", "approval")} == {
        "step_key": "2",
        "state": "queued",
        "requested_runtime": "any",
        "runtime": "any",
        "mode": "headless",
        "approval": "review",
    }
    assert (run["repo"], run["branch"], run["timeout_min"], run["attempt"], run["max_attempts"]) == (
        "evo-agents",
        "feat/queue",
        60,
        1,
        3,
    )
    assert run["plan_revision"] == plan(client, hub["owner"])["revision"] and run["dispatched_by"] == OWNER
    again = dispatch(client, hub["other"], [2])
    assert again.status_code == 409 and f"run #{run['id']}" in again.json()["message"]
    assert audit_rows(hub_db, "run") == [("run.dispatch", f"{PROJECT}/{PLAN}#2 run:{run['id']}", OWNER, PROJECT)]


def test_another_writer_cannot_dispatch_to_my_worker(client, hub, hub_db):
    mine = add_worker(client, hub["owner"], "mac-mini")
    refused = dispatch(client, hub["other"], [4], worker_id=mine["id"])
    assert refused.status_code == 403 and "worker of the member who dispatches" in refused.json()["message"]
    # a worker id nobody holds reads the same, so the answer tells nothing about other members' workers
    nobodys = dispatch(client, hub["other"], [4], worker_id=999999)
    assert nobodys.status_code == 403
    assert nobodys.json()["message"].replace("999999", str(mine["id"])) == refused.json()["message"]
    assert count_runs(hub_db) == [(0,)]
    pinned = dispatched(client, hub["owner"], [4], worker_id=mine["id"], runtime="claude-code", timeout_min=5)[0]
    assert (pinned["pinned_worker_id"], pinned["requested_runtime"], pinned["timeout_min"]) == (
        mine["id"],
        "claude-code",
        5,
    )
    assert client.post(f"/v1/workers/{mine['id']}/revoke", headers=hub["owner"]).status_code == 200
    revoked = dispatch(client, hub["owner"], [5], worker_id=mine["id"])
    assert revoked.status_code == 409 and "revoked" in revoked.json()["message"]
    assert dispatch(client, hub["owner"], [5], timeout_min=4).status_code == 422
    assert dispatch(client, hub["owner"], [5], runtime="gemini").status_code == 422


# Claim


def test_two_workers_claiming_at_once_get_the_run_once(client, hub, hub_db):
    first, second = add_worker(client, hub["owner"], "mac-mini"), add_worker(client, hub["owner"], "linux-box")
    run = dispatched(client, hub["owner"], [2])[0]
    barrier = threading.Barrier(2)
    answers = {}

    def take(worker):
        barrier.wait()
        answers[worker["name"]] = claim(client, worker, wait=1)

    threads = [threading.Thread(target=take, args=(worker,)) for worker in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    got = {name: spec for name, spec in answers.items() if spec is not None}
    assert len(answers) == 2 and len(got) == 1, answers
    ((name, spec),) = got.items()
    assert spec["id"] == run["id"] and spec["runtime"] == "claude-code"
    winner = first if name == first["name"] else second
    assert sql(hub_db, select(tables.runs.c.worker_id, tables.runs.c.state)) == [(winner["id"], "leased")]
    assert moves(hub_db, run["id"]) == [(1, "queued", "leased", "worker")]


def test_a_claim_skips_a_run_another_claim_holds_locked_instead_of_waiting(client, hub, hub_db):
    worker = add_worker(client, hub["owner"], "mac-mini")
    run = dispatched(client, hub["owner"], [2])[0]
    with live.engine(hub_db).begin() as conn:
        conn.execute(select(tables.runs.c.id).where(tables.runs.c.id == run["id"]).with_for_update())
        started = time.monotonic()
        assert claim(client, worker) is None  # SKIP LOCKED: the locked run is passed over at once
        assert time.monotonic() - started < 5
    assert claim(client, worker)["id"] == run["id"]


def test_the_claimed_run_comes_with_its_spec_and_prompt(client, hub):
    worker = add_worker(client, hub["owner"], "mac-mini")
    run = dispatched(client, hub["owner"], [2], approval="auto", mode="interactive", timeout_min=30)[0]
    spec = claim(client, worker)
    assert {key: spec[key] for key in ("id", "project", "plan_id", "step_key", "title", "attempt", "max_attempts")} == {
        "id": run["id"],
        "project": PROJECT,
        "plan_id": PLAN,
        "step_key": "2",
        "title": "Queue",
        "attempt": 1,
        "max_attempts": 3,
    }
    assert (spec["runtime"], spec["mode"], spec["approval"], spec["timeout_min"]) == (
        "claude-code",
        "interactive",
        "auto",
        30,
    )
    assert (spec["repo"], spec["branch"], spec["parent_run_id"]) == ("evo-agents", "feat/queue", None)
    lease = datetime.fromisoformat(spec["lease_expires_at"]) - datetime.now(UTC)
    assert 280 < lease.total_seconds() <= runs.LEASE_SECONDS
    prompt = spec["prompt"]
    assert prompt.startswith(f"You are running step 2 of the plan {PLAN} from the evo-agents hub.")
    assert "# Step 2: Queue" in prompt and "the queue" in prompt and "Branch: feat/queue." in prompt
    assert "evo-agents@abc1234: 40 tests pass" in prompt  # the evidence of step 1, which it depends on


def test_a_dispatch_of_steps_takes_a_model_the_claim_carries_and_a_rerun_keeps(client, hub, hub_db):
    worker = add_worker(client, hub["owner"], "mac-mini")
    for model in ("", "opus\nfast", "m" * (runs.MAX_MODEL_CHARS + 1), 4):
        refused = dispatch(client, hub["owner"], [2], runtime="claude-code", model=model)
        assert refused.status_code == 422, model
    assert count_runs(hub_db)[0][0] == 0
    run = dispatched(client, hub["owner"], [2, 4], runtime="claude-code", model="claude-opus-4-1")
    assert [(item["step_key"], item["model"]) for item in run] == [("2", "claude-opus-4-1"), ("4", "claude-opus-4-1")]
    spec = claim(client, worker)
    assert (spec["id"], spec["kind"], spec["runtime"], spec["model"]) == (
        run[0]["id"],
        "step",
        "claude-code",
        "claude-opus-4-1",
    )
    shown = client.get(f"/v1/projects/{PROJECT}/runs/{run[1]['id']}", headers=hub["reader"]).json()
    assert shown["model"] == "claude-opus-4-1"

    # a rerun keeps the model of the run it reruns, as it keeps its runtime
    assert control(client, hub["owner"], run[1]["id"], "cancel").status_code == 200
    again = control(client, hub["owner"], run[1]["id"], "rerun")
    assert again.status_code == 201, again.text
    assert (again.json()["model"], again.json()["requested_runtime"]) == ("claude-opus-4-1", "claude-code")

    # without one the runtime chooses: the run and its claim say null
    moved(client, worker, spec["id"], "cancelled")
    assert control(client, hub["owner"], again.json()["id"], "cancel").status_code == 200
    plain = dispatched(client, hub["owner"], [4])[0]
    assert plain["model"] is None and claim(client, worker)["model"] is None


def test_the_heartbeat_keeps_the_models_each_runtime_lists(client, hub):
    listed = {"available": True, "version": "1.18.34", "models": ["zai-coding-plan/glm-5.3-flash", "openai/gpt-5.5"]}
    worker = add_worker(client, hub["owner"], "mac-mini", runtimes={**RUNTIMES, "opencode": listed})
    shown = client.get(f"/v1/workers/{worker['id']}", headers=hub["owner"]).json()
    assert shown["runtimes"] == {
        "claude-code": {"available": True, "version": "2.1.289", "reason": None, "models": None},
        "opencode": {**listed, "reason": None},
    }
    too_many = [f"provider/model-{number}" for number in range(runs.MAX_RUNTIME_MODELS + 1)]
    for models in (too_many, [""], ["opus\nfast"], ["m" * (runs.MAX_MODEL_CHARS + 1)], "opus"):
        body = {"runtimes": {"opencode": {**listed, "models": models}}, "checkouts": CHECKOUTS, "free_slots": 1}
        refused = client.post("/v1/worker/heartbeat", json=body, headers=worker["headers"])
        assert refused.status_code == 422, models
    most = [f"provider/model-{number}" for number in range(runs.MAX_RUNTIME_MODELS)]
    beat(client, {**worker, "runtimes": {"opencode": {**listed, "models": most}}})
    shown = client.get(f"/v1/workers/{worker['id']}", headers=hub["owner"]).json()
    assert shown["runtimes"]["opencode"]["models"] == most and list(shown["runtimes"]) == ["opencode"]


def test_a_worker_of_another_member_never_gets_my_run(client, hub):
    theirs = add_worker(client, hub["other"], "their-box")
    mine = add_worker(client, hub["owner"], "mac-mini")
    run = dispatched(client, hub["owner"], [2])[0]
    assert claim(client, theirs) is None  # same project, runtime and checkout, but not its owner's run
    assert claim(client, mine)["id"] == run["id"]
    their_run = dispatched(client, hub["other"], [4])[0]
    assert claim(client, mine) is None  # one slot, held; and step 4 is the other member's anyway
    assert claim(client, theirs)["id"] == their_run["id"]


def test_a_claim_skips_runs_once_the_owner_lost_the_writer_role(client, hub, hub_db):
    worker = add_worker(client, hub["owner"], "mac-mini")
    run = dispatched(client, hub["owner"], [2])[0]
    grants = f"/v1/admin/projects/{PROJECT}/grants/{OWNER}"
    assert client.put(grants, json=READ_ONLY, headers=hub["admin"]).status_code == 200
    assert claim(client, worker) is None
    assert state_of(hub_db, run["id"]) == "queued"
    assert client.put(grants, json=WRITER, headers=hub["admin"]).status_code == 200
    assert claim(client, worker)["id"] == run["id"]


def test_a_claim_needs_the_runtime_a_checkout_a_free_slot_and_no_drain(client, hub, hub_db):
    unavailable = {"available": False, "version": "2.1.289", "reason": "not signed in"}
    runtimes = {"codex": {"available": True, "version": "0.153.4"}, "claude-code": unavailable}
    codex = add_worker(client, hub["owner"], "codex-box", runtimes=runtimes)
    bare = add_worker(client, hub["owner"], "bare-box", checkouts={})
    wanted = dispatched(client, hub["owner"], [2], runtime="claude-code")[0]
    assert claim(client, codex) is None and claim(client, bare) is None  # claude-code is there, but unavailable
    assert control(client, hub["owner"], wanted["id"], "cancel").status_code == 200
    anything = dispatched(client, hub["owner"], [2])[0]
    spec = claim(client, codex)
    assert (spec["id"], spec["runtime"]) == (anything["id"], "codex")  # any: the runtime the worker has
    assert of_run(hub_db, anything["id"], "requested_runtime", "runtime") == [("any", "codex")]
    later = dispatched(client, hub["owner"], [4])[0]
    assert claim(client, codex) is None  # its one slot is taken
    two = add_worker(client, hub["owner"], "two-slots", slots=2)
    assert client.post(f"/v1/workers/{two['id']}/drain", headers=hub["admin"]).status_code == 200
    assert claim(client, two) is None
    assert beat(client, two)["drain"] is True
    assert client.post(f"/v1/workers/{two['id']}/undrain", headers=hub["owner"]).status_code == 200
    assert claim(client, two)["id"] == later["id"]
    # a run pinned to one worker is claimed by that one only
    pinned = dispatched(client, hub["owner"], [5], worker_id=codex["id"])[0]
    assert claim(client, two) is None
    moved(client, codex, anything["id"], "running", "failed", error="the agent stopped")
    assert claim(client, codex)["id"] == pinned["id"]


def test_a_waiting_claim_wakes_when_a_run_is_queued(client, hub, monkeypatch):
    monkeypatch.setattr(run_routes, "CLAIM_POLL_SECONDS", 60.0)  # only the notification can wake it in time
    worker = add_worker(client, hub["owner"], "mac-mini")
    answer = {}

    def wait():
        answer["run"] = claim(client, worker, wait=20)

    thread = threading.Thread(target=wait)
    thread.start()
    time.sleep(1.5)
    queued = time.monotonic()
    run = dispatched(client, hub["owner"], [2])[0]
    thread.join(25)
    assert answer["run"]["id"] == run["id"]
    assert time.monotonic() - queued < 5


def test_a_newer_claim_of_the_same_worker_ends_the_older_one(client, hub):
    worker = add_worker(client, hub["owner"], "mac-mini")
    answer = {}

    def wait():
        started = time.monotonic()
        answer["run"] = claim(client, worker, wait=20)
        answer["elapsed"] = time.monotonic() - started

    thread = threading.Thread(target=wait)
    thread.start()
    time.sleep(1.0)
    assert claim(client, worker) is None
    thread.join(25)
    assert answer["run"] is None and answer["elapsed"] < 10


# Heartbeat and the owner's cancel


def test_a_heartbeat_records_the_machine_extends_the_lease_and_carries_the_cancel(client, hub, hub_db):
    worker = add_worker(client, hub["owner"], "mac-mini")
    run_id = dispatched(client, hub["owner"], [2])[0]["id"]
    spec = claim(client, worker)
    moved(client, worker, run_id, "running", session_id="0199a3c1-0000-7000-8000-00000000000a")
    set_run(hub_db, run_id, lease_expires_at=func.now() + timedelta(seconds=10))
    owner = select(tables.users.c.id).where(tables.users.c.login == OWNER).scalar_subquery()
    sql(hub_db, insert(tables.run_inbox).values(run_id=run_id, sent_by=owner, body="also run ruff"))
    answer = beat(client, worker, runs_held=[run_id, 999999], free_slots=0)
    assert answer["drain"] is False
    held, unknown = answer["runs"]
    assert {key: held[key] for key in ("id", "held", "state", "cancel", "takeover", "handback", "inbox")} == {
        "id": run_id,
        "held": True,
        "state": "running",
        "cancel": False,
        "takeover": False,
        "handback": False,
        "inbox": 1,
    }
    assert datetime.fromisoformat(held["lease_expires_at"]) >= datetime.fromisoformat(spec["lease_expires_at"])
    assert unknown == {
        "id": 999999,
        "held": False,
        "state": None,
        "lease_expires_at": None,
        "cancel": True,
        "takeover": False,
        "handback": False,
        "terminal_open": False,
        "park": False,
        "finish": False,
        "inbox": 0,
        "decisions": 0,
    }
    # the worker shows what the heartbeat reported, each runtime and checkout with every key
    shown = client.get(f"/v1/workers/{worker['id']}", headers=hub["owner"]).json()
    assert (shown["status"], shown["free_slots"]) == ("online", 0)
    assert shown["runtimes"] == {
        "claude-code": {"available": True, "version": "2.1.289", "reason": None, "models": None}
    }
    assert shown["checkouts"] == {f"{PROJECT}/evo-agents": {"path": "/src/evo-agents", "branch": "main"}}
    for runtimes, checkouts in (
        ({"claude-code": {"version": "2.1.289"}}, CHECKOUTS),  # available is required
        ({"gemini": {"available": True}}, CHECKOUTS),
        (RUNTIMES, {"evo-agents": {"path": "/src"}}),  # a checkout is keyed <project>/<repo>
        (RUNTIMES, {f"{PROJECT}/evo-agents": {"branch": "main"}}),
    ):
        body = {"runtimes": runtimes, "checkouts": checkouts, "free_slots": 1}
        refused = client.post("/v1/worker/heartbeat", json=body, headers=worker["headers"])
        assert refused.status_code == 422, (runtimes, checkouts)

    # cancel belongs to the owner; a held run is asked to stop, once
    for who, status in (("other", 403), ("reader", 403), ("admin", 403), ("stranger", 404)):
        assert control(client, hub[who], run_id, "cancel").status_code == status, who
    for _ in range(2):
        asked = control(client, hub["owner"], run_id, "cancel")
        assert asked.status_code == 200 and asked.json()["state"] == "running"
        assert asked.json()["cancel_requested_at"] is not None
    assert [row[0] for row in audit_rows(hub_db, "run")] == ["run.dispatch", "run.cancel"]
    assert beat(client, worker, runs_held=[run_id])["runs"][0]["cancel"] is True
    stopped = moved(client, worker, run_id, "cancelled")
    assert stopped["state"] == "cancelled" and stopped["finished_at"] is not None
    assert step(client, hub["owner"], 2)["status"] == "pending"
    assert "its owner asked to cancel it" in step(client, hub["owner"], 2)["note"]
    final = control(client, hub["owner"], run_id, "cancel")
    assert final.status_code == 409 and "final" in final.json()["message"]
    assert beat(client, worker, runs_held=[run_id])["runs"][0]["held"] is False


def test_cancelling_a_queued_run_ends_it_at_once(client, hub, hub_db):
    run_id = dispatched(client, hub["owner"], [2])[0]["id"]
    cancelled = control(client, hub["owner"], run_id, "cancel")
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
    assert moves(hub_db, run_id) == [(1, "queued", "cancelled", "owner")]
    assert step(client, hub["owner"], 2)["note"] == f"run #{run_id} was cancelled: {OWNER} cancelled it"
    assert control(client, hub["owner"], 999999, "cancel").status_code == 404


# The worker's reports


def test_a_worker_reports_only_moves_of_runs_it_holds_and_the_table_allows(client, hub, hub_db):
    first, second = add_worker(client, hub["owner"], "mac-mini"), add_worker(client, hub["owner"], "linux-box")
    run_id = dispatched(client, hub["owner"], [2])[0]["id"]
    assert claim(client, first)["id"] == run_id
    assert report(client, second, run_id, "running").status_code == 404
    assert report(client, first, 999999, "running").status_code == 404
    skipped = report(client, first, run_id, "verifying")
    assert skipped.status_code == 409 and "cannot go from leased to verifying" in skipped.json()["message"]
    stale = report(client, first, run_id, "running", **{"from": "queued"})
    assert stale.status_code == 409 and "is leased, not queued" in stale.json()["message"]
    assert report(client, first, run_id, "running", **{"from": "leased"}).status_code == 200
    # a resend of the state the run is in moves nothing and keeps the news it carries
    again = report(client, first, run_id, "running", usage={"input_tokens": 1200}, session_id="sess-1")
    assert again.status_code == 200 and again.json()["usage"] == {"input_tokens": 1200}
    assert again.json()["session_id"] == "sess-1" and again.json()["started_at"] is not None
    assert moves(hub_db, run_id) == [(1, "queued", "leased", "worker"), (2, "leased", "running", "worker")]
    assert report(client, first, run_id, "lost").status_code == 409  # the reaper's move, never the worker's
    assert report(client, first, run_id, "done", verify=PASSED).status_code == 409  # not from running
    failed = moved(client, first, run_id, "failed")
    assert failed["error"] == "worker mac-mini reported that the run failed"
    assert (
        step(client, hub["owner"], 2)["note"] == f"run #{run_id} failed: worker mac-mini reported that the run failed"
    )
    assert report(client, first, run_id, "running").status_code == 404  # it holds the run no more


# Approval


def test_approval_auto_marks_the_step_done_in_a_revision_with_the_evidence(client, hub, hub_db):
    worker = add_worker(client, hub["owner"], "mac-mini")
    run_id = dispatched(client, hub["owner"], [2], approval="auto")[0]["id"]
    claim(client, worker)
    before = plan(client, hub["owner"])["revision"]
    moved(client, worker, run_id, "running")
    started = step(client, hub["owner"], 2)
    assert (started["status"], started["note"]) == ("in_progress", f"run #{run_id} on worker mac-mini")
    moved(client, worker, run_id, "verifying")
    failing = report(client, worker, run_id, "done", verify=[{"command": "ruff check .", "exit_code": 1}])
    assert failing.status_code == 409 and "every verify command exited 0" in failing.json()["message"]
    assert report(client, worker, run_id, "done").status_code == 409  # no verify results at all
    assert report(client, worker, run_id, "review", verify=PASSED).status_code == 409
    done = moved(
        client,
        worker,
        run_id,
        "done",
        verify=PASSED,
        commit_sha=SHA,
        diffstat={"files": 3, "insertions": 120, "deletions": 4},
        summary="The queue claims with SKIP LOCKED.",
    )
    assert done["state"] == "done" and done["verify"] == PASSED and done["commit_sha"] == SHA
    expected = (
        f"run #{run_id} on worker mac-mini (claude-code, attempt 1): evo-agents@{SHA[:12]} on feat/queue, "
        "3 files +120 -4.\nverify: `python -m pytest -q` exit 0; `ruff check .` exit 0.\n"
        "The queue claims with SKIP LOCKED."
    )
    assert done["evidence"] == expected
    finished = step(client, hub["owner"], 2)
    assert (finished["status"], finished["done_at"], finished["evidence"]) == ("done", today(), expected)
    held = plan(client, hub["owner"])
    assert held["revision"] == before + 2 and held["updated_by"] == OWNER
    revisions = client.get(f"/v1/projects/{PROJECT}/plans/{PLAN}/revisions", headers=hub["reader"]).json()
    assert [(r["revision"], r["actor"], r["summary"]) for r in revisions[:2]] == [
        (before + 2, OWNER, "step 2: status in_progress -> done; set done_at, evidence"),
        (before + 1, OWNER, "step 2: status pending -> in_progress; set note"),
    ]
    # the plan writes are the dispatcher's, made with the worker's token
    assert sql(hub_db, select(tables.audit.c.token_id).distinct().where(tables.audit.c.action == "plan.patch")) == [
        (worker["token_id"],)
    ]
    assert ready(client, hub["reader"])["3"]["ready"] is True


def test_approval_review_waits_for_the_owners_approve(client, hub, hub_db):
    worker = add_worker(client, hub["owner"], "mac-mini")
    run_id = dispatched(client, hub["owner"], [2])[0]["id"]
    claim(client, worker)
    moved(client, worker, run_id, "running", "verifying")
    early = report(client, worker, run_id, "done", verify=PASSED)
    assert early.status_code == 409 and "approval" in early.json()["message"]
    mixed = [*PASSED[:1], {"command": "ruff check .", "exit_code": 1}]
    review = moved(client, worker, run_id, "review", verify=mixed, commit_sha=SHA)
    assert review["state"] == "review" and review["lease_expires_at"] is None
    assert step(client, hub["owner"], 2)["status"] == "in_progress"
    assert report(client, worker, run_id, "done", verify=PASSED).status_code == 404  # the owner's move now
    for who, status in (("other", 403), ("reader", 403), ("admin", 403)):
        assert control(client, hub[who], run_id, "approve").status_code == status, who
    approved = control(client, hub["owner"], run_id, "approve")
    assert approved.status_code == 200 and approved.json()["state"] == "done"
    finished = step(client, hub["owner"], 2)
    assert finished["status"] == "done" and finished["done_at"] == today()
    assert finished["evidence"] == review["evidence"] and "`ruff check .` exit 1" in finished["evidence"]
    assert moves(hub_db, run_id)[-1] == (5, "review", "done", "owner")
    assert ("run.approve", f"{PROJECT}/{PLAN}#2 run:{run_id}", OWNER, PROJECT) in audit_rows(hub_db, "run")
    again = control(client, hub["owner"], run_id, "approve")
    assert again.status_code == 409 and "only a run in review" in again.json()["message"]


# The plan's revisions


def bump(db, note: str) -> None:
    """Another writer sets a note on step 4, taking the plan's next revision."""
    plans, revisions = tables.plans, tables.plan_revisions
    with live.connect(db) as conn:
        body, revision = conn.execute(select(plans.c.body, plans.c.revision).where(plans.c.plan_id == PLAN)).one()
        body["steps"][3]["note"] = note
        conn.execute(
            update(plans)
            .values(body=body, revision=revision + 1, digest=plan_digest(body))
            .where(plans.c.plan_id == PLAN)
        )
        copied = ("project_id", "plan_id", "revision", "area", "label", "body", "digest")
        written = select(*(plans.c[name] for name in copied), literal("step 4: set note"), plans.c.updated_by)
        conn.execute(
            insert(revisions).from_select([*copied, "summary", "actor_id"], written.where(plans.c.plan_id == PLAN))
        )


def test_a_revision_conflict_on_the_plan_is_tried_again(client, hub, hub_db, monkeypatch):
    worker = add_worker(client, hub["owner"], "mac-mini")
    run_id = dispatched(client, hub["owner"], [2])[0]["id"]
    claim(client, worker)
    before = plan(client, hub["owner"])["revision"]
    reads = []
    read_plan = run_state.read_plan

    async def racing(conn, access, plan_id):
        held = await read_plan(conn, access, plan_id)
        reads.append(held.revision)
        if len(reads) == 1:  # another write lands between the hub's read and its write
            bump(hub_db, "edited meanwhile")
        return held

    monkeypatch.setattr(run_state, "read_plan", racing)
    moved(client, worker, run_id, "running")
    assert reads == [before, before + 1]
    held = plan(client, hub["owner"])
    assert held["revision"] == before + 2
    steps = {item["id"]: item for item in held["body"]["steps"]}
    assert steps[2]["status"] == "in_progress" and steps[4]["note"] == "edited meanwhile"


def test_the_plan_write_gives_up_after_five_conflicts_and_the_move_stays(client, hub, hub_db, monkeypatch, caplog):
    worker = add_worker(client, hub["owner"], "mac-mini")
    run_id = dispatched(client, hub["owner"], [2])[0]["id"]
    claim(client, worker)
    reads = []
    read_plan = run_state.read_plan

    async def always_late(conn, access, plan_id):
        held = await read_plan(conn, access, plan_id)
        reads.append(held.revision)
        bump(hub_db, f"edit {len(reads)}")
        return held

    monkeypatch.setattr(run_state, "read_plan", always_late)
    assert moved(client, worker, run_id, "running")["state"] == "running"
    assert len(reads) == run_state.PLAN_TRIES == 5
    assert step(client, hub["owner"], 2)["status"] == "pending" and step(client, hub["owner"], 4)["note"] == "edit 5"
    assert "run step not written to the plan" in caplog.text


# The reaper


def test_an_expired_lease_loses_the_run_and_the_third_attempt_fails_back_to_pending(client, hub, hub_db):
    worker = add_worker(client, hub["owner"], "mac-mini")
    first = dispatched(client, hub["owner"], [2], approval="auto")[0]["id"]
    assert recover(client) == {"lost": 0, "failed": 0, "cancelled": 0, "parked": 0}
    assert claim(client, worker)["attempt"] == 1
    moved(client, worker, first, "running")
    expire(hub_db, first)
    assert recover(client) == {"lost": 1, "failed": 0, "cancelled": 0, "parked": 0}
    assert of_run(hub_db, first, "state", "error") == [("lost", "its worker mac-mini stopped extending the lease")]
    assert moves(hub_db, first)[-1] == (3, "running", "lost", "reaper")
    run = tables.runs
    retries = select(run.c.id, run.c.attempt, run.c.state, run.c.requested_runtime, run.c.runtime)
    ((second, attempt, state, requested, runtime),) = sql(hub_db, retries.where(run.c.parent_run_id == first))
    assert (attempt, state, requested, runtime) == (2, "queued", "any", "any")
    assert step(client, hub["owner"], 2)["status"] == "in_progress"  # the dispatch goes on
    assert beat(client, worker, runs_held=[first])["runs"][0] == {
        "id": first,
        "held": False,
        "state": None,
        "lease_expires_at": None,
        "cancel": True,
        "takeover": False,
        "handback": False,
        "terminal_open": False,
        "park": False,
        "finish": False,
        "inbox": 0,
        "decisions": 0,
    }

    assert claim(client, worker) is None, "the worker that lost the run is not steady yet"
    steady(hub_db, worker)
    spec = claim(client, worker)
    assert (spec["id"], spec["attempt"], spec["parent_run_id"]) == (second, 2, first)
    moved(client, worker, second, "running")
    assert step(client, hub["owner"], 2)["note"] == f"run #{second} on worker mac-mini"
    expire(hub_db, second)
    assert recover(client)["lost"] == 1
    ((third,),) = sql(hub_db, select(run.c.id).where(run.c.parent_run_id == second))
    assert claim(client, worker)["attempt"] == 3, "steady still: its heartbeats went on"
    expire(hub_db, third)
    assert recover(client) == {"lost": 0, "failed": 1, "cancelled": 0, "parked": 0}
    ((state, error),) = of_run(hub_db, third, "state", "error")
    assert state == "failed" and error == "its worker mac-mini stopped extending the lease, and it was attempt 3 of 3"
    assert count_runs(hub_db, run.c.parent_run_id == third) == [(0,)]
    assert moves(hub_db, third) == [(1, "queued", "leased", "worker"), (2, "leased", "failed", "reaper")]
    back = step(client, hub["owner"], 2)
    assert back["status"] == "pending" and back["note"] == f"run #{third} failed: {error}"
    assert ready(client, hub["reader"])["2"]["ready"] is True


def run_of(client, headers, run_id: int) -> dict:
    response = client.get(f"/v1/projects/{PROJECT}/runs/{run_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def lost_once(client, hub, hub_db, worker: dict, steps=(2,), **extra) -> tuple[int, int]:
    """Step 2 claimed by ``worker`` and lost: the lost run's id and its next attempt's."""
    first = dispatched(client, hub["owner"], list(steps), **extra)[0]["id"]
    assert claim(client, worker)["id"] == first
    moved(client, worker, first, "running")
    expire(hub_db, first)
    assert recover(client)["lost"] == 1
    ((second,),) = sql(hub_db, select(tables.runs.c.id).where(tables.runs.c.parent_run_id == first))
    return first, second


def test_the_next_attempt_of_a_lost_run_waits_until_the_worker_that_lost_it_is_stable(client, hub, hub_db):
    worker = add_worker(client, hub["owner"], "mac-mini")
    first, second = lost_once(client, hub, hub_db, worker)
    waits = {"worker_id": worker["id"], "worker": "mac-mini"}

    # Its heartbeats came for 100 seconds: not enough.
    worker_clock(hub_db, worker, steady_since=100, last_heartbeat_at=0)
    beat(client, worker, runs_held=[first])
    assert claim(client, worker) is None
    shown = run_of(client, hub["reader"], second)
    assert shown["state"] == "queued" and shown["attempt"] == 2 and shown["max_attempts"] == runs.MAX_ATTEMPTS == 3
    assert {key: shown["steady_wait"][key] for key in waits} == waits
    assert 19 < seconds_from_now(hub_db, shown["steady_wait"]["steady_at"]) <= 20, "steady 120 s after it began"
    listed = client.get(f"/v1/projects/{PROJECT}/runs", params={"state": "queued"}, headers=hub["reader"]).json()
    assert [item["steady_wait"]["worker"] for item in listed["runs"]] == ["mac-mini"], "the list says it too"

    # A heartbeat 31 seconds after the one before (it slept) starts the count again.
    worker_clock(hub_db, worker, steady_since=1000, last_heartbeat_at=31)
    beat(client, worker)
    assert claim(client, worker) is None
    assert 119 < seconds_from_now(hub_db, run_of(client, hub["owner"], second)["steady_wait"]["steady_at"]) <= 120

    # Silent for 31 seconds: not steady whenever its count began, and no time is known until it beats again.
    worker_clock(hub_db, worker, steady_since=1000, last_heartbeat_at=31)
    assert claim(client, worker) is None
    assert run_of(client, hub["owner"], second)["steady_wait"] == {**waits, "steady_at": None}

    # 120 seconds of heartbeats, none more than 30 seconds late: it takes the attempt, which waits for nobody now.
    worker_clock(hub_db, worker, steady_since=runs.STEADY_SECONDS, last_heartbeat_at=runs.STEADY_GAP_SECONDS - 1)
    spec = claim(client, worker)
    assert (spec["id"], spec["attempt"], spec["parent_run_id"]) == (second, 2, first)
    assert run_of(client, hub["owner"], second)["steady_wait"] is None
    assert run_of(client, hub["owner"], first)["steady_wait"] is None, "a run that ended waits for nobody"


def test_a_lost_run_not_pinned_goes_to_another_worker_at_once_and_its_own_waits_until_stable(client, hub, hub_db):
    mac = add_worker(client, hub["owner"], "mac-mini")
    first, second = lost_once(client, hub, hub_db, mac)
    lab = add_worker(client, hub["owner"], "lab-02")  # registered just now: not steady, but it lost nothing
    assert claim(client, mac) is None
    spec = claim(client, lab)
    assert (spec["id"], spec["attempt"], spec["parent_run_id"]) == (second, 2, first)
    assert run_of(client, hub["owner"], second)["worker"] == "lab-02"

    # Any other queued run still goes to the worker that lost one: only the next attempt waits.
    other = dispatched(client, hub["owner"], [4])[0]["id"]
    assert claim(client, mac)["id"] == other


def test_a_lost_run_pinned_to_its_worker_waits_until_it_is_stable_and_the_third_attempt_fails(client, hub, hub_db):
    mac = add_worker(client, hub["owner"], "mac-mini")
    lab = add_worker(client, hub["owner"], "lab-02")
    first, second = lost_once(client, hub, hub_db, mac, worker_id=mac["id"])
    shown = run_of(client, hub["owner"], second)
    assert (shown["pinned_worker_id"], shown["steady_wait"]["worker_id"]) == (mac["id"], mac["id"])
    assert claim(client, lab) is None, "pinned to mac-mini"
    assert claim(client, mac) is None, "mac-mini is not steady yet"
    steady(hub_db, mac)
    assert claim(client, mac)["id"] == second
    moved(client, mac, second, "running")
    expire(hub_db, second)
    assert recover(client)["lost"] == 1
    ((third,),) = sql(hub_db, select(tables.runs.c.id).where(tables.runs.c.parent_run_id == second))
    worker_clock(hub_db, mac, last_heartbeat_at=runs.STEADY_GAP_SECONDS + 1)  # it slept again
    beat(client, mac)
    assert claim(client, mac) is None
    steady(hub_db, mac)
    assert claim(client, mac)["attempt"] == 3
    expire(hub_db, third)
    assert recover(client) == {"lost": 0, "failed": 1, "cancelled": 0, "parked": 0}
    assert of_run(hub_db, third, "error") == [
        (f"its worker mac-mini stopped extending the lease, and it was attempt 3 of {runs.MAX_ATTEMPTS}",)
    ]
    assert count_runs(hub_db, tables.runs.c.parent_run_id == third) == [(0,)]


def test_a_heartbeat_keeps_when_the_worker_became_stable_and_one_that_comes_late_starts_it_again(client, hub, hub_db):
    w = tables.workers
    steady_since = select(w.c.steady_since)
    worker = add_worker(client, hub["owner"], "mac-mini")
    ((first,),) = sql(hub_db, steady_since.where(w.c.id == worker["id"]))
    assert first is not None, "the first heartbeat begins the count"
    beat(client, worker)
    assert sql(hub_db, steady_since.where(w.c.id == worker["id"])) == [(first,)], "the next one keeps it"
    worker_clock(hub_db, worker, last_heartbeat_at=runs.STEADY_GAP_SECONDS - 1)
    beat(client, worker)
    assert sql(hub_db, steady_since.where(w.c.id == worker["id"])) == [(first,)], "29 seconds late is not late"
    worker_clock(hub_db, worker, last_heartbeat_at=runs.STEADY_GAP_SECONDS + 1)
    beat(client, worker)
    ((again,),) = sql(hub_db, steady_since.where(w.c.id == worker["id"]))
    assert again > first, "31 seconds late begins the count again"
    sql(hub_db, update(w).values(steady_since=None).where(w.c.id == worker["id"]))  # a worker from before 0023
    beat(client, worker)
    assert sql(hub_db, steady_since.where(w.c.id == worker["id"]))[0][0] is not None


def test_a_claim_and_each_heartbeat_lease_the_run_for_evo_hub_run_lease_seconds(hub_db, tmp_path, github):
    config = live.hub_config(hub_db, tmp_path, github, run_lease_seconds=30)
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        headers = members(client, github)
        worker = add_worker(client, headers["owner"], "mac-mini")
        run_id = dispatched(client, headers["owner"], [2])[0]["id"]
        assert claim(client, worker)["id"] == run_id
        run = tables.runs
        leased = select(extract("epoch", run.c.lease_expires_at - run.c.leased_at)).where(run.c.id == run_id)
        assert float(sql(hub_db, leased)[0][0]) == 30.0
        set_run(hub_db, run_id, lease_expires_at=func.now() + timedelta(seconds=1))
        beat(client, worker, runs_held=[run_id])
        left = sql(hub_db, select(extract("epoch", run.c.lease_expires_at - func.now())).where(run.c.id == run_id))
        assert 20 < float(left[0][0]) <= 30, "the heartbeat extends it by the same time"

    env = {"EVO_HUB_DSN": "postgresql://hub@db/hub"}
    assert load_config(env).run_lease_seconds == runs.LEASE_SECONDS == 300
    assert load_config({**env, "EVO_HUB_RUN_LEASE_SECONDS": "8"}).run_lease_seconds == 8
    for value in ("4", "3601", "soon"):
        with pytest.raises(ConfigError) as caught:
            load_config({**env, "EVO_HUB_RUN_LEASE_SECONDS": value})
        assert caught.value.variable == "EVO_HUB_RUN_LEASE_SECONDS"


def test_a_heartbeat_keeps_the_reaper_away_and_an_asked_cancel_ends_cancelled(client, hub, hub_db):
    worker = add_worker(client, hub["owner"], "mac-mini")
    run_id = dispatched(client, hub["owner"], [2])[0]["id"]
    claim(client, worker)
    moved(client, worker, run_id, "running")
    expire(hub_db, run_id)
    beat(client, worker, runs_held=[run_id])  # the lease is extended before the reaper looks
    assert recover(client)["lost"] == 0 and state_of(hub_db, run_id) == "running"
    assert control(client, hub["owner"], run_id, "cancel").status_code == 200
    expire(hub_db, run_id)
    assert recover(client) == {"lost": 0, "failed": 0, "cancelled": 1, "parked": 0}
    assert state_of(hub_db, run_id) == "cancelled"
    assert count_runs(hub_db, tables.runs.c.parent_run_id == run_id) == [(0,)]
    assert step(client, hub["owner"], 2)["status"] == "pending"


def test_a_run_past_its_timeout_fails_and_is_not_tried_again(client, hub, hub_db):
    worker = add_worker(client, hub["owner"], "mac-mini")
    run_id = dispatched(client, hub["owner"], [2], timeout_min=5)[0]["id"]
    claim(client, worker)
    moved(client, worker, run_id, "running")
    assert recover(client) == {"lost": 0, "failed": 0, "cancelled": 0, "parked": 0}
    # the agent has run 301 seconds since its time was last counted
    set_run(hub_db, run_id, counted_at=func.now() - timedelta(seconds=301))
    beat(client, worker, runs_held=[run_id])  # a lease the worker keeps extending does not keep the timeout away
    assert recover(client) == {"lost": 0, "failed": 1, "cancelled": 0, "parked": 0}
    reason = "it ran past its timeout of 5 minutes"
    assert of_run(hub_db, run_id, "state", "error") == [("failed", reason)]
    assert moves(hub_db, run_id)[-1] == (3, "running", "failed", "reaper")
    assert count_runs(hub_db, tables.runs.c.parent_run_id == run_id) == [(0,)]
    back = step(client, hub["owner"], 2)
    assert (back["status"], back["note"]) == ("pending", f"run #{run_id} failed: {reason}")
    assert beat(client, worker, runs_held=[run_id])["runs"][0]["cancel"] is True  # the worker stops the agent
    # a run not started yet counts from its claim, and one whose cancel was asked for ends cancelled
    second = dispatched(client, hub["owner"], [4], timeout_min=5)[0]["id"]
    claim(client, worker)
    assert control(client, hub["owner"], second, "cancel").status_code == 200
    assert recover(client) == {"lost": 0, "failed": 0, "cancelled": 0, "parked": 0}
    set_run(hub_db, second, counted_at=func.now() - timedelta(seconds=301))
    assert recover(client) == {"lost": 0, "failed": 0, "cancelled": 1, "parked": 0}
    assert state_of(hub_db, second) == "cancelled"
    assert moves(hub_db, second)[-1] == (2, "leased", "cancelled", "reaper")


def test_revoking_a_worker_fails_its_pinned_runs_and_their_steps_go_back_to_pending(client, hub, hub_db):
    worker = add_worker(client, hub["owner"], "mac-mini")
    held = dispatched(client, hub["owner"], [2], worker_id=worker["id"])[0]["id"]
    claim(client, worker)
    moved(client, worker, held, "running")
    queued = dispatched(client, hub["owner"], [4], worker_id=worker["id"])[0]["id"]
    assert client.post(f"/v1/workers/{worker['id']}/revoke", headers=hub["admin"]).status_code == 200
    assert (state_of(hub_db, held), state_of(hub_db, queued)) == ("failed", "failed")
    reason = "its worker mac-mini was revoked, and the run was pinned to it"
    for key, run_id in ((2, held), (4, queued)):
        back = step(client, hub["owner"], key)
        assert (back["status"], back["note"]) == ("pending", f"run #{run_id} failed: {reason}")


# Rerun


def test_rerun_queues_the_step_again_after_a_run_that_ended(client, hub, hub_db):
    worker = add_worker(client, hub["owner"], "mac-mini")
    old = dispatched(client, hub["owner"], [2], runtime="claude-code", approval="auto", worker_id=worker["id"])[0]
    still = control(client, hub["owner"], old["id"], "rerun")
    assert still.status_code == 409 and "still queued" in still.json()["message"]
    assert control(client, hub["owner"], old["id"], "cancel").status_code == 200
    for who, status in (("other", 403), ("reader", 403), ("stranger", 404)):
        assert control(client, hub[who], old["id"], "rerun").status_code == status, who
    again = control(client, hub["owner"], old["id"], "rerun")
    assert again.status_code == 201, again.text
    new = again.json()
    assert (new["state"], new["attempt"], new["parent_run_id"], new["pinned_worker_id"]) == (
        "queued",
        1,
        old["id"],
        worker["id"],
    )
    assert (new["requested_runtime"], new["approval"], new["plan_revision"]) == (
        "claude-code",
        "auto",
        plan(client, hub["owner"])["revision"],
    )
    twice = control(client, hub["owner"], old["id"], "rerun")
    assert twice.status_code == 409 and f"run #{new['id']}" in twice.json()["message"]
    assert audit_rows(hub_db, "run")[-1] == (
        "run.rerun",
        f"{PROJECT}/{PLAN}#2 run:{new['id']} rerun of run:{old['id']}",
        OWNER,
        PROJECT,
    )
    assert claim(client, worker)["id"] == new["id"]


# Listing runs


def test_the_run_list_shows_each_member_the_runs_of_the_plans_its_label_lets_it_read(client, hub):
    sinks = [{"id": "hub", "kind": "hub", "clearance": {"level": "customer"}}]
    assert client.put(f"/v1/projects/{PROJECT}", json=registration(sinks), headers=hub["admin"]).status_code == 200
    grant = {"role": "writer", "max_level": "customer"}
    path = f"/v1/admin/projects/{PROJECT}/grants/{OWNER}"
    assert client.put(path, json=grant, headers=hub["admin"]).status_code == 200
    vault = {**plan_body(), "id": "vault", "repos": [{"repo": "evo-agents", "branch": "feat/vault"}]}
    body = {"body": vault, "label": {"level": "customer"}}
    pushed = client.put(f"/v1/projects/{PROJECT}/plans/vault", json=body, headers=hub["owner"])
    assert pushed.status_code == 200, pushed.text
    first = dispatched(client, hub["owner"], [2])[0]["id"]
    hidden = client.post(
        f"/v1/projects/{PROJECT}/runs", json={"plan_id": "vault", "steps": [4]}, headers=hub["owner"]
    ).json()[0]["id"]
    last = dispatched(client, hub["owner"], [4])[0]["id"]
    assert control(client, hub["owner"], last, "cancel").status_code == 200

    def listed(headers, **params) -> dict:
        response = client.get(f"/v1/projects/{PROJECT}/runs", params=params, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    # the reader's grant stops at internal: the run of vault is neither listed nor counted
    seen = listed(hub["reader"])
    assert ([run["id"] for run in seen["runs"]], seen["total"]) == ([last, first], 2)
    assert {state: n for state, n in seen["counts"].items() if n} == {"queued": 1, "cancelled": 1}
    assert listed(hub["reader"], plan_id="vault")["total"] == 0 and listed(hub["reader"], q="vault")["total"] == 0
    shown = client.get(f"/v1/projects/{PROJECT}/runs/{hidden}", headers=hub["reader"])
    assert shown.status_code == 404
    # the owner's reaches customer, through the hub sink that clears it: every run, newest first
    every = listed(hub["owner"])
    assert ([run["id"] for run in every["runs"]], every["total"]) == ([last, hidden, first], 3)
    assert {state: n for state, n in every["counts"].items() if n} == {"queued": 2, "cancelled": 1}
    assert [run["plan_id"] for run in every["runs"]] == [PLAN, "vault", PLAN]
    # text: the plan, the branch and the login match too
    assert [run["id"] for run in listed(hub["owner"], q="VAULT")["runs"]] == [hidden]
    assert [run["id"] for run in listed(hub["owner"], q="feat/queue")["runs"]] == [last, first]
    assert listed(hub["owner"], q=OWNER)["total"] == 3 and listed(hub["owner"], q="nobody")["total"] == 0
    assert [run["id"] for run in listed(hub["owner"], state="queued", plan_id="vault")["runs"]] == [hidden]


# Preflight and failure causes


def without_origin(client, hub, *repos: str) -> None:
    """Register the project again, its repos ``repos`` without their origin."""
    body = registration()
    body["repos"] = [{**repo, "origin": None} if repo["name"] in repos else repo for repo in body["repos"]]
    assert client.put(f"/v1/projects/{PROJECT}", json=body, headers=hub["admin"]).status_code == 200


def notices(db, run_id: int) -> list[tuple]:
    n = tables.notifications
    query = select(n.c.user_id, n.c.notice_kind, n.c.title, n.c.body, n.c.details).where(n.c.run_id == run_id)
    return sql(db, query.order_by(n.c.id))


def test_dispatch_and_rerun_of_a_step_whose_repo_has_no_origin_are_409_naming_the_repo(client, hub, hub_db):
    worker = add_worker(client, hub["owner"], "mac-mini")
    old = dispatched(client, hub["owner"], [2], worker_id=worker["id"])[0]
    assert control(client, hub["owner"], old["id"], "cancel").status_code == 200
    without_origin(client, hub, "evo-agents")
    refused = dispatch(client, hub["owner"], [2])
    assert refused.status_code == 409, refused.text
    message = refused.json()["message"]
    assert "step 2 of plan rollout needs evo-agents" in message and f"project {PROJECT} lists no origin" in message
    assert message.endswith("nothing was dispatched")
    again = control(client, hub["owner"], old["id"], "rerun")
    assert again.status_code == 409 and "lists no origin" in again.json()["message"]
    assert count_runs(hub_db) == [(1,)]
    # a repo the project does not list at all is refused the same way
    body = registration()
    body["repos"] = [repo for repo in body["repos"] if repo["name"] != "evo-agents"]
    assert client.put(f"/v1/projects/{PROJECT}", json=body, headers=hub["admin"]).status_code == 200
    assert "needs evo-agents, for which" in dispatch(client, hub["owner"], [2]).json()["message"]
    assert count_runs(hub_db) == [(1,)]


def test_the_claim_of_a_step_run_names_the_verify_its_preflight_checks(client, hub):
    worker = add_worker(client, hub["owner"], "mac-mini", slots=3)
    run = dispatched(client, hub["owner"], [2], worker_id=worker["id"])[0]
    spec = claim(client, worker)
    assert spec["id"] == run["id"] and spec["verify"] == ["test -f feature.txt"]
    held = plan(client, hub["owner"])
    body = held["body"]
    body["steps"][3]["verify"] = "  ruff check . && pytest -q\n"
    pushed = client.put(
        f"/v1/projects/{PROJECT}/plans/{PLAN}",
        json={"body": body, "if_revision": held["revision"]},
        headers=hub["owner"],
    )
    assert pushed.status_code == 200, pushed.text
    dispatched(client, hub["owner"], [4], worker_id=worker["id"])
    assert claim(client, worker)["verify"] == ["ruff check . && pytest -q"]
    dispatched(client, hub["owner"], [5], worker_id=worker["id"])
    assert claim(client, worker)["verify"] == []  # a step without verify: nothing to check


def test_a_failed_report_keeps_its_cause_which_the_run_and_its_list_show(client, hub, hub_db):
    worker = add_worker(client, hub["owner"], "mac-mini", slots=3)
    first, second, third = (dispatched(client, hub["owner"], [key])[0]["id"] for key in (2, 4, 5))
    for _ in range(3):
        claim(client, worker)
    error = "verify command `pnpm test` calls pnpm, which is not on this worker's PATH"
    failed = report(client, worker, first, "failed", error=error, failure_cause="missing_tool")
    assert failed.status_code == 200, failed.text
    assert (failed.json()["state"], failed.json()["failure_cause"]) == ("failed", "missing_tool")
    # a daemon that sends no cause still ends its run; a cause that is no name is refused
    assert report(client, worker, second, "failed", error="the agent gave up").json()["failure_cause"] is None
    bad = report(client, worker, third, "failed", error="x", failure_cause="Not A Cause")
    assert bad.status_code == 422, bad.text
    # a newer worker's own cause is kept; with a move that is not failed, a cause is ignored
    assert moved(client, worker, third, "running")["failure_cause"] is None
    assert report(client, worker, third, "failed", error="x", failure_cause="disk_full").json()["failure_cause"] == (
        "disk_full"
    )
    shown = client.get(f"/v1/projects/{PROJECT}/runs/{first}", headers=hub["reader"]).json()
    assert (shown["failure_cause"], shown["error"]) == ("missing_tool", error)
    listed = client.get(f"/v1/projects/{PROJECT}/runs", headers=hub["reader"]).json()["runs"]
    assert {run["id"]: run["failure_cause"] for run in listed} == {
        first: "missing_tool",
        second: None,
        third: "disk_full",
    }
    assert of_run(hub_db, first, "failure_cause") == [("missing_tool",)]
    assert step(client, hub["owner"], 2)["status"] == "pending"


def test_a_step_run_its_preflight_failed_sends_its_owner_run_failed_with_the_cause(client, hub, hub_db):
    worker = add_worker(client, hub["owner"], "mac-mini", slots=2)
    stopped, worked = (dispatched(client, hub["owner"], [key])[0]["id"] for key in (2, 4))
    claim(client, worker)
    claim(client, worker)
    error = "no credential reads evo-agents (https://git.example.org/evo/evo-agents.git): git could not read Username"
    assert report(client, worker, stopped, "failed", error=error, failure_cause="credentials").status_code == 200
    ((user_id, kind, title, body, details),) = notices(hub_db, stopped)
    owner_id = sql(hub_db, select(tables.users.c.id).where(tables.users.c.login == OWNER))[0][0]
    assert (user_id, kind, body) == (owner_id, "run_failed", error)
    assert title == f"Run #{stopped} of step 2 of {PLAN} failed (credentials)"
    assert details == {
        "run_kind": "step",
        "plan_id": PLAN,
        "step_key": "2",
        "failure_cause": "credentials",
        "error": error,
    }
    # a failure of the work itself is the run page's to show, as before
    moved(client, worker, worked, "running", "verifying")
    error = "verify command `pytest` exited 1; nothing was pushed"
    assert report(client, worker, worked, "failed", error=error, failure_cause="verify_failed").status_code == 200
    assert notices(hub_db, worked) == []


# Dispatch from the web only


@pytest.fixture
def web_client(hub_db, tmp_path, github):
    """``client`` on a hub that also signs in on the web."""
    config = live.hub_config(hub_db, tmp_path, github, **live.web_changes(github))
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def web_hub(web_client, github) -> dict:
    """What ``hub`` sets up, on ``web_client``, with ``owner_web``: the headers of a write the owner makes signed in on
    the web, the session cookie and its CSRF header."""
    headers = members(web_client, github)
    session = web_sign_in(web_client, github, Account(OWNER, GITHUB_IDS[OWNER]))
    return headers | {"owner_web": {**cookie(session), "X-Evo-CSRF": csrf_for(web_client, session)}}


def dispatch_from(client, hub, worker: dict, value: str) -> None:
    response = client.post(f"/v1/workers/{worker['id']}/dispatch-from", json={"value": value}, headers=hub["owner_web"])
    assert response.status_code == 200, response.text
    assert response.json()["dispatch_from"] == value


WEB_ONLY = "takes only runs dispatched from a web session"


def test_dispatch_from_each_run_records_the_credential_it_was_dispatched_with(web_client, web_hub, hub_db):
    client, hub = web_client, web_hub
    by_token = dispatched(client, hub["owner"], [2])[0]
    by_web = dispatched(client, hub["owner_web"], [4])[0]
    assert (by_token["dispatched_via"], by_web["dispatched_via"]) == ("machine", "web")
    shown = client.get(f"/v1/projects/{PROJECT}/runs/{by_web['id']}", headers=hub["reader"]).json()
    assert shown["dispatched_via"] == "web"
    # a rerun records the credential of its own dispatch, not the old run's
    for run in (by_token, by_web):
        assert control(client, hub["owner"], run["id"], "cancel").status_code == 200
    from_web = control(client, hub["owner_web"], by_token["id"], "rerun")
    from_token = control(client, hub["owner"], by_web["id"], "rerun")
    assert from_web.status_code == from_token.status_code == 201
    assert (from_web.json()["dispatched_via"], from_token.json()["dispatched_via"]) == ("web", "machine")
    # and so does a plan run
    for run in (from_web.json(), from_token.json()):
        assert control(client, hub["owner"], run["id"], "cancel").status_code == 200
    body = {"plan_id": PLAN}
    plan_run = client.post(f"/v1/projects/{PROJECT}/plan-runs", json=body, headers=hub["owner_web"])
    assert plan_run.status_code == 201, plan_run.text
    assert plan_run.json()["dispatched_via"] == "web"
    via = tables.runs.c.dispatched_via
    assert sql(hub_db, select(via, func.count()).group_by(via).order_by(via)) == [
        ("machine", 2),
        ("web", 3),
    ]


def test_a_worker_set_to_dispatch_from_web_claims_only_runs_dispatched_from_the_web(web_client, web_hub, hub_db):
    client, hub = web_client, web_hub
    guarded = add_worker(client, hub["owner"], "mac-mini")
    dispatch_from(client, hub, guarded, "web")

    # a token pinning a run to it is refused, saying why, and nothing is queued; a plan run too
    refused = dispatch(client, hub["owner"], [2], worker_id=guarded["id"])
    assert refused.status_code == 403 and f"worker mac-mini {WEB_ONLY}" in refused.json()["message"]
    body = {"plan_id": PLAN, "worker_id": guarded["id"]}
    plan_run = client.post(f"/v1/projects/{PROJECT}/plan-runs", json=body, headers=hub["owner"])
    assert plan_run.status_code == 403 and WEB_ONLY in plan_run.json()["message"]
    assert count_runs(hub_db) == [(0,)]

    # its claims pass over a run dispatched with a token, and take the next one dispatched from the web
    by_token = dispatched(client, hub["owner"], [2])[0]
    assert claim(client, guarded) is None
    by_web = dispatched(client, hub["owner_web"], [4])[0]
    assert claim(client, guarded)["id"] == by_web["id"]
    # the next attempt of a lost run keeps the credential of its dispatch, so it comes back here, once steady
    moved(client, guarded, by_web["id"], "running")
    expire(hub_db, by_web["id"])
    assert recover(client)["lost"] == 1
    steady(hub_db, guarded)
    retry = claim(client, guarded)
    assert (retry["parent_run_id"], retry["attempt"]) == (by_web["id"], 2)
    # a worker of the owner's that takes runs from anywhere takes the run dispatched with a token, and its retry
    # stays with such workers
    anywhere = add_worker(client, hub["owner"], "linux-box")
    assert claim(client, anywhere)["id"] == by_token["id"]
    moved(client, anywhere, by_token["id"], "running")
    expire(hub_db, by_token["id"])
    assert recover(client)["lost"] == 1
    moved(client, guarded, retry["id"], "running", "failed", error="the agent stopped")
    assert claim(client, guarded) is None
    steady(hub_db, anywhere)
    assert claim(client, anywhere)["parent_run_id"] == by_token["id"]
    run = tables.runs
    retried = select(run.c.dispatched_via).where(run.c.parent_run_id.in_([by_web["id"], by_token["id"]]))
    assert sql(hub_db, retried.order_by(run.c.id)) == [("web",), ("machine",)]

    # pinned from the web, it takes the run; a rerun of that run with a token is refused, one from the web is not
    pinned = dispatched(client, hub["owner_web"], [5], worker_id=guarded["id"])[0]
    assert claim(client, guarded)["id"] == pinned["id"]
    moved(client, guarded, pinned["id"], "running", "failed", error="the agent stopped")
    again = control(client, hub["owner"], pinned["id"], "rerun")
    assert again.status_code == 403 and WEB_ONLY in again.json()["message"]
    rerun = control(client, hub["owner_web"], pinned["id"], "rerun")
    assert rerun.status_code == 201, rerun.text

    # a run dispatched before schema 0011 recorded no credential, and is passed over too, until the owner lets the
    # worker take runs from anywhere again
    set_run(hub_db, rerun.json()["id"], dispatched_via=None)
    assert claim(client, guarded) is None
    dispatch_from(client, hub, guarded, "any")
    assert claim(client, guarded)["id"] == rerun.json()["id"]


def test_a_token_cannot_message_a_run_a_worker_set_to_web_holds_or_may_claim(web_client, web_hub, hub_db):
    client, hub = web_client, web_hub
    guarded = add_worker(client, hub["owner"], "mac-mini")
    anywhere = add_worker(client, hub["owner"], "linux-box")
    dispatch_from(client, hub, guarded, "web")

    def message(run_id: int, headers):
        path = f"/v1/projects/{PROJECT}/runs/{run_id}/messages"
        return client.post(path, json={"text": "Keep the old API."}, headers=headers)

    # queued without a pin and dispatched from the web: the worker set to web may claim it
    by_web = dispatched(client, hub["owner_web"], [2])[0]
    refused = message(by_web["id"], hub["owner"])
    assert refused.status_code == 403
    assert refused.json()["message"] == (
        f"run {by_web['id']} may go to worker mac-mini, which {WEB_ONLY}, as its owner set it, so a token cannot send "
        "its agent a message: send it from the web; nothing was sent"
    )
    assert message(by_web["id"], hub["owner_web"]).status_code == 201
    # dispatched with a token, it never goes there: a token's message is taken, held by a worker that takes any
    by_token = dispatched(client, hub["owner"], [4])[0]
    assert message(by_token["id"], hub["owner"]).status_code == 201
    assert claim(client, anywhere)["id"] == by_web["id"]
    assert message(by_web["id"], hub["owner"]).status_code == 201, "held by linux-box, which takes any"
    assert claim(client, guarded) is None
    inbox = tables.run_inbox
    assert sql(hub_db, select(func.count()).select_from(inbox).where(inbox.c.run_id == by_web["id"])) == [(2,)]


# Pruning and the periodic jobs


def test_events_of_runs_that_ended_long_ago_are_pruned(client, hub, hub_db):
    worker = add_worker(client, hub["owner"], "mac-mini", slots=2)
    old, recent = (dispatched(client, hub["owner"], [key])[0]["id"] for key in (4, 5))
    for run_id in (old, recent):
        assert control(client, hub["owner"], run_id, "cancel").status_code == 200
    set_run(hub_db, old, finished_at=func.now() - timedelta(days=31))
    set_run(hub_db, recent, finished_at=func.now() - timedelta(days=29))
    running = dispatched(client, hub["owner"], [2])[0]["id"]
    claim(client, worker)
    state = client.app.state
    found = SimpleNamespace(engine=state.engine, config=state.config, sealer=state.sealer, github_app=state.github_app)
    context = SimpleNamespace(additional_context={"hub": found})
    report_ = client.portal.call(queue.tasks[jobs.PRUNE_RUN_EVENTS].func, context)
    assert report_ == {"deleted": 1, "days": 30, "tokens_dropped": 0}
    events = tables.run_events
    left = dict(sql(hub_db, select(events.c.run_id, func.count()).group_by(events.c.run_id)))
    assert left == {recent: 1, running: 1}
    assert count_runs(hub_db) == [(3,)]  # the runs stay; only their events go
    assert client.portal.call(queue.tasks[jobs.RECOVER_RUNS].func, context) == {
        "lost": 0,
        "failed": 0,
        "cancelled": 0,
        "parked": 0,
    }


def test_the_reaper_runs_every_minute_and_the_pruning_daily_each_queued_at_most_once():
    periodic = {p.task.name: p for p in queue.periodic_registry.periodic_tasks.values()}
    assert periodic[jobs.RECOVER_RUNS].cron == "* * * * *"
    assert periodic[jobs.PRUNE_RUN_EVENTS].cron == "13 4 * * *"
    for name in (jobs.RECOVER_RUNS, jobs.PRUNE_RUN_EVENTS):
        assert queue.tasks[name].queueing_lock == name


def test_run_events_are_kept_30_days_unless_evo_hub_run_log_days_says_otherwise():
    env = {"EVO_HUB_DSN": "postgresql://hub@db/hub"}
    assert load_config(env).run_log_days == 30
    assert load_config({**env, "EVO_HUB_RUN_LOG_DAYS": "7"}).run_log_days == 7
    for value in ("0", "3651", "a month"):
        with pytest.raises(ConfigError) as caught:
            load_config({**env, "EVO_HUB_RUN_LOG_DAYS": value})
        assert caught.value.variable == "EVO_HUB_RUN_LOG_DAYS"

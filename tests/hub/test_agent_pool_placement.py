"""Where the runs of an agent in pool mode go, step 2 of the plan agent-dedicated-pools (criteria a2 and a7, a3 in part).

a2: every run of an agent in pool mode gets the selector pool=agent-<handle> and the pool's taints as tolerations; only
the ready nodes of that pool claim it, whoever dispatched it, and no member's worker nor shared worker outside the pool
does; a chat run prefers the node that ran the previous turn of its chat; with no ready node the run stays queued and
the admins of the project hear of it once for each outage; the dispatch, plan-run, author-run and rerun routes answer
422 with guidance for worker_id, pool, labels or tolerations; a node of an agent's pool takes no run of another agent.
a7: a local run and the worker protocol v1 behave as before (step 8 removed the legacy mode).
"""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from evo_agents.hub import runs, tables
from evo_agents.hub.server.app import create_app
from tests.hub import live
from tests.hub import test_credentials_api as credentials_api
from tests.hub.account_pool_setup import give_account_pool
from tests.hub.live import ADMIN, bearer, sql
from tests.hub.test_chat_runs import (  # noqa: F401  (fixtures and helpers of the chat tests)
    ADMIN_GRANT,
    OTHER,
    OWNER,
    STRANGER,
    app_key,
    beat,
    chat_worker,
    created,
    ended,
    run_of,
    sent,
    started,
)
from tests.hub.test_chats import OPEN_SINKS
from tests.hub.test_runs import CHECKOUTS as RUN_CHECKOUTS
from tests.hub.test_runs import (  # noqa: F401  (fixtures)
    PLAN,
    PROJECT,
    claim,
    client,
    control,
    dispatch,
    dispatched,
    moved,
)
from tests.hub.test_runs import hub as runs_hub  # noqa: F401  (a fixture)

POOLS = "/v1/worker-pools"
EMPTY = "agent_pool_empty"
HIGH = {"code": "other-admin", "severity": "high", "detail": "carol administers this machine (group sudo)."}


@pytest.fixture
def hub(client, runs_hub) -> dict:
    """The project and members of ``tests.hub.test_runs.hub``, where owner is an admin of the project too."""
    response = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=runs_hub["admin"])
    assert response.status_code == 200, response.text
    return runs_hub


def worker(client, headers, name: str) -> dict:
    """A worker of a member, whose daemon says it runs every kind of run and applies agent profiles."""
    return chat_worker(client, headers, name, checkouts=RUN_CHECKOUTS, pool=None)


def make_agent(client, headers, handle: str, *, db=None, account_pool: bool = True, **body) -> dict:
    """An agent in pool mode, with an account pool of the headers' member (an agent in pool mode with none is refused
    a dispatch: ``tests.hub.test_account_pools``)."""
    response = client.post(
        f"/v1/projects/{PROJECT}/agents", json={"handle": handle, "name": handle.title(), **body}, headers=headers
    )
    assert response.status_code == 201, response.text
    if account_pool and db is not None:
        give_account_pool(db, PROJECT, handle, OWNER)
    return response.json()


def pool_id(db, handle: str) -> int:
    p = tables.worker_pools
    return sql(db, select(p.c.id).where(p.c.name == f"agent-{handle}"))[0][0]


def join(client, db, worker: dict, handle: str, *findings: dict) -> dict:
    """``worker`` made a node of the pool of agent ``handle``, with a doctor report of ``findings``: ready unless one is
    a high finding."""
    sql(db, update(tables.workers).where(tables.workers.c.id == worker["id"]).values(pool_id=pool_id(db, handle)))
    doctor(client, worker, *findings)
    return worker


def doctor(client, worker: dict, *findings: dict) -> None:
    response = client.post("/v1/worker/doctor", json={"findings": list(findings)}, headers=worker["headers"])
    assert response.status_code == 200, response.text


def run_row(db, run_id: int) -> tuple:
    r = tables.runs
    return sql(db, select(r.c.selector, r.c.tolerations, r.c.pinned_worker_id, r.c.state).where(r.c.id == run_id))[0]


def runs_count(db) -> int:
    return sql(db, select(func.count()).select_from(tables.runs))[0][0]


def notices(db) -> list[tuple]:
    n = tables.notifications
    query = select(n.c.user_id, n.c.details).where(n.c.notice_kind == EMPTY).order_by(n.c.id)
    return sql(db, query)


def grant_admin(client, hub, login: str) -> None:
    response = client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=ADMIN_GRANT, headers=hub["admin"])
    assert response.status_code == 200, response.text


# The runs of an agent in pool mode


def test_a_run_of_an_agent_in_pool_mode_gets_its_pools_selector_and_taints_and_only_its_nodes_claim_it(
    client, hub, hub_db
):
    make_agent(client, hub["owner"], "helper", db=hub_db)
    taints = client.patch(f"{POOLS}/agent-helper", json={"taints": ["gpu"]}, headers=hub["owner"])
    assert taints.status_code == 200, taints.text
    mine = worker(client, hub["owner"], "mine")  # the dispatcher's own worker, outside the pool
    theirs = worker(client, hub["other"], "theirs")  # a member's worker of the project, outside the pool
    node = join(client, hub_db, worker(client, hub["other"], "node"), "helper")  # not the dispatcher's
    (run,) = dispatched(client, hub["owner"], [2], profile="helper")
    selector, tolerations, pinned, state = run_row(hub_db, run["id"])
    assert (selector, tolerations, pinned, state) == (["pool=agent-helper"], ["gpu"], None, "queued")
    assert claim(client, mine) is None
    assert claim(client, theirs) is None
    assert claim(client, node)["id"] == run["id"], "a ready node of the pool takes it, whoever dispatched it"


def test_a_pool_node_that_is_not_ready_takes_no_run_and_the_run_waits_for_a_ready_one(client, hub, hub_db):
    make_agent(client, hub["owner"], "helper", db=hub_db)
    node = join(client, hub_db, worker(client, hub["owner"], "node"), "helper", HIGH)
    (run,) = dispatched(client, hub["owner"], [2], profile="helper")
    assert claim(client, node) is None
    doctor(client, node)
    assert claim(client, node)["id"] == run["id"]


def test_a_node_of_an_agents_pool_takes_the_runs_of_that_agent_alone(client, hub, hub_db):
    make_agent(client, hub["owner"], "helper", db=hub_db)
    make_agent(client, hub["owner"], "other-agent", db=hub_db)
    node = join(client, hub_db, worker(client, hub["owner"], "node"), "helper")
    (elsewhere,) = dispatched(client, hub["owner"], [2], profile="other-agent")
    assert claim(client, node) is None, "the run of another agent in pool mode is not its"
    (mine,) = dispatched(client, hub["owner"], [4], profile="helper")
    assert claim(client, node)["id"] == mine["id"], "its own agent's run is"
    assert elsewhere["id"] != mine["id"]


def test_the_default_agent_runs_in_the_same_way_in_pool_mode(client, hub, hub_db):
    """A run that names no agent goes to the project's default agent, and so to its pool."""
    make_agent(client, hub["owner"], "helper", db=hub_db)  # makes the project's default agent too, when it has none
    default = client.get(f"/v1/projects/{PROJECT}/agents/{PROJECT}", headers=hub["owner"])
    assert default.status_code == 200, default.text
    assert "placement" not in default.json()
    assert default.json()["account_pool"] is not None  # the default agent needs an account pool too
    mine = worker(client, hub["owner"], "mine")
    (run,) = dispatched(client, hub["owner"], [2])
    assert run_row(hub_db, run["id"])[:2] == ([f"pool=agent-{PROJECT}"], [])
    assert claim(client, mine) is None
    node = join(client, hub_db, worker(client, hub["other"], "node"), PROJECT)
    assert claim(client, node)["id"] == run["id"]


def test_a_node_joined_to_an_agents_pool_is_the_token_creators_worker_though_the_agents_creator_left(
    client, hub, hub_db
):
    """The member who created the agent owns its pool; once they hold no grant on the project, another admin of the
    project creates a join token and the machine joins as that admin's worker, which takes the agent's runs."""
    from tests.hub.test_workers import HOST, PROTOCOL

    make_agent(client, hub["owner"], "helper", db=hub_db)
    grant_admin(client, hub, OTHER)
    gone = client.delete(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", headers=hub["admin"])
    assert gone.status_code in (200, 204), gone.text
    issued = client.post(f"{POOLS}/agent-helper/tokens", json={}, headers=hub["other"])
    assert issued.status_code == 201, issued.text
    joined = client.post("/v1/worker/join", json={"token": issued.json()["token"], **HOST}, headers=PROTOCOL)
    assert joined.status_code == 201, joined.text
    node = joined.json()["worker"]
    assert (node["owner"], node["pool"]) == (OTHER, "agent-helper")
    w = tables.workers
    assert sql(hub_db, select(w.c.pool_id).where(w.c.id == node["id"])) == [(pool_id(hub_db, "helper"),)]


# 422 for a machine nobody picks


def test_a_dispatch_that_picks_the_machine_is_refused_with_guidance(client, hub, hub_db):
    make_agent(client, hub["owner"], "helper", db=hub_db)
    mine = worker(client, hub["owner"], "mine")
    before = runs_count(hub_db)
    asked = [
        {"worker_id": mine["id"]},
        {"pool": "any"},
        {"labels": ["gpu"]},
        {"tolerations": ["gpu"]},
    ]
    for extra in asked:
        response = dispatch(client, hub["owner"], [2], profile="helper", **extra)
        assert response.status_code == 422, (extra, response.text)
        assert "agent-<handle>" in response.text, response.text
    plan = client.post(
        f"/v1/projects/{PROJECT}/plan-runs",
        json={"plan_id": PLAN, "profile": "helper", "worker_id": mine["id"]},
        headers=hub["owner"],
    )
    assert plan.status_code == 422 and "agent-<handle>" in plan.text, plan.text
    author = client.post(
        f"/v1/projects/{PROJECT}/author-runs",
        json={"request": "Write a plan.", "profile": "helper", "labels": ["gpu"]},
        headers=hub["owner"],
    )
    assert author.status_code == 422 and "agent-<handle>" in author.text, author.text
    assert runs_count(hub_db) == before, "nothing was dispatched"


def test_a_rerun_of_a_run_of_an_agent_in_pool_mode_stays_in_the_pool_and_refuses_a_machine(client, hub, hub_db):
    make_agent(client, hub["owner"], "helper", db=hub_db)
    node = join(client, hub_db, worker(client, hub["owner"], "node"), "helper")
    mine = worker(client, hub["owner"], "mine")
    (run,) = dispatched(client, hub["owner"], [2], profile="helper")
    spec = claim(client, node)
    assert spec["id"] == run["id"]
    moved(client, node, run["id"], "running", "failed", error="the agent stopped")
    path = f"/v1/projects/{PROJECT}/runs/{run['id']}/rerun"
    for body in ({"pool": "any"}, {"labels": ["gpu"]}, {"tolerations": ["gpu"]}):
        refused = client.post(path, json=body, headers=hub["owner"])
        assert refused.status_code == 422, (body, refused.text)
        assert "agent-<handle>" in refused.text
    again = client.post(path, headers=hub["owner"])
    assert again.status_code == 201, again.text
    selector, _, pinned, state = run_row(hub_db, again.json()["id"])
    assert (selector, state) == (["pool=agent-helper"], "queued")
    assert pinned in (None, node["id"])
    assert claim(client, mine) is None


# No node ready


def test_a_run_with_no_ready_node_stays_queued_and_the_admins_hear_of_each_outage_once(client, hub, hub_db):
    grant_admin(client, hub, OTHER)
    make_agent(client, hub["owner"], "helper", db=hub_db)
    node = join(client, hub_db, worker(client, hub["owner"], "node"), "helper", HIGH)  # not ready
    first = dispatched(client, hub["owner"], [2], profile="helper")[0]
    second = dispatched(client, hub["owner"], [4], profile="helper")[0]
    assert [run_row(hub_db, r["id"])[3] for r in (first, second)] == ["queued", "queued"]
    found = notices(hub_db)
    admins = {user_id for user_id, _ in found}
    assert len(admins) == 2 and len(found) == 2, "one notice for each admin, not for each run"
    assert all(d["agent"] == "helper" and d["pool"] == "agent-helper" for _, d in found)
    assert claim(client, node) is None

    assert control(client, hub["owner"], second["id"], "cancel").status_code == 200
    doctor(client, node)  # the pool has a node again
    assert claim(client, node)["id"] == first["id"]
    (third,) = dispatched(client, hub["owner"], [4], profile="helper")
    assert len(notices(hub_db)) == 2

    doctor(client, node, HIGH)  # and runs out of one a second time
    dispatched(client, hub["owner"], [5], profile="helper")
    again = notices(hub_db)
    assert sorted(user_id for user_id, _ in again) == sorted([*admins, *admins])
    assert third["id"] != second["id"]


# Chat


@pytest.fixture
def chat_client(hub_db, tmp_path, github, app_key):  # noqa: F811
    config = credentials_api.hub_config(hub_db, tmp_path, github, app_key)
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        client.db = hub_db
        yield client


@pytest.fixture
def chat_hub(chat_client, hub_db, github) -> dict:
    """The project of the chat tests, with the agent @reviewer in pool mode."""
    client = chat_client
    headers = {
        "admin": bearer(live.sign_in(client, github, ADMIN, credentials_api.ADMIN_ID)["token"]),
        "owner": bearer(live.sign_in(client, github, OWNER, credentials_api.OWNER_ID)["token"]),
        "other": bearer(live.sign_in(client, github, OTHER, credentials_api.OTHER_ID)["token"]),
        "stranger": bearer(live.sign_in(client, github, STRANGER, 503)["token"]),
    }
    body = {**credentials_api.registration(credentials_api.DISPATCHED), "sinks": OPEN_SINKS}
    assert client.put(f"/v1/projects/{PROJECT}", json=body, headers=headers["admin"]).status_code == 200
    for login, grant in ((OWNER, ADMIN_GRANT), (OTHER, credentials_api.WRITER)):
        response = client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=grant, headers=headers["admin"])
        assert response.status_code == 200, response.text
    make_agent(client, headers["owner"], "reviewer", db=hub_db)
    return headers


def chat_node(client, db, headers, name: str, features=None) -> dict:
    worker = chat_worker(client, headers, name, pool=None)
    return join(client, db, worker, "reviewer")


def chat_runs_of(db) -> list[tuple]:
    r = tables.runs
    query = select(r.c.id, r.c.state, r.c.pinned_worker_id, r.c.selector).where(r.c.kind == "chat")
    return sql(db, query.order_by(r.c.id))


def test_a_chat_run_goes_to_a_node_of_the_pool_and_the_next_turn_to_the_node_of_the_last_one(
    chat_client, chat_hub, hub_db
):
    client, hub = chat_client, chat_hub
    outside = chat_worker(client, hub["owner"], "studio", pool=None)  # the sender's own worker, outside the pool
    one = chat_node(client, hub_db, hub["other"], "node-one")
    two = chat_node(client, hub_db, hub["other"], "node-two")
    chat = created(client, hub["owner"], people=[OTHER])
    mention = sent(client, hub["owner"], chat["id"], "@reviewer please fix the flaky wait test")
    ((first, state, pinned, selector),) = chat_runs_of(hub_db)
    assert (state, selector) == ("queued", ["pool=agent-reviewer"])
    assert pinned in (one["id"], two["id"])
    mine = one if pinned == one["id"] else two
    spare = two if mine is one else one
    assert claim(client, outside) is None, "a worker outside the pool takes no chat run of the agent"
    assert claim(client, spare) is None, "it is pinned to the node it was given"
    spec = claim(client, mine)
    assert spec is not None and spec["id"] == first
    moved(client, mine, first, "running", session_id="sess-1")
    ended(client, mine, first)
    sent(client, hub["owner"], chat["id"], "@reviewer and the docs?", mention["id"])
    (_, (next_id, state, pinned, selector)) = chat_runs_of(hub_db)
    assert (state, pinned, selector) == ("queued", mine["id"], ["pool=agent-reviewer"])
    assert claim(client, mine)["id"] == next_id

    # the node of the last turn fails its doctor while it runs the turn: the run that goes on goes to another node
    moved(client, mine, next_id, "running")
    doctor(client, mine, HIGH)
    ended(client, mine, next_id)
    (_, _, (last_id, state, pinned, selector)) = chat_runs_of(hub_db)
    assert (state, pinned, selector) == ("queued", None, ["pool=agent-reviewer"])
    assert claim(client, mine) is None, "a node that is not ready takes no run"
    assert claim(client, spare)["id"] == last_id
    assert runs.RUN_KINDS  # the chat kind is among the kinds a daemon says it runs


def test_a_chat_run_with_no_ready_node_waits_in_the_queue_and_tells_the_admins(chat_client, chat_hub, hub_db):
    client, hub = chat_client, chat_hub
    outside = chat_worker(client, hub["owner"], "studio", pool=None)
    chat = created(client, hub["owner"])
    sent(client, hub["owner"], chat["id"], "@reviewer please fix the flaky wait test")
    ((_, state, pinned, selector),) = chat_runs_of(hub_db)
    assert (state, pinned, selector) == ("queued", None, ["pool=agent-reviewer"])
    assert claim(client, outside) is None
    assert len(notices(hub_db)) >= 1
    node = chat_node(client, hub_db, hub["other"], "late-node")
    assert claim(client, node)["kind"] == "chat"


# a7: the runs and the protocol that are not placed by agent


def test_a_local_run_does_not_ask_for_a_node_of_a_pool(client, hub, hub_db):
    make_agent(client, hub["owner"], "helper", db=hub_db)
    registered = client.put(
        f"/v1/projects/{PROJECT}/local-runs/0199a3c1-0000-7000-8000-0000000000a1",
        json={"machine": "mac-mini", "cwd": "/Users/me/evo-agents", "claude_email": "me@example.com"},
        headers=hub["owner"],
    )
    assert registered.status_code == 201, registered.text
    run = registered.json()
    selector, tolerations, pinned, state = run_row(hub_db, run["id"])
    assert (selector, tolerations, pinned, state) == ([], [], None, "running")
    mine = worker(client, hub["owner"], "mine")
    assert claim(client, mine) is None, "a local run is never claimed"

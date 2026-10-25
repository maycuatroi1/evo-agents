"""Worker pools on the hub, step 1 of the worker-pools plan (criterion a1): a member creates a pool and its ``evj_``
join tokens; one token joins N machines as N workers of the pool, each with its own ``evw_`` token, a name made of the
pool's and the machine's with a suffix when it collides, and the pool's labels and pool=<name>. A token that is
unknown, expired, revoked or used up gets the same 403 as a wrong code, under the same limit per address; every join,
pool and token change leaves its audit row; the hub keeps only the SHA-256 of a token; a pool's new settings reach
its nodes' heartbeat answers; and joining with a pairing code works as it did (a9)."""

import hashlib
import logging
from datetime import timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select, update

from evo_agents.hub import tables
from evo_agents.hub.log import scrub
from evo_agents.hub.server import audit
from tests.hub.live import sql
from tests.hub.test_workers import (  # noqa: F401 - the fixtures of the worker tests
    HOST,
    OWNER,
    PROTOCOL,
    admin,
    audit_rows,
    client,
    config,
    count,
    hub,
    joined,
    paired,
    unknown_code,
    worker_headers,
)

POOLS = "/v1/worker-pools"


def create(client, headers, name="ci", projects=("demo",), **extra):
    return client.post(POOLS, json={"name": name, "projects": list(projects), **extra}, headers=headers)


def created(client, headers, **extra) -> dict:
    response = create(client, headers, **extra)
    assert response.status_code == 201, response.text
    return response.json()


def token_of(client, headers, pool="ci", **extra) -> dict:
    response = client.post(f"{POOLS}/{pool}/tokens", json=extra, headers=headers)
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def join(client, token: str, **host):
    return client.post("/v1/worker/join", json={"token": token, **HOST, **host}, headers=PROTOCOL)


def pool_joined(client, token: str, **host) -> dict:
    response = join(client, token, **host)
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def beat(client, token: str) -> dict:
    response = client.post("/v1/worker/heartbeat", json={"free_slots": 1}, headers=worker_headers(token))
    assert response.status_code == 200, response.text
    return response.json()["settings"]


def test_a_pool_is_created_with_its_settings_and_shown_to_its_owner(client, hub, hub_db):
    pool = created(
        client,
        hub["owner"],
        name="CI",
        projects=["docs", "demo", "docs"],
        slots=2,
        labels=["gpu", "zone=eu", "gpu"],
        taints=["dedicated=ci"],
        accepted_findings=["no-docker"],
    )
    assert (pool["name"], pool["owner"], pool["projects"], pool["slots"]) == ("CI", OWNER, ["demo", "docs"], 2)
    assert (pool["labels"], pool["taints"], pool["accepted_findings"]) == (
        ["gpu", "zone=eu"],
        ["dedicated=ci"],
        ["no-docker"],
    )
    assert (pool["reap_after_days"], pool["nodes"]) == (7, [])
    assert client.get(f"{POOLS}/ci", headers=hub["owner"]).json() == pool  # the name in any case
    assert [p["name"] for p in client.get(POOLS, headers=hub["owner"]).json()] == ["CI"]
    assert audit_rows(hub_db, audit.WORKER_POOL_CREATE) == [(f"pool:{pool['id']} name=CI", OWNER)]

    # unique among the owner's pools in any case; another member has a namespace of their own
    assert create(client, hub["owner"], name="ci").status_code == 409
    assert create(client, hub["other"], name="ci").status_code == 201
    # another member's pool is 404 for everything, as one the hub never had
    created(client, hub["owner"], name="mine")
    for method, path, body in (
        ("GET", "/mine", None),
        ("PATCH", "/mine", {"slots": 3}),
        ("POST", "/mine/tokens", {}),
        ("GET", "/mine/tokens", None),
        ("POST", "/mine/tokens/1/revoke", None),
    ):
        response = client.request(method, POOLS + path, json=body, headers=hub["other"])
        assert response.status_code == 404, (method, path, response.text)
    assert [p["name"] for p in client.get(POOLS, headers=hub["other"]).json()] == ["ci"]


def test_a_pool_needs_the_writer_role_on_each_project_and_settings_within_bounds(client, hub, hub_db):
    assert create(client, hub["owner"], projects=["demo", "notes"]).status_code == 403  # a reader on notes
    assert create(client, hub["owner"], projects=["nope"]).status_code == 404
    for wrong in (
        {"reap_after_days": 0},
        {"reap_after_days": 91},
        {"slots": 0},
        {"slots": 17},
        {"labels": ["pool=other"]},  # the hub gives pool= itself
        {"taints": ["os=linux"]},
        {"labels": ["no spaces"]},
        {"accepted_findings": ["Not-A-Code"]},
        {"projects": []},
        {"name": "-dash"},
        {"name": "x" * 41},
    ):
        response = create(client, hub["owner"], **wrong)
        assert response.status_code == 422, (wrong, response.text)
    assert count(hub_db, tables.worker_pools) == [(0,)]
    for days in (1, 90):
        assert created(client, hub["owner"], name=f"days-{days}", reap_after_days=days)["reap_after_days"] == days
    for wrong in ({"hours": 0}, {"hours": 721}, {"max_uses": 0}, {"max_uses": 101}):
        response = client.post(f"{POOLS}/days-1/tokens", json=wrong, headers=hub["owner"])
        assert response.status_code == 422, (wrong, response.text)
    assert client.patch(f"{POOLS}/days-1", json={}, headers=hub["owner"]).status_code == 422
    assert client.patch(f"{POOLS}/days-1", json={"reap_after_days": 91}, headers=hub["owner"]).status_code == 422


def test_one_join_token_joins_n_machines_as_n_workers_of_the_pool(client, hub, hub_db):
    pool = created(client, hub["owner"], projects=["demo", "docs"], slots=3, labels=["gpu"])
    issued = token_of(client, hub["owner"], hours=2, max_uses=3)
    token = issued["token"]
    assert token.startswith("evj_") and len(token) == 4 + 43
    assert (issued["status"], issued["uses"], issued["max_uses"], issued["pool"]) == ("live", 0, 3, "ci")
    jt = tables.worker_join_tokens
    assert sql(hub_db, select(jt.c.token_hash)) == [(hashlib.sha256(token.encode()).hexdigest(),)]
    assert token not in str(client.get(f"{POOLS}/ci/tokens", headers=hub["owner"]).json())

    # a live worker of the owner named ci-box already: the first node takes a suffix
    paired_one = paired(client, hub["owner"], name="ci-box")
    assert joined(client, paired_one["code"])["worker"]["name"] == "ci-box"
    answers = [
        pool_joined(client, token, hostname="box.example.org"),
        pool_joined(client, token, hostname="BOX"),
        pool_joined(client, token, hostname="ci-runner-7.local"),
    ]
    names = [answer["worker"]["name"] for answer in answers]
    assert names == ["ci-box-2", "ci-box-3", "ci-runner-7"]
    tokens = {answer["token"] for answer in answers}
    assert len(tokens) == 3 and all(t.startswith("evw_") for t in tokens)
    for answer in answers:
        worker = answer["worker"]
        assert (worker["owner"], worker["pool"], worker["projects"], worker["slots"]) == (
            OWNER,
            "ci",
            ["demo", "docs"],
            3,
        )
        assert worker["labels"] == ["gpu", "pool=ci"]
        assert worker["allow_web_terminal"] is False
        assert beat(client, answer["token"])["labels"] == ["gpu", "pool=ci"]
    shown = client.get(f"{POOLS}/ci", headers=hub["owner"]).json()
    assert sorted(node["name"] for node in shown["nodes"]) == sorted(names)
    assert {node["status"] for node in shown["nodes"]} == {"online"}

    # used up: the fourth machine gets the 403 of a wrong code
    refused = join(client, token, hostname="box-4")
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"] == "forbidden"
    assert count(hub_db, tables.workers) == [(4,)]
    (listed,) = client.get(f"{POOLS}/ci/tokens", headers=hub["owner"]).json()
    assert (listed["status"], listed["uses"]) == ("used", 3)
    targets = [target for target, _ in audit_rows(hub_db, audit.WORKER_JOIN) if "pool:" in target]
    assert targets == [
        f"worker:{a['worker']['id']} name={a['worker']['name']} pool:{pool['id']} join_token:{issued['id']}"
        for a in answers
    ]
    assert audit_rows(hub_db, audit.WORKER_POOL_TOKEN) == [
        (f"pool:{pool['id']} name=ci join_token:{issued['id']} hours=2 max_uses=3", OWNER)
    ]


def test_unknown_expired_revoked_and_used_tokens_get_the_403_of_a_wrong_code_under_its_limit(client, hub, hub_db):
    now = [1000.0]
    client.app.state.join_refusals.clock = lambda: now[0]
    created(client, hub["owner"])
    expired, revoked, used, live = (token_of(client, hub["owner"]) for _ in range(4))
    jt = tables.worker_join_tokens
    sql(
        hub_db,
        update(jt)
        .values(created_at=jt.c.created_at - pg_hours(2), expires_at=jt.c.created_at - pg_hours(1))
        .where(jt.c.id == expired["id"]),
    )
    response = client.post(f"{POOLS}/ci/tokens/{revoked['id']}/revoke", headers=hub["owner"])
    assert response.status_code == 200 and response.json()["status"] == "revoked"
    assert client.post(f"{POOLS}/ci/tokens/{revoked['id']}/revoke", headers=hub["owner"]).status_code == 409
    assert client.post(f"{POOLS}/ci/tokens/999999/revoke", headers=hub["owner"]).status_code == 404
    pool_joined(client, used["token"], hostname="first")
    pairing = paired(client, hub["owner"])

    wrong = [expired["token"], revoked["token"], used["token"], "evj_" + "A" * 43, "not-a-token"]
    bodies = []
    for token in wrong:
        response = join(client, token, hostname="late")
        assert response.status_code == 403, response.text
        bodies.append(response.json())
        now[0] += 30
    assert len({body["message"] for body in bodies}) == 1  # the same refusal, naming no reason
    for _ in range(5):  # and wrong codes count against the same address
        assert (
            client.post("/v1/worker/join", json={"code": unknown_code(pairing), **HOST}, headers=PROTOCOL).status_code
            == 403
        )
    limited = join(client, live["token"])  # the live token waits too
    assert limited.status_code == 429, limited.text
    assert limited.headers["retry-after"]
    statuses = {t["id"]: t["status"] for t in client.get(f"{POOLS}/ci/tokens", headers=hub["owner"]).json()}
    assert statuses == {expired["id"]: "expired", revoked["id"]: "revoked", used["id"]: "used", live["id"]: "live"}
    assert sql(hub_db, select(jt.c.uses).where(jt.c.id == live["id"])) == [(0,)]  # a 429 never reaches the token
    assert count(hub_db, tables.workers) == [(1,)]
    assert audit_rows(hub_db, audit.WORKER_POOL_TOKEN_REVOKE) == [
        (f"pool:{pool_id(hub_db)} name=ci join_token:{revoked['id']}", OWNER)
    ]


def pg_hours(hours: int) -> timedelta:
    return timedelta(hours=hours)


def pool_id(db) -> int:
    ((found,),) = sql(db, select(tables.worker_pools.c.id))
    return found


def test_a_pools_new_settings_reach_its_nodes_heartbeat_answers(client, hub, hub_db):
    pool = created(client, hub["owner"], labels=["gpu"])
    token = token_of(client, hub["owner"], max_uses=2)["token"]
    first, second = (pool_joined(client, token, hostname=f"node-{n}") for n in (1, 2))
    assert beat(client, first["token"])["slots"] == 1
    # a node's own labels stay its own: PATCH /v1/workers/{id} changes them, the pool's come after
    worker_id = first["worker"]["id"]
    response = client.patch(f"/v1/workers/{worker_id}", json={"labels": ["fast"]}, headers=hub["owner"])
    assert response.status_code == 200, response.text
    assert response.json()["labels"] == ["fast", "gpu", "pool=ci"]

    body = {"slots": 4, "labels": ["gpu", "big"], "projects": ["demo", "docs"], "taints": ["t"], "reap_after_days": 3}
    response = client.patch(f"{POOLS}/ci", json=body, headers=hub["owner"])
    assert response.status_code == 200, response.text
    changed = response.json()
    assert (changed["slots"], changed["labels"], changed["projects"], changed["taints"]) == (
        4,
        ["gpu", "big"],
        ["demo", "docs"],
        ["t"],
    )
    assert changed["reap_after_days"] == 3 and changed["updated_at"] > pool["updated_at"]
    one, two = beat(client, first["token"]), beat(client, second["token"])
    assert (one["slots"], one["labels"], one["projects"]) == (4, ["fast", "gpu", "big", "pool=ci"], ["demo", "docs"])
    assert (two["slots"], two["labels"], two["projects"]) == (4, ["gpu", "big", "pool=ci"], ["demo", "docs"])
    (target,) = [t for t, _ in audit_rows(hub_db, audit.WORKER_POOL_UPDATE)]
    assert (
        target.startswith(f"pool:{pool['id']} name=ci ")
        and "slots=1->4" in target
        and "projects=[demo]->[demo,docs]" in target
    )

    # the values it has: nothing changes, nothing is audited
    assert client.patch(f"{POOLS}/ci", json={"slots": 4}, headers=hub["owner"]).status_code == 200
    assert len(audit_rows(hub_db, audit.WORKER_POOL_UPDATE)) == 1
    # a project added needs the writer role, as creating the pool does
    assert client.patch(f"{POOLS}/ci", json={"projects": ["notes"]}, headers=hub["owner"]).status_code == 403
    # a revoked node keeps what it had
    assert client.post(f"/v1/workers/{second['worker']['id']}/revoke", headers=hub["owner"]).status_code == 200
    assert client.patch(f"{POOLS}/ci", json={"slots": 2}, headers=hub["owner"]).status_code == 200
    w = tables.workers
    assert sql(hub_db, select(w.c.slots).where(w.c.id == second["worker"]["id"])) == [(4,)]
    assert beat(client, first["token"])["slots"] == 2


def test_a_join_refused_for_a_lost_writer_role_spends_no_use(client, hub, hub_db, admin):
    created(client, hub["owner"], projects=["docs"])
    token = token_of(client, hub["owner"])["token"]
    response = client.put(
        f"/v1/admin/projects/docs/grants/{OWNER}", json={"role": "reader", "max_level": "internal"}, headers=admin
    )
    assert response.status_code == 200, response.text
    assert join(client, token).status_code == 409
    jt = tables.worker_join_tokens
    assert sql(hub_db, select(jt.c.uses)) == [(0,)]


def test_joining_needs_a_code_or_a_token_and_a_pairing_code_joins_as_before(client, hub, hub_db):
    both = client.post("/v1/worker/join", json={"code": "ABCD-EFGH", "token": "evj_x", **HOST}, headers=PROTOCOL)
    neither = client.post("/v1/worker/join", json=HOST, headers=PROTOCOL)
    assert (both.status_code, neither.status_code) == (422, 422)
    answer = joined(client, paired(client, hub["owner"], labels=["mac"])["code"])
    assert (answer["worker"]["name"], answer["worker"]["labels"], answer["worker"]["pool"]) == (
        "mac-mini",
        ["mac"],
        None,
    )
    assert beat(client, answer["token"])["labels"] == ["mac"]


def test_no_log_record_holds_a_join_token(client, hub, caplog):
    caplog.set_level(logging.DEBUG)
    created(client, hub["owner"])
    issued = token_of(client, hub["owner"])
    answer = pool_joined(client, issued["token"])
    assert join(client, issued["token"]).status_code == 403
    assert {"worker pool created", "worker pool join token created", "worker joined a pool"} <= {
        record.getMessage() for record in caplog.records
    }
    raw = caplog.text + "\n".join(
        f"{record.getMessage()} {record.args!r} {vars(record)!r}" for record in caplog.records
    )
    for secret in (issued["token"], answer["token"]):
        assert secret not in raw


def test_a_join_token_in_any_log_line_is_masked():
    token = "evj_" + "Jn-_9" * 8 + "abc"
    assert scrub(f"joined with {token} now") == "joined with evj_*** now"

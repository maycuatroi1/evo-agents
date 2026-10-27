"""A run that runs as its owner (``evo_agents.hub.server.run_access``): the owner lets the run's agent call the hub's
API as them, through a token of the run's own (``evr_...``) that its worker is leased.

The checks of step 1 of the plan owner-run-access, by criterion: the token acts as the owner, with the owner's grants
on every project and hub admin when the owner is one, while a run not let keeps the leases it always got (a1, a10);
it is refused secrets, tokens and signing in, workers, the run's own decision, the owner's autonomy and letting any run
run as the owner, with 403 and why (a2); it lives while the run is held and its lease out, ends at once with the run,
the lease, the grant, the worker or the owner's stop, a resumed run gets a new one, and the audit names each grant,
stop and write, never the token (a3); and the API side of the choice: dispatch, plan run, rerun, the access route and
the run's view (a4)."""

from datetime import timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, select, update

from evo_agents.hub import runs, tables
from evo_agents.hub.credentials import HUB_KIND, HUB_TOKEN_ENV, RUN_ACCESS, Lease, env_name_refusal
from evo_agents.hub.server import run_access_rules
from tests.hub import test_credentials_api as credentials_api
from tests.hub.live import ADMIN, bearer, sql, table_dump
from tests.hub.test_credentials_api import (
    DISPATCHED,
    OTHER,
    OWNER,
    PLAN,
    PROJECT,
    STEPS,
    WRITER,
    by_name,
    env_secret,
    give_back,
    leased,
    reap,
    registration,
    sample,
    step_run,
    worker_of,
)
from tests.hub.test_runs import claim, expire, moved, report, state_of, steady

app_key, config, client, hub = (
    credentials_api.app_key,
    credentials_api.config,
    credentials_api.client,
    credentials_api.hub,
)

SECOND = "lumen-ops"  # another project, where the owner holds a grant too
HUB_URL = "https://hub.test"
QUESTION = {
    "category": "architecture",
    "question": "Which database should the dashboard read?",
    "context": "## Why\nThe plan leaves it open.",
    "options": [{"key": "sqlite", "label": "Keep SQLite"}, {"key": "postgres", "label": "Move to Postgres"}],
    "recommended": "postgres",
    "step_key": "1",
}


def hub_lease(answer: dict) -> dict | None:
    """The lease of the run's own token in an answer of the credentials route, if there is one."""
    found = [lease for lease in answer["leases"] if lease["kind"] == HUB_KIND]
    assert len(found) <= 1, found
    return found[0] if found else None


def run_token(client, worker: dict, run_id: int) -> str:
    lease = hub_lease(leased(client, worker, run_id))
    assert lease is not None
    return lease["value"]


def as_run(token: str) -> dict:
    return bearer(token)


def whoami(client, token: str):
    return client.get("/v1/auth/whoami", headers=as_run(token))


def view(client, headers, run_id: int) -> dict:
    response = client.get(f"/v1/projects/{PROJECT}/runs/{run_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def change(client, headers, run_id: int, as_me: bool):
    return client.put(f"/v1/projects/{PROJECT}/runs/{run_id}/access", json={"as_me": as_me}, headers=headers)


def audit_of(db, action: str) -> list[tuple]:
    """(target, actor, project, token kind) of each audit row of ``action``, oldest first."""
    a, u, p, t = tables.audit, tables.users, tables.projects, tables.tokens
    return sql(
        db,
        select(a.c.target, u.c.login, p.c.name, t.c.kind)
        .join_from(a, u, u.c.id == a.c.actor_id, isouter=True)
        .outerjoin(p, p.c.id == a.c.project_id)
        .outerjoin(t, t.c.id == a.c.token_id)
        .where(a.c.action == action)
        .order_by(a.c.id),
    )


def run_leases(db, run_id: int) -> list[tuple]:
    """(provider, revoked, sealed, token revoked) of each run-access lease of the run, oldest first."""
    cl, t = tables.credential_leases, tables.tokens
    return sql(
        db,
        select(cl.c.provider, cl.c.revoked_at.is_not(None), cl.c.sealed_value.is_not(None), t.c.revoked_at.is_not(None))
        .join_from(cl, t, t.c.id == cl.c.token_id)
        .where(cl.c.run_id == run_id)
        .order_by(cl.c.id),
    )


def second_project(client, hub) -> None:
    """Project SECOND, registered by the admin, where the owner reads."""
    body = registration(DISPATCHED)
    body["harness"] = {**body["harness"], "name": SECOND}
    registered = client.put(f"/v1/projects/{SECOND}", json=body, headers=hub["admin"])
    assert registered.status_code == 200, registered.text
    reader = {"role": "reader", "max_level": "internal"}
    granted = client.put(f"/v1/admin/projects/{SECOND}/grants/{OWNER}", json=reader, headers=hub["admin"])
    assert granted.status_code == 200, granted.text


def as_me_run(client, hub, worker: dict, repo: str = "notes") -> tuple[int, str]:
    """A run of the step on ``repo`` that runs as its owner, claimed by ``worker``, and its token."""
    run_id = step_run(client, hub["owner"], repo, worker, as_me=True)
    return run_id, run_token(client, worker, run_id)


# a1, a10: the token is the owner; a run not let gets what it always got


def test_a_run_dispatched_as_me_acts_as_owner_on_every_project(client, hub, hub_db):
    second_project(client, hub)
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id = step_run(client, hub["owner"], "notes", worker, as_me=True)
    answer = leased(client, worker, run_id)
    lease = hub_lease(answer)
    assert lease is not None
    assert (lease["provider"], lease["name"], lease["env_var"], lease["base_url"]) == (
        RUN_ACCESS,
        f"{RUN_ACCESS}:{OWNER}",
        HUB_TOKEN_ENV,
        HUB_URL,
    )
    assert lease["expires_at"] is None and lease["url_prefix"] is None and lease["username"] is None
    token = lease["value"]
    assert token.startswith("evr_") and len(token) == 47
    # the daemon reads it with the shared model, whose repr leaves the value out
    read = Lease.from_json(lease)
    assert read.kind == HUB_KIND and token not in repr(read)
    # a daemon that knows no kind hub puts env leases alone into the agent's environment, and never this variable
    assert env_name_refusal(read.env_var) is not None

    me = whoami(client, token)
    assert me.status_code == 200, me.text
    assert (me.json()["login"], me.json()["admin"], me.json()["token"]["kind"]) == (OWNER, False, "run")
    assert {grant["project"] for grant in me.json()["grants"]} == {PROJECT, SECOND}
    # the owner's grants on every project, not the run's alone
    for project in (PROJECT, SECOND):
        plans = client.get(f"/v1/projects/{project}/plans", headers=as_run(token))
        assert plans.status_code == 200, plans.text
    # it writes as the owner: a run of another step, dispatched by the owner through the run's token
    other = worker_of(client, hub["owner"], "desk")
    body = {"plan_id": PLAN, "steps": [STEPS["evo-agents"]], "worker_id": other["id"]}
    dispatched = client.post(f"/v1/projects/{PROJECT}/runs", json=body, headers=as_run(token))
    assert dispatched.status_code == 201, dispatched.text
    (queued,) = dispatched.json()
    assert (queued["dispatched_by"], queued["dispatched_via"], queued["as_owner"]) == (OWNER, "machine", False)
    # but never a hub admin's routes, as the owner is none
    assert client.get("/v1/admin/users", headers=as_run(token)).status_code == 403
    # asking again hands the same token back
    assert run_token(client, worker, run_id) == token


def test_a_run_of_a_hub_admin_acts_as_owner_with_the_admins_rights(client, hub, hub_db):
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{ADMIN}", json=WRITER, headers=hub["admin"])
    assert granted.status_code == 200, granted.text
    worker = worker_of(client, hub["admin"], "admin-box")
    run_id = step_run(client, hub["admin"], "notes", worker, as_me=True)
    token = run_token(client, worker, run_id)
    me = whoami(client, token).json()
    assert (me["login"], me["admin"]) == (ADMIN, True)
    users = client.get("/v1/admin/users", headers=as_run(token))
    assert users.status_code == 200, users.text


def test_a_run_not_let_acts_as_owner_gets_the_leases_it_always_did(client, hub, hub_db):
    oauth = sample("oauth-")
    env_secret(client, hub["owner"], "claude-oauth", "CLAUDE_CODE_OAUTH_TOKEN", oauth)
    worker = worker_of(client, hub["owner"], "mac-mini", slots=2)
    plain = step_run(client, hub["owner"], "notes", worker)
    answer = leased(client, worker, plain)
    assert set(by_name(answer)) == {"claude-oauth"} and hub_lease(answer) is None
    assert view(client, hub["owner"], plain)["as_owner"] is False
    assert run_leases(hub_db, plain) == []
    cl = tables.credential_leases
    assert sql(hub_db, select(func.count()).select_from(cl).where(cl.c.token_id.is_not(None))) == [(0,)]
    # the same run let afterwards: its next ask adds the lease of its token, and nothing else changes
    assert change(client, hub["owner"], plain, True).status_code == 200
    again = leased(client, worker, plain)
    assert set(by_name(again)) == {"claude-oauth", f"{RUN_ACCESS}:{OWNER}"}
    assert by_name(again)["claude-oauth"] == by_name(answer)["claude-oauth"]
    target = f"{PROJECT}/{PLAN}#4 run:{plain} secrets="
    (first, second) = audit_of(hub_db, "credential.lease")
    assert first[0].startswith(target) and "run-access" not in first[0]
    assert second[0].startswith(target) and " run-access=token:" in second[0]


# a2: what the token never reaches


REFUSED = [
    ("GET", "/v1/secrets"),
    ("PUT", "/v1/secrets/claude-oauth"),
    ("GET", "/v1/secrets/claude-oauth/shares"),
    ("DELETE", "/v1/shares/1"),
    ("GET", "/v1/accounts"),
    ("PATCH", "/v1/accounts/claude-oauth"),
    ("GET", "/v1/account-policies"),
    ("GET", "/v1/agent-accounts"),
    ("GET", "/v1/provider-accounts"),
    ("GET", f"/v1/projects/{PROJECT}/agents/reviewer/accounts"),
    ("GET", f"/v1/projects/{PROJECT}/browser-identities"),
    ("GET", f"/v1/projects/{PROJECT}/runs/{{run}}/credentials"),
    ("GET", f"/v1/projects/{PROJECT}/runs/{{run}}/accounts"),
    ("GET", "/v1/tokens"),
    ("DELETE", "/v1/tokens/{token}"),
    ("GET", "/v1/admin/tokens"),
    ("POST", "/v1/admin/invites"),
    ("POST", "/v1/invites/accept"),
    ("POST", "/v1/auth/logout"),
    ("GET", "/v1/auth/web/csrf"),
    ("GET", f"/v1/projects/{PROJECT}/signal-ingest-keys"),
    ("GET", "/v1/workers"),
    ("POST", "/v1/workers/pairings"),
    ("GET", "/v1/worker-pools"),
    ("GET", "/v1/org/workers"),
    ("POST", f"/v1/projects/{PROJECT}/runs/{{run}}/takeover"),
    ("POST", f"/v1/projects/{PROJECT}/runs/{{run}}/handback"),
    ("PUT", f"/v1/projects/{PROJECT}/runs/{{run}}/access"),
    ("PUT", "/v1/autonomy/prefs"),
    ("POST", "/v1/autonomy/stops"),
    ("POST", "/v1/autonomy/stops/lift"),
    ("PUT", "/v1/org/ceilings"),
    ("PUT", f"/v1/projects/{PROJECT}/policy"),
]


def test_a_runs_token_is_refused_secrets_tokens_workers_and_the_owners_autonomy(client, hub, hub_db):
    env_secret(client, hub["owner"], "claude-oauth", "CLAUDE_CODE_OAUTH_TOKEN", sample())
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id, token = as_me_run(client, hub, worker)
    t = tables.tokens
    (token_id,) = sql(hub_db, select(t.c.id).where(t.c.kind == "run"))[0]
    for method, path in REFUSED:
        url = path.format(run=run_id, token=token_id)
        response = client.request(method, url, json={}, headers=as_run(token))
        assert response.status_code == 403, (method, url, response.text)
        message = response.json()["message"]
        assert "a run's token (evr_...) acts as its owner but never reaches" in message, (method, url)
        assert token not in response.text
    # the worker's routes take no run's token, before anything else
    refused = client.post("/v1/worker/claim", json={}, headers={**as_run(token), "X-Evo-Worker-Protocol": "1"})
    assert refused.status_code == 403 and "not a run's token" in refused.json()["message"]
    # the token still works, and the owner still holds every secret and token refused it
    assert whoami(client, token).status_code == 200
    assert client.get("/v1/secrets", headers=hub["owner"]).status_code == 200
    # reading the owner's autonomy is not changing it
    assert client.get("/v1/autonomy/settings", headers=as_run(token)).status_code == 200
    assert client.get(f"/v1/projects/{PROJECT}/policy", headers=as_run(token)).status_code == 200


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/secrets/later/route"),  # a route added later under a group's prefix is refused with it
        ("POST", "/v1/workers/7/something-new"),
        ("GET", "/v1/auth/new-flow"),
        ("DELETE", "/v1/autonomy/anything"),
        ("GET", "/v1/projects/p/runs/9/terminal"),
        ("GET", "/v1/projects/p/runs/9/browser"),
    ],
)
def test_the_groups_refused_take_every_path_under_their_prefix(method, path):
    assert run_access_rules.refusal(method, path) is not None


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/auth/whoami"),
        ("GET", "/v1/autonomy/settings"),
        ("GET", "/v1/projects/p/policy"),
        ("POST", "/v1/projects/p/runs"),
        ("GET", "/v1/projects/p/runs/9"),
        ("GET", "/v1/workersx"),  # a prefix is a path segment, not a string's start
        ("GET", "/v1/secretsfoo"),
        ("POST", "/v1/admin/projects/p/grants/alice"),
    ],
)
def test_paths_outside_the_groups_are_not_refused(method, path):
    assert run_access_rules.refusal(method, path) is None


def test_a_runs_token_is_refused_answering_its_own_runs_decision(client, hub, hub_db):
    worker = worker_of(client, hub["owner"], "mac-mini", slots=2)
    response = client.post(
        f"/v1/projects/{PROJECT}/plan-runs",
        json={"plan_id": PLAN, "worker_id": worker["id"], "as_me": True},
        headers=hub["owner"],
    )
    assert response.status_code == 201, response.text
    run_id = claim(client, worker)["id"]
    token = run_token(client, worker, run_id)
    moved(client, worker, run_id, "running")
    asked = client.post(f"/v1/worker/runs/{run_id}/decisions", json=QUESTION, headers=worker["headers"])
    assert asked.status_code == 201, asked.text
    decision_id = asked.json()["id"]
    answer = {"option": "postgres"}
    url = f"/v1/projects/{PROJECT}/decisions/{decision_id}/answer"
    refused = client.post(url, json=answer, headers=as_run(token))
    assert refused.status_code == 403, refused.text
    assert refused.json()["message"] == (
        f"decision {decision_id} is a question of run {run_id} to its owner, and that run's token never answers it: "
        "the owner answers with their own sign-in"
    )
    # it reads the decision as the owner does, and the owner answers it
    assert client.get(f"/v1/projects/{PROJECT}/decisions/{decision_id}", headers=as_run(token)).status_code == 200
    answered = client.post(url, json=answer, headers=hub["owner"])
    assert answered.status_code == 200 and answered.json()["state"] == "answered"


def test_a_runs_token_is_refused_letting_any_run_run_as_owner(client, hub, hub_db):
    worker = worker_of(client, hub["owner"], "mac-mini", slots=3)
    run_id, token = as_me_run(client, hub, worker)
    onward = (
        "a run's token (evr_...) never lets a run run as its owner, so no run passes the owner's account on: ask with "
        "as_me false, or let the owner choose with their own sign-in"
    )
    body = {"plan_id": PLAN, "steps": [STEPS["evo-agents"]], "worker_id": worker["id"], "as_me": True}
    for path, sent in (("runs", body), ("plan-runs", {"plan_id": PLAN, "worker_id": worker["id"], "as_me": True})):
        refused = client.post(f"/v1/projects/{PROJECT}/{path}", json=sent, headers=as_run(token))
        assert refused.status_code == 403, refused.text
        assert refused.json()["message"] == onward
    # a run of the owner's that ran as them: its rerun keeps that unless told otherwise, which the token must say
    ended = step_run(client, hub["owner"], "lumen-kb-docs", worker, as_me=True)
    assert report(client, worker, ended, "failed", error="the agent gave up").status_code == 200
    rerun = f"/v1/projects/{PROJECT}/runs/{ended}/rerun"
    refused = client.post(rerun, headers=as_run(token))
    assert refused.status_code == 403 and refused.json()["message"] == onward
    plain = client.post(rerun, json={"as_me": False}, headers=as_run(token))
    assert plain.status_code == 201, plain.text
    assert plain.json()["as_owner"] is False
    assert state_of(hub_db, run_id) == "leased"
    assert audit_of(hub_db, "run_access.grant") == [
        (f"{PROJECT}/{PLAN}#4 run:{run_id} by=dispatch", OWNER, PROJECT, "machine"),
        (f"{PROJECT}/{PLAN}#2 run:{ended} by=dispatch", OWNER, PROJECT, "machine"),
    ]


# a3: the life of the token, and the audit


def test_lifecycle_the_token_lives_while_the_run_is_held_and_ends_with_it(client, hub, hub_db):
    worker = worker_of(client, hub["owner"], "mac-mini", slots=3)
    run_id, token = as_me_run(client, hub, worker)
    moved(client, worker, run_id, "running")
    assert whoami(client, token).status_code == 200
    assert report(client, worker, run_id, "failed", error="the agent gave up").status_code == 200
    gone = whoami(client, token)
    assert gone.status_code == 401 and gone.json()["message"] == (
        "the run's token is revoked, expired or unknown: run `evo-agents hub login`"
    )
    assert run_leases(hub_db, run_id) == [(RUN_ACCESS, True, False, True)]

    # the worker gives back its leases: the token ends; asking again makes a new one
    second, token = as_me_run(client, hub, worker, "evo-agents")
    assert give_back(client, worker["headers"], second).status_code == 200
    assert whoami(client, token).status_code == 401
    fresh = run_token(client, worker, second)
    assert fresh != token and whoami(client, fresh).status_code == 200

    # the owner stops it: at once, and the run's next ask gets no token
    stopped = change(client, hub["owner"], second, False)
    assert stopped.status_code == 200 and stopped.json()["as_owner"] is False
    assert whoami(client, fresh).status_code == 401
    assert hub_lease(leased(client, worker, second)) is None

    # the owner revokes the token itself: the run stops running as them
    third, token = as_me_run(client, hub, worker, "lumen-kb-docs")
    t = tables.tokens
    (token_id,) = sql(hub_db, select(func.max(t.c.id)).where(t.c.kind == "run"))[0]
    listed = client.get("/v1/tokens", headers=hub["owner"]).json()
    assert {item["id"]: item["kind"] for item in listed}[token_id] == "run"
    assert client.delete(f"/v1/tokens/{token_id}", headers=hub["owner"]).status_code == 204
    assert whoami(client, token).status_code == 401
    assert view(client, hub["owner"], third)["as_owner"] is False
    assert hub_lease(leased(client, worker, third)) is None


def test_lifecycle_the_token_ends_with_the_owners_writer_grant_and_with_the_worker(client, hub, hub_db):
    worker = worker_of(client, hub["owner"], "mac-mini", slots=2)
    run_id, token = as_me_run(client, hub, worker)
    grant = f"/v1/admin/projects/{PROJECT}/grants/{OWNER}"
    assert client.put(grant, json={**WRITER, "role": "reader"}, headers=hub["admin"]).status_code == 200
    assert whoami(client, token).status_code == 401
    assert run_leases(hub_db, run_id) == [(RUN_ACCESS, True, False, True)]
    assert audit_of(hub_db, "credential.revoke")[-1][0].endswith(" run-access=1 by=grant-reader")

    assert client.put(grant, json=WRITER, headers=hub["admin"]).status_code == 200
    token = run_token(client, worker, run_id)
    assert whoami(client, token).status_code == 200
    revoked = client.post(f"/v1/workers/{worker['id']}/revoke", headers=hub["owner"])
    assert revoked.status_code == 200, revoked.text
    assert whoami(client, token).status_code == 401
    assert all(row[1] and row[3] for row in run_leases(hub_db, run_id))


def test_lifecycle_the_reaper_ends_the_token_and_the_next_attempt_keeps_the_choice(client, hub, hub_db):
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id, token = as_me_run(client, hub, worker)
    expire(hub_db, run_id)
    assert reap(client)["lost"] == 1 and state_of(hub_db, run_id) == "lost"
    assert whoami(client, token).status_code == 401
    steady(hub_db, worker)
    retry = claim(client, worker)
    assert (retry["attempt"], view(client, hub["owner"], retry["id"])["as_owner"]) == (2, True)
    fresh = run_token(client, worker, retry["id"])
    assert fresh != token and whoami(client, fresh).status_code == 200


def test_lifecycle_a_parked_run_loses_its_token_and_the_run_that_resumes_it_gets_a_new_one(client, hub, hub_db):
    worker = worker_of(client, hub["owner"], "mac-mini")
    body = {"plan_id": PLAN, "worker_id": worker["id"], "as_me": True}
    assert client.post(f"/v1/projects/{PROJECT}/plan-runs", json=body, headers=hub["owner"]).status_code == 201
    run_id = claim(client, worker)["id"]
    token = run_token(client, worker, run_id)
    moved(client, worker, run_id, "running", session_id="0199a3c1-0000-7000-8000-00000000000c")
    asked = client.post(f"/v1/worker/runs/{run_id}/decisions", json=QUESTION, headers=worker["headers"])
    assert asked.status_code == 201, asked.text
    moved(client, worker, run_id, "waiting")
    assert whoami(client, token).status_code == 200, "a run waiting for its owner is held, and keeps its token"
    r = tables.runs
    waited = func.now() - timedelta(seconds=runs.DECISION_WAIT_SECONDS + 1)
    sql(hub_db, update(r).values(waiting_since=waited).where(r.c.id == run_id))
    assert reap(client)["parked"] == 1 and state_of(hub_db, run_id) == "parked"
    assert whoami(client, token).status_code == 401

    url = f"/v1/projects/{PROJECT}/decisions/{asked.json()['id']}/answer"
    assert client.post(url, json={"option": "postgres"}, headers=hub["owner"]).status_code == 200
    resumed = claim(client, worker)
    assert (resumed["resume_of_run_id"], view(client, hub["owner"], resumed["id"])["as_owner"]) == (run_id, True)
    fresh = run_token(client, worker, resumed["id"])
    assert fresh != token and whoami(client, fresh).status_code == 200


def test_audit_names_each_grant_stop_and_write_of_a_runs_token_and_never_its_value(client, hub, hub_db, caplog):
    caplog.set_level("INFO")
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id, token = as_me_run(client, hub, worker)
    t = tables.tokens
    (token_id,) = sql(hub_db, select(t.c.id).where(t.c.kind == "run"))[0]
    run = f"{PROJECT}/{PLAN}#4 run:{run_id}"
    # a write that passes, one the route refuses, and one the middleware refuses
    message = client.post(f"/v1/projects/{PROJECT}/runs/{run_id}/messages", json={"text": "hi"}, headers=as_run(token))
    assert message.status_code in (200, 201), message.text
    assert client.post(f"/v1/projects/{PROJECT}/runs/{run_id}/approve", headers=as_run(token)).status_code == 409
    assert client.put("/v1/secrets/stolen", json={}, headers=as_run(token)).status_code == 403
    # reads are not audited
    assert client.get(f"/v1/projects/{PROJECT}/runs/{run_id}", headers=as_run(token)).status_code == 200
    assert change(client, hub["owner"], run_id, False).status_code == 200

    writes = audit_of(hub_db, "run_access.write")
    assert writes == [
        (f"{run} POST /v1/projects/{PROJECT}/runs/{run_id}/messages {message.status_code}", OWNER, PROJECT, "run"),
        (f"{run} POST /v1/projects/{PROJECT}/runs/{run_id}/approve 409", OWNER, PROJECT, "run"),
        (f"{run} PUT /v1/secrets/stolen 403", OWNER, PROJECT, "run"),
    ]
    assert audit_of(hub_db, "run_access.grant") == [(f"{run} by=dispatch", OWNER, PROJECT, "machine")]
    assert audit_of(hub_db, "run_access.revoke") == [(f"{run} by=access tokens=1", OWNER, PROJECT, "machine")]
    (lease_row,) = audit_of(hub_db, "credential.lease")
    assert lease_row[0].endswith(f" run-access=token:{token_id}")
    # the token is in no table but its hash and its sealed lease (dropped once the lease ended), no event, no log
    assert token not in table_dump(hub_db)
    logged = caplog.text + "\n".join(f"{record.getMessage()} {vars(record)!r}" for record in caplog.records)
    assert token not in logged


# a4, the API side: dispatch, plan run, rerun, the access route and the view


def test_as_me_on_dispatch_plan_run_and_rerun_and_the_view_says_it(client, hub, hub_db):
    worker = worker_of(client, hub["owner"], "mac-mini", slots=3)
    run_id = step_run(client, hub["owner"], "notes", worker, as_me=True)
    assert view(client, hub["owner"], run_id)["as_owner"] is True
    assert view(client, hub["other"], run_id)["as_owner"] is True, "a reader of the run sees it too"
    assert report(client, worker, run_id, "failed", error="the agent gave up").status_code == 200
    rerun = f"/v1/projects/{PROJECT}/runs/{run_id}/rerun"
    kept = client.post(rerun, headers=hub["owner"])
    assert kept.status_code == 201 and kept.json()["as_owner"] is True
    assert audit_of(hub_db, "run_access.grant")[-1][0] == (
        f"{PROJECT}/{PLAN}#4 run:{kept.json()['id']} by=rerun of run:{run_id}"
    )
    assert (
        client.post(f"/v1/projects/{PROJECT}/runs/{kept.json()['id']}/cancel", headers=hub["owner"]).status_code == 200
    )
    dropped = client.post(rerun, json={"as_me": False}, headers=hub["owner"])
    assert dropped.status_code == 201 and dropped.json()["as_owner"] is False
    assert (
        client.post(f"/v1/projects/{PROJECT}/runs/{dropped.json()['id']}/cancel", headers=hub["owner"]).status_code
        == 200
    )

    plan = client.post(f"/v1/projects/{PROJECT}/plan-runs", json={"plan_id": PLAN, "as_me": True}, headers=hub["owner"])
    assert plan.status_code == 201 and plan.json()["as_owner"] is True
    plain = client.post(f"/v1/projects/{PROJECT}/plan-runs", json={"plan_id": PLAN}, headers=hub["owner"])
    assert plain.status_code == 409  # one plan run at a time: the first one is still queued
    listed = client.get(f"/v1/projects/{PROJECT}/runs", headers=hub["owner"])
    assert listed.status_code == 200
    assert {run["id"]: run["as_owner"] for run in listed.json()["runs"]}[plan.json()["id"]] is True


def test_access_only_the_owner_changes_it_and_only_on_a_run_that_has_not_ended(client, hub, hub_db):
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id = step_run(client, hub["owner"], "notes", worker)
    refused = change(client, hub["other"], run_id, True)
    assert refused.status_code == 403, refused.text
    assert refused.json()["message"] == f"only {OWNER}, who dispatched run {run_id}, may change the access of it"
    assert change(client, hub["admin"], run_id, True).status_code == 403, "a hub admin is not the run's owner"
    granted = change(client, hub["owner"], run_id, True)
    assert granted.status_code == 200 and granted.json()["as_owner"] is True
    assert change(client, hub["owner"], run_id, True).status_code == 200  # asked again: nothing changes
    assert len(audit_of(hub_db, "run_access.grant")) == 1
    assert audit_of(hub_db, "run_access.grant") == [
        (f"{PROJECT}/{PLAN}#4 run:{run_id} by=access", OWNER, PROJECT, "machine")
    ]
    assert report(client, worker, run_id, "failed", error="the agent gave up").status_code == 200
    late = change(client, hub["owner"], run_id, True)
    assert late.status_code == 200, "it runs as the owner already: nothing to change"
    assert change(client, hub["owner"], run_id, False).status_code == 200
    final = change(client, hub["owner"], run_id, True)
    assert final.status_code == 409 and final.json()["message"] == (
        f"run {run_id} is failed, which is final: it has no agent to run as you"
    )
    assert (
        client.put(
            f"/v1/projects/{PROJECT}/runs/{run_id}/access", json={"as_me": True, "more": 1}, headers=hub["owner"]
        ).status_code
        == 422
    )
    assert (
        client.put(f"/v1/projects/{PROJECT}/runs/999999/access", json={"as_me": True}, headers=hub["owner"]).status_code
        == 404
    )
    assert OTHER  # the other member sees the run but may not change it

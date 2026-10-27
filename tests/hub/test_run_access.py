"""A run that runs as its owner (``evo_agents.hub.server.run_access``): the owner lets the run's agent call the hub's
API as them, through a token of the run's own (``evr_...``) that its worker is leased.

The checks of step 1 of the plan owner-run-access, by criterion: the token acts as the owner, with the owner's grants
on every project and hub admin when the owner is one, while a run not let keeps the leases it always got (a1, a10);
it is refused secrets, tokens and signing in, workers, the run's own decision, the owner's autonomy and letting any run
run as the owner, with 403 and why (a2); it lives while the run is held and its lease out, ends at once with the run,
the lease, the grant, the worker or the owner's stop, a resumed run gets a new one, and the audit names each grant,
stop and write, never the token (a3); and the API side of the choice: dispatch, plan run, rerun, the access route and
the run's view (a4).

Step 2 adds the worker and the command line: what the daemon puts into the agent's environment from the lease
(``worker.credentials.run_access_env``), with which ``evo-agents hub ...`` acts as the owner on a worker with no login
(a1), and ``hub run dispatch|plan --as-me`` and ``hub run access`` (a4). The command line runs in this process, its
requests sent to the in-process hub (``through``)."""

import asyncio
import io
import json
import os
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi import HTTPException
from psycopg import errors
from sqlalchemy import func, select, update

from evo_agents.cli import main
from evo_agents.core_plugins.builder import errors as builder_errors
from evo_agents.core_plugins.builder import proposals as builder_proposals
from evo_agents.core_plugins.builder import server as builder_server
from evo_agents.hub import client as hub_client
from evo_agents.hub import credential_requests, runs, tables
from evo_agents.hub.credentials import (
    HUB_KIND,
    HUB_TOKEN_ENV,
    HUB_URL_ENV,
    MAX_SECRET_BYTES,
    RUN_ACCESS,
    Lease,
    env_name_refusal,
)
from evo_agents.hub.server import (
    autonomy_policies,
    autonomy_stops,
    autonomy_store,
    chat_events,
    core_plugin_api,
    goals,
    invites,
    opportunities,
    post_merge,
    probes,
    problems,
    route_autonomy,
    run_access,
    run_access_rules,
    run_events,
    task_dispatch,
    worker_shares,
)
from evo_agents.hub.server import org as org_routes
from evo_agents.hub.server.canvas import content as canvas_content
from evo_agents.hub.server.canvas import server as canvas_server
from evo_agents.hub.server.loop import breaker as loop_breaker
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.runs.service import dispatch
from evo_agents.hub.server.security import RUN, Principal
from evo_agents.hub.server.srs import doc as srs_doc
from evo_agents.hub.server.srs import reviews as srs_reviews
from evo_agents.worker.credentials import run_access_env
from tests.hub import test_credentials_api as credentials_api
from tests.hub.live import ADMIN, bearer, sql, table_dump
from tests.hub.test_chat_runs import ADMIN_GRANT, chat_worker
from tests.hub.test_chats import OPEN_SINKS
from tests.hub.test_credentials_api import (
    DISPATCHED,
    GITLAB_KB,
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
from tests.hub.test_opportunities import DEPOT as OPPORTUNITY
from tests.hub.test_problems import NIGHT as PROBLEM
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


GOAL = {"statement": "Fewer searches find nothing"}
NOTE = {
    "scope": "project",
    "project": PROJECT,
    "location": "harness",
    "name": "roadmap.md",
    "type": "project",
    "body": "---\nname: Roadmap\n---\nthe roadmap\n",
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
    # it writes as the owner: a memory of the project, under the owner's name
    written = client.put("/v1/memories", params={"sink": "hub"}, json=NOTE, headers=as_run(token))
    assert written.status_code == 200, written.text
    m = tables.memories
    assert sql(hub_db, select(m.c.updated_by).where(m.c.id == written.json()["id"])) == sql(
        hub_db, select(tables.users.c.id).where(tables.users.c.login == OWNER)
    )
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
    # the fix round: grants and users, the org chart and agent profiles, runs and their messages, decisions and
    # approvals, the Curator
    ("PUT", f"/v1/admin/projects/{PROJECT}/grants/{OTHER}"),
    ("DELETE", f"/v1/admin/projects/{PROJECT}/grants/{OTHER}"),
    ("DELETE", f"/v1/admin/users/{OTHER}/telegram"),
    ("POST", "/v1/org/seats"),
    ("PATCH", "/v1/org/seats/1"),
    ("POST", "/v1/org/units"),
    ("PATCH", "/v1/org/units/ops"),
    ("POST", f"/v1/projects/{PROJECT}/agents"),
    ("PATCH", f"/v1/projects/{PROJECT}/agents/reviewer"),
    ("DELETE", f"/v1/projects/{PROJECT}/agents/reviewer"),
    ("PUT", f"/v1/projects/{PROJECT}/agent-types/builder/builder"),
    ("POST", f"/v1/projects/{PROJECT}/runs"),
    ("POST", f"/v1/projects/{PROJECT}/plan-runs"),
    ("POST", f"/v1/projects/{PROJECT}/author-runs"),
    ("POST", f"/v1/projects/{PROJECT}/runs/{{run}}/rerun"),
    ("POST", f"/v1/projects/{PROJECT}/runs/{{run}}/messages"),
    ("POST", f"/v1/projects/{PROJECT}/local-runs/{{run}}/inbox"),
    ("POST", f"/v1/projects/{PROJECT}/releases/run"),
    ("PUT", f"/v1/projects/{PROJECT}/releases/schedule"),
    ("POST", f"/v1/x/editorial/{PROJECT}/posts/launch/run"),
    ("POST", f"/v1/projects/{PROJECT}/decisions/1/answer"),
    ("POST", f"/v1/projects/{PROJECT}/local-runs/{{run}}/decisions/1/answered"),
    ("POST", f"/v1/projects/{PROJECT}/runs/{{run}}/approve"),
    ("POST", f"/v1/projects/{PROJECT}/merge-queue/1/pass"),
    ("POST", f"/v1/projects/{PROJECT}/merge-queue/1/release"),
    ("POST", f"/v1/projects/{PROJECT}/merge-queue/repos/notes/resume"),
    ("POST", f"/v1/projects/{PROJECT}/loop/resume"),
    ("POST", f"/v1/x/builder/{PROJECT}/proposals/1/answer"),
    ("PUT", f"/v1/projects/{PROJECT}/curator/charter"),
    ("POST", f"/v1/projects/{PROJECT}/curator/plans/{PLAN}/build"),
    ("POST", f"/v1/projects/{PROJECT}/curator/proposals/1/answer"),
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
        ("POST", "/v1/projects/p/plan-runs/later"),
        ("POST", "/v1/projects/p/curator/new-route"),
        ("PUT", "/v1/admin/projects/p/grants/alice"),
        ("POST", "/v1/org/anything-new"),
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
        ("GET", "/v1/projects/p/runs"),  # listing runs is not starting one
        ("GET", "/v1/projects/p/runs/9"),
        ("POST", "/v1/projects/p/runs/9/cancel"),
        ("GET", "/v1/projects/p/decisions/3"),
        ("GET", "/v1/org"),
        ("GET", "/v1/projects/p/agents"),
        ("GET", "/v1/workersx"),  # a prefix is a path segment, not a string's start
        ("GET", "/v1/secretsfoo"),
        ("POST", "/v1/projects/p/runsx"),
        # the owner's ordinary work in a deploy run of northwind: a chat's label, Telegram's webhook, the project's sinks
        ("PUT", "/v1/chats/3/label"),
        ("POST", "/v1/admin/telegram/webhook"),
        ("PUT", "/v1/projects/p"),
        ("PUT", "/v1/projects/p/plans/rollout"),
        ("PUT", "/v1/memories"),
        ("POST", "/v1/chats/3/messages"),
    ],
)
def test_paths_outside_the_groups_are_not_refused(method, path):
    assert run_access_rules.refusal(method, path) is None


def asked_decision(client, hub, worker: dict) -> tuple[int, str, int]:
    """A plan run that runs as its owner, claimed and running on ``worker``, that asked QUESTION: (its id, its token,
    the decision's id)."""
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
    return run_id, token, asked.json()["id"]


def test_a_runs_token_is_refused_answering_any_decision_its_own_or_another_runs(client, hub, hub_db):
    worker = worker_of(client, hub["owner"], "mac-mini", slots=3)
    # another run of the owner's that runs as them: a step of another plan
    later = {**credentials_api.plan_body(), "id": "credentials-later"}
    pushed = client.put(f"/v1/projects/{PROJECT}/plans/credentials-later", json={"body": later}, headers=hub["owner"])
    assert pushed.status_code == 200, pushed.text
    body = {"plan_id": "credentials-later", "steps": [STEPS["notes"]], "worker_id": worker["id"], "as_me": True}
    made = client.post(f"/v1/projects/{PROJECT}/runs", json=body, headers=hub["owner"])
    assert made.status_code == 201, made.text
    other_id = claim(client, worker)["id"]
    other_token = run_token(client, worker, other_id)
    run_id, token, decision_id = asked_decision(client, hub, worker)
    answer = {"option": "postgres"}
    url = f"/v1/projects/{PROJECT}/decisions/{decision_id}/answer"
    for used in (token, other_token):
        refused = client.post(url, json=answer, headers=as_run(used))
        assert refused.status_code == 403, refused.text
        assert refused.json()["message"] == run_access_rules.refusal("POST", url)
        assert "(group decisions)" in refused.json()["message"]
    d = tables.decisions
    assert sql(hub_db, select(d.c.state).where(d.c.id == decision_id)) == [("open",)]
    # it reads the decision as the owner does, and the owner answers it
    assert client.get(f"/v1/projects/{PROJECT}/decisions/{decision_id}", headers=as_run(token)).status_code == 200
    answered = client.post(url, json=answer, headers=hub["owner"])
    assert answered.status_code == 200 and answered.json()["state"] == "answered"
    assert run_id != other_id


START_RUN = "(group runs)"


def test_a_runs_token_is_refused_starting_any_run_with_or_without_the_owners_account(client, hub, hub_db):
    worker = worker_of(client, hub["owner"], "mac-mini", slots=3)
    run_id, token = as_me_run(client, hub, worker)
    ended = step_run(client, hub["owner"], "lumen-kb-docs", worker, as_me=True)
    assert report(client, worker, ended, "failed", error="the agent gave up").status_code == 200
    r = tables.runs
    before = sql(hub_db, select(func.count()).select_from(r))
    step = {"plan_id": PLAN, "steps": [STEPS["evo-agents"]], "worker_id": worker["id"]}
    plan = {"plan_id": PLAN, "worker_id": worker["id"]}
    author = {"request": "Plan the dashboard's move to Postgres", "worker_id": worker["id"]}
    for as_me in ({}, {"as_me": False}, {"as_me": True}):
        for path, body in (("runs", step), ("plan-runs", plan), (f"runs/{ended}/rerun", {}), ("author-runs", author)):
            sent = {**body, **as_me} if path != "author-runs" else body
            refused = client.post(f"/v1/projects/{PROJECT}/{path}", json=sent, headers=as_run(token))
            assert refused.status_code == 403, (path, refused.text)
            assert START_RUN in refused.json()["message"], path
    assert sql(hub_db, select(func.count()).select_from(r)) == before, "no run was queued"
    assert state_of(hub_db, run_id) == "leased"
    assert audit_of(hub_db, "run_access.grant") == [
        (f"{PROJECT}/{PLAN}#4 run:{run_id} by=dispatch", OWNER, PROJECT, "machine"),
        (f"{PROJECT}/{PLAN}#2 run:{ended} by=dispatch", OWNER, PROJECT, "machine"),
    ]
    # the owner's own sign-in still dispatches, and a dispatch's as_me from a run's token is refused by its route too
    assert client.post(f"/v1/projects/{PROJECT}/runs/{ended}/rerun", headers=hub["owner"]).status_code == 201
    with pytest.raises(HTTPException) as onward:
        run_access.refuse_onward(run_principal(hub_db, run_id), True)
    assert onward.value.status_code == 403 and onward.value.detail == run_access.ONWARD


# The fix round of step 1: the rules of the form "the agent of a run never does X" take a run's token too


def run_principal(db, run_id: int) -> Principal:
    """The principal the live token of run ``run_id`` is, as ``security.authenticate`` makes it."""
    t, cl = tables.tokens, tables.credential_leases
    ((token_id, user_id),) = sql(
        db,
        select(t.c.id, t.c.user_id)
        .join_from(cl, t, t.c.id == cl.c.token_id)
        .where(cl.c.run_id == run_id, cl.c.revoked_at.is_(None)),
    )
    return Principal(user_id, OWNER, False, token_id, RUN, "0" * 64, run_id=run_id)


def owner_admin(client, hub) -> None:
    """Owner becomes an admin of the project, which lets everyone's messages out (a chat may hold its agents), with
    the agent @reviewer."""
    body = {**registration(DISPATCHED), "sinks": OPEN_SINKS}
    assert client.put(f"/v1/projects/{PROJECT}", json=body, headers=hub["admin"]).status_code == 200
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=hub["admin"])
    assert granted.status_code == 200, granted.text
    made = client.post(
        f"/v1/projects/{PROJECT}/agents", json={"handle": "reviewer", "name": "Reviewer"}, headers=hub["owner"]
    )
    assert made.status_code == 201, made.text


def test_a_runs_token_is_refused_writing_goals_opportunities_and_problems_as_the_agent_of_a_run(client, hub, hub_db):
    owner_admin(client, hub)
    worker = worker_of(client, hub["owner"], "mac-mini")
    _, token = as_me_run(client, hub, worker)
    writes = (
        ("goals/G-1", GOAL, "the agent of a run never writes a goal"),
        ("opportunities/O-1", OPPORTUNITY, "the agent of a run never writes an opportunity"),
        ("problems/P-1", PROBLEM, "the agent of a run never writes a problem"),
    )
    for path, body, why in writes:
        url = f"/v1/projects/{PROJECT}/{path}"
        refused = client.put(url, json=body, headers=as_run(token))
        assert refused.status_code == 403, refused.text
        assert refused.json()["message"].startswith(why), refused.text
        assert client.put(url, json=body, headers=hub["owner"]).status_code == 200, "the owner's own sign-in writes it"


def test_a_runs_token_is_refused_the_browser_session_of_a_marketing_channel(client, hub, hub_db):
    owner_admin(client, hub)
    body = {"permissions": []}
    enabled = client.post(f"/v1/projects/{PROJECT}/plugins/marketing/enable", json=body, headers=hub["owner"])
    assert enabled.status_code == 200, enabled.text
    worker = worker_of(client, hub["owner"], "mac-mini")
    _, token = as_me_run(client, hub, worker)
    url = f"/v1/x/marketing/{PROJECT}/browser/claim"
    refused = client.post(url, headers=as_run(token))
    assert refused.status_code == 403, refused.text
    assert "claiming a post for the browser is not for an agent" in refused.text
    assert client.post(url, headers=hub["owner"]).status_code == 200, "the owner's own sign-in claims it"


GUARDED = (
    ("goals", lambda user, access: goals.refusal(user, access)),
    ("opportunities", lambda user, access: opportunities.refusal(user, access)),
    ("problems", lambda user, access: problems.refusal(user, access)),
    ("policy", lambda user, access: autonomy_policies.refusal(user, access)),
    ("builder", lambda user, access: builder_proposals.refusal(user, access, "skill", "approving a proposal")),
    ("probe verify", lambda user, access: None if probes.verify_shown(access, user) else "hidden"),
)
RAISING = (
    ("builder answer", lambda user, access: builder_server.answer_as(None, user, PROJECT, 1, "approve", "0")),
    ("srs review", lambda user, access: srs_reviews._may_review(None, access, user, None, None, None)),
    ("canvas decide", lambda user, access: canvas_server._no_run(user, "decides an item of a baseline")),
    ("merge queue", lambda user, access: post_merge.admin_only(user, access)),
    ("loop breaker", lambda user, access: loop_breaker.resume(None, user, access)),
    ("org chart", lambda user, access: org_routes.actor(user)),
    ("autonomy preference", lambda user, access: autonomy_store.write_pref(None, user, None)),
    ("autonomy stop", lambda user, access: autonomy_stops._subject(None, None, user)),
    ("worker shares", lambda user, access: worker_shares._writer(user)),
)


def _outcome(call, user, access):
    """What ``call`` does for ``user``: None when it lets them, else (status, message)."""
    try:
        made = call(user, access)
        if asyncio.iscoroutine(made):
            made = asyncio.run(made)
    except (HTTPException, srs_doc.SrsError, canvas_content.CanvasError, builder_errors.ProposalError) as exc:
        status = getattr(exc, "status_code", None) or exc.status
        return status, str(getattr(exc, "detail", None) or exc)
    return made


def test_a_runs_token_is_refused_each_rule_the_agent_of_a_run_keeps_on_the_rest_api(client, hub, hub_db):
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id, _ = as_me_run(client, hub, worker)
    run = run_principal(hub_db, run_id)
    member = replace(run, kind="machine", run_id=None)
    assert (run.acts_for_run, member.acts_for_run) == (run_id, None)
    admin = ProjectAccess(1, SimpleNamespace(name=PROJECT), "admin", "internal")
    for name, call in GUARDED:
        assert call(run, admin) is not None, name
        assert call(member, admin) is None, name
    for name, call in RAISING:
        refused = _outcome(call, run, admin)
        assert refused is not None and refused[0] == 403, (name, refused)
        assert "agent" in refused[1] or "admin role" in refused[1], (name, refused)
    # the core plugins' own test (marketing's _no_agent, say) reads the run of the session
    session = core_plugin_api.caller(run)
    assert core_plugin_api.session_run(session) == run_id
    assert core_plugin_api.session_run(core_plugin_api.caller(member)) is None
    # an invite's right to register a project is not the run's to use
    assert asyncio.run(invites.register_right(None, run)) is None


def test_a_runs_token_is_refused_by_the_autonomy_of_its_run_on_the_routes_of_rules(client, hub, hub_db):
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id, _ = as_me_run(client, hub, worker)
    run = run_principal(hub_db, run_id)
    request = SimpleNamespace(
        method="PUT",
        scope={"route": SimpleNamespace(path="/v1/rules/{name}")},
        url=SimpleNamespace(path="/v1/rules/x"),
        app=client.app,
    )

    async def held():
        try:
            await route_autonomy.held(request, run, "K12")
        except HTTPException as exc:
            return exc.status_code
        return None

    status = client.portal.call(held)
    (decided,) = [
        row[0]
        for row in audit_of(hub_db, "autonomy.decided")
        if row[0].startswith(f"run:{run_id} ") and row[0].endswith(" PUT /v1/rules/{name}")
    ]
    assert f" principal={OWNER} " in decided and " K12 mode=" in decided
    assert status == (403 if " result=refused " in decided else None)


def test_a_runs_token_is_refused_handing_an_agent_work_in_a_chat_and_its_message_stays(client, hub, hub_db):
    owner_admin(client, hub)
    worker = worker_of(client, hub["owner"], "mac-mini")
    _, token = as_me_run(client, hub, worker)
    made = client.post(
        "/v1/chats", json={"title": "Release talk", "people": [], "agents": ["reviewer"]}, headers=as_run(token)
    )
    assert made.status_code == 201, made.text
    chat_id = made.json()["id"]
    r = tables.runs
    before = sql(hub_db, select(func.count()).select_from(r))
    sent = client.post(f"/v1/chats/{chat_id}/messages", json={"body": "@reviewer read the diff"}, headers=as_run(token))
    assert sent.status_code == 201, sent.text
    assert sql(hub_db, select(func.count()).select_from(r)) == before, "no chat run, no inbox of one"
    listed = client.get(f"/v1/chats/{chat_id}/messages", params={"limit": 50}, headers=hub["owner"])
    bodies = [message["body"] for message in listed.json()["messages"]]
    assert "@reviewer read the diff" in bodies
    assert any(run_access_rules.NEW_RUN in body for body in bodies), bodies


def test_a_runs_token_is_refused_any_new_run_whatever_route_would_queue_it():
    assert run_access_rules.new_run_refusal() is None
    run_access_rules.refuse_new_run()  # nothing while no run's token is being answered
    acting = run_access_rules.ACTING_RUN.set(7)
    try:
        assert run_access_rules.new_run_refusal() == run_access_rules.NEW_RUN
        with pytest.raises(HTTPException) as refused:
            run_access_rules.refuse_new_run()
        assert refused.value.status_code == 403 and refused.value.detail == run_access_rules.NEW_RUN
        made = asyncio.run(task_dispatch.hand_own_task(None, "pm", 1, "pm", "a task"))
        assert made == (None, run_access_rules.NEW_RUN)
        with pytest.raises(HTTPException):
            asyncio.run(dispatch.insert_run(None, {"project_id": 1}))
    finally:
        run_access_rules.ACTING_RUN.reset(acting)


def test_lifecycle_the_triage_rerun_of_a_run_that_ran_as_its_owner_does_not(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "mac-mini", slots=4)
    run_id = step_run(client, hub["owner"], "lumen-kb-docs", worker, as_me=True)
    failed = report(client, worker, run_id, "failed", error="exec: ruff: not found", failure_cause="missing_tool")
    assert failed.status_code == 200, failed.text
    agent = triage_agent_of(client, worker, run_id, hub_db)
    done = client.post(
        f"/v1/worker/runs/{agent}/triage/rerun",
        json={"reason": "the program is installed now"},
        headers=worker["headers"],
    )
    assert done.status_code == 201, done.text
    new_id = done.json()["run_id"]
    r = tables.runs
    assert sql(hub_db, select(r.c.parent_run_id, r.c.as_owner).where(r.c.id == new_id)) == [(run_id, False)]
    assert audit_of(hub_db, "run_access.grant") == [
        (f"{PROJECT}/{PLAN}#2 run:{run_id} by=dispatch", OWNER, PROJECT, "machine")
    ]


def triage_agent_of(client, worker, run_id: int, hub_db) -> int:
    """The chat run of the triage of ``run_id``, claimed and running on ``worker``."""
    t = tables.run_triage
    ((agent_run,),) = sql(hub_db, select(t.c.agent_run_id).where(t.c.run_id == run_id))
    for _ in range(4):
        spec = claim(client, worker)
        if spec and spec["id"] == agent_run:
            moved(client, worker, agent_run, "running")
            return agent_run
    raise AssertionError(f"chat run {agent_run} was not claimed")


def test_lifecycle_an_open_run_stream_and_chat_stream_of_a_runs_token_stop_once_it_ends(
    client, hub, hub_db, monkeypatch
):
    monkeypatch.setattr(run_events, "STREAM_POLL_SECONDS", 0.05)
    monkeypatch.setattr(chat_events, "STREAM_POLL_SECONDS", 0.05)
    owner_admin(client, hub)
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id, token = as_me_run(client, hub, worker)
    moved(client, worker, run_id, "running")
    made = client.post("/v1/chats", json={"title": "Release talk", "people": [OTHER]}, headers=hub["owner"])
    assert made.status_code == 201, made.text
    chat_id = made.json()["id"]
    said = client.post(f"/v1/chats/{chat_id}/messages", json={"body": "the release is at noon"}, headers=hub["owner"])
    assert said.status_code == 201, said.text
    user = run_principal(hub_db, run_id)
    request = SimpleNamespace(app=client.app)
    runs_stream = run_events.stream(request, run_id, user, None, 0)
    chats_stream = chat_events.stream(request, (chat_id, user), None, 0, False)

    async def first(stream):
        return await stream.__anext__()

    async def rest(stream, limit: int = 50):
        got = []
        async for item in stream:
            got.append(item)
            if len(got) >= limit:
                break
        return got

    assert client.portal.call(first, runs_stream).id is not None, "the run's events stream while its token lives"
    assert client.portal.call(first, chats_stream) is not None
    assert change(client, hub["owner"], run_id, False).status_code == 200  # the owner stops it: the token ends
    left = client.portal.call(rest, runs_stream)
    assert all(item.event != "end" for item in left) and len(left) < 50, "the run's stream stops, the run goes on"
    (ended,) = [item for item in client.portal.call(rest, chats_stream) if item.event == "end"]
    assert ended.data["reason"] == chat_events.STREAM_TOKEN_ENDED
    assert state_of(hub_db, run_id) == "running"


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
    written = client.put("/v1/memories", params={"sink": "hub"}, json=NOTE, headers=as_run(token))
    assert written.status_code == 200, written.text
    goal = client.put(f"/v1/projects/{PROJECT}/goals/G-1", json=GOAL, headers=as_run(token))
    assert goal.status_code == 403, goal.text
    assert client.put("/v1/secrets/stolen", json={}, headers=as_run(token)).status_code == 403
    # reads are not audited
    assert client.get(f"/v1/projects/{PROJECT}/runs/{run_id}", headers=as_run(token)).status_code == 200
    assert change(client, hub["owner"], run_id, False).status_code == 200

    writes = audit_of(hub_db, "run_access.write")
    assert writes == [
        (f"{run} PUT /v1/memories 200", OWNER, PROJECT, "run"),
        (f"{run} PUT /v1/projects/{PROJECT}/goals/G-1 403", OWNER, PROJECT, "run"),
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


def test_as_me_since_is_when_the_owner_let_the_run_and_the_next_attempt_keeps_it(client, hub, hub_db):
    worker = worker_of(client, hub["owner"], "mac-mini", slots=2)
    plain = step_run(client, hub["owner"], "notes", worker)
    assert view(client, hub["owner"], plain)["as_owner_since"] is None
    granted = change(client, hub["owner"], plain, True).json()
    assert granted["as_owner_since"] is not None and granted["as_owner_since"] >= granted["queued_at"]
    assert view(client, hub["other"], plain)["as_owner_since"] == granted["as_owner_since"], "a reader sees it too"
    stopped = change(client, hub["owner"], plain, False).json()
    assert (stopped["as_owner"], stopped["as_owner_since"]) == (False, None)
    assert client.post(f"/v1/projects/{PROJECT}/runs/{plain}/cancel", headers=hub["owner"]).status_code == 200

    run_id, _ = as_me_run(client, hub, worker, "evo-agents")
    let = view(client, hub["owner"], run_id)
    assert let["as_owner_since"] == let["queued_at"], "let at dispatch: the time it was queued"
    expire(hub_db, run_id)
    assert reap(client)["lost"] == 1
    steady(hub_db, worker)
    retry = view(client, hub["owner"], claim(client, worker)["id"])
    assert (retry["attempt"], retry["as_owner"], retry["as_owner_since"]) == (2, True, let["as_owner_since"])
    with pytest.raises(errors.CheckViolation):  # a run that does not run as its owner has no such time
        sql(hub_db, update(tables.runs).values(as_owner=False).where(tables.runs.c.id == retry["id"]))


# Step 2: the worker and the command line


def through(client):
    """``client._send`` that hands each request of the command line to the in-process hub at HUB_URL."""

    def send(method, url, headers, data, timeout):
        assert url.startswith(HUB_URL + "/"), url
        content = data.read() if hasattr(data, "read") else data
        response = client.request(method, url.removeprefix(HUB_URL), headers=headers, content=content)
        return response.status_code, response.content

    return send


def saved_login(directory: Path, login: str, token: str, url: str = HUB_URL) -> Path:
    """A login directory as `hub login` leaves it."""
    directory.mkdir(parents=True, mode=0o700)
    (directory / "token").write_text(token + "\n", encoding="utf-8")
    (directory / "config.json").write_text(json.dumps({"url": url, "login": login}), encoding="utf-8")
    for name in ("token", "config.json"):
        os.chmod(directory / name, 0o600)
    return directory


def token_of(headers: dict) -> str:
    return headers["Authorization"].removeprefix("Bearer ")


def command(monkeypatch, capsys, env: dict[str, str], *args: str) -> SimpleNamespace:
    """``evo-agents ARGS`` in this process with ``env`` added to its environment."""
    with monkeypatch.context() as patched:
        for name, value in env.items():
            patched.setenv(name, value)
        code = main(list(args))
    out, err = capsys.readouterr()
    return SimpleNamespace(code=code, out=out, err=err)


def agent_env(answer: dict) -> dict[str, str]:
    """What the daemon puts into the agent's environment from the leases of ``answer``: the run's token of the hub
    and the hub's URL (``run_access_env``; with no hub URL of the daemon, the lease's own)."""
    env, refusal = run_access_env([Lease.from_json(item) for item in answer["leases"]], None, guarded=False)
    assert refusal is None
    return env


def test_in_the_run_hub_commands_acts_as_owner_with_no_login_on_the_worker(
    client, hub, hub_db, tmp_path, monkeypatch, capsys
):
    second_project(client, hub)
    monkeypatch.setattr(hub_client, "_send", through(client))
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id = step_run(client, hub["owner"], "notes", worker, as_me=True)
    env = agent_env(leased(client, worker, run_id))
    assert set(env) == {HUB_TOKEN_ENV, HUB_URL_ENV} and env[HUB_URL_ENV] == HUB_URL
    # the worker's machine has no login of its own; the harness's EVO_HUB_HOME names another member's, on another hub
    monkeypatch.setenv("HOME", str(tmp_path / "worker-home"))
    other = saved_login(tmp_path / "hub-lumen", OTHER, "evh_" + "L" * 43, "https://hub.lumen.example")
    env = {**env, "EVO_HUB_HOME": str(other)}

    me = command(monkeypatch, capsys, env, "hub", "whoami")
    assert me.code == 0, me.err
    assert me.out.splitlines()[0] == f"{OWNER} on {HUB_URL}"
    assert f": run on run:{run_id}," in me.out.splitlines()[1]
    grants = json.loads(command(monkeypatch, capsys, env, "hub", "whoami", "--json").out)["grants"]
    assert {grant["project"] for grant in grants} == {PROJECT, SECOND}, "the owner's grants on every project"
    shown = command(monkeypatch, capsys, env, "hub", "run", "show", str(run_id), "--project", PROJECT, "--json")
    assert shown.code == 0, shown.err
    assert json.loads(shown.out)["as_owner"] is True
    assert (
        "runs as them on the hub's API"
        in command(monkeypatch, capsys, env, "hub", "run", "show", str(run_id), "--project", PROJECT).out
    )
    refused = command(monkeypatch, capsys, env, "hub", "secret", "list")
    assert refused.code == 1 and "error: " in refused.err and env[HUB_TOKEN_ENV] not in refused.err

    # the run ends: its token goes with it, and the command says the hub refused it
    assert report(client, worker, run_id, "failed", error="the agent gave up").status_code == 200
    ended = command(monkeypatch, capsys, env, "hub", "whoami")
    assert ended.code == 1 and "run's token is revoked, expired or unknown" in ended.err
    # a run its owner did not let run as them: no variable, and the worker's own login, or none, as before
    plain = step_run(client, hub["owner"], "notes", worker)
    assert agent_env(leased(client, worker, plain)) == {}
    nobody = command(monkeypatch, capsys, {}, "hub", "whoami")
    assert nobody.code == 1 and "not signed in to a hub" in nobody.err


def test_as_me_and_access_from_the_command_line(client, hub, hub_db, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hub_client, "_send", through(client))
    monkeypatch.setenv("HOME", str(tmp_path / "laptop"))
    saved_login(tmp_path / "laptop" / ".evo" / "hub", OWNER, token_of(hub["owner"]))
    worker = worker_of(client, hub["owner"], "mac-mini", slots=2)
    where = ("--project", PROJECT)

    planned = command(monkeypatch, capsys, {}, "hub", "run", "plan", PLAN, "--as-me", *where, "--json")
    assert planned.code == 0, planned.err
    plan_run = json.loads(planned.out)
    assert plan_run["as_owner"] is True
    cancelled = client.post(f"/v1/projects/{PROJECT}/runs/{plan_run['id']}/cancel", headers=hub["owner"])
    assert cancelled.status_code == 200, cancelled.text

    pinned = ("--worker", str(worker["id"]))
    dispatched = command(
        monkeypatch, capsys, {}, "hub", "run", "dispatch", PLAN, str(STEPS["notes"]), *pinned, "--as-me", *where
    )
    assert dispatched.code == 0, dispatched.err
    assert "; it runs as you, secrets, tokens and workers aside." in dispatched.out
    run_id = int(dispatched.out.split("Queued run #", 1)[1].split(":", 1)[0])
    assert view(client, hub["owner"], run_id)["as_owner"] is True
    plain = command(monkeypatch, capsys, {}, "hub", "run", "dispatch", PLAN, str(STEPS["evo-agents"]), *where)
    assert plain.code == 0, plain.err
    assert "runs as you" not in plain.out, "without --as-me the run does not run as you"

    revoked = command(monkeypatch, capsys, {}, "hub", "run", "access", str(run_id), "--revoke", *where, "--json")
    assert revoked.code == 0, revoked.err
    assert json.loads(revoked.out)["as_owner"] is False
    granted = command(monkeypatch, capsys, {}, "hub", "run", "access", str(run_id), "--as-me", *where)
    assert granted.code == 0, granted.err
    assert granted.out.startswith(f"Run #{run_id} runs as you:")
    assert view(client, hub["owner"], run_id)["as_owner"] is True
    with pytest.raises(SystemExit) as neither:
        main(["hub", "run", "access", str(run_id), *where])
    assert neither.value.code == 2
    assert "one of the arguments --as-me --revoke is required" in capsys.readouterr().err

    # another member may not change it, and neither may the run's own token
    other = command(
        monkeypatch,
        capsys,
        {HUB_TOKEN_ENV: token_of(hub["other"]), HUB_URL_ENV: HUB_URL},
        "hub",
        "run",
        "access",
        str(run_id),
        "--revoke",
        *where,
    )
    assert other.code == 1 and f"only {OWNER}, who dispatched run {run_id}" in other.err
    assert claim(client, worker)["id"] == run_id
    token = run_token(client, worker, run_id)
    onward = command(
        monkeypatch,
        capsys,
        {HUB_TOKEN_ENV: token, HUB_URL_ENV: HUB_URL},
        "hub",
        "run",
        "access",
        str(run_id),
        "--revoke",
        *where,
    )
    assert onward.code == 1 and onward.err.startswith("error: ") and token not in onward.err
    assert view(client, hub["owner"], run_id)["as_owner"] is True


# Step 3: a run asks for the credential it lacks, and goes on with what its owner gives (a5)

NEEDED = "NORTHWIND_DEPLOY_KEY"  # the variable the run asks for: the key of the service it deploys to
NEEDED_SECRET = "northwind-deploy-key"  # the secret that keeps it when the agent proposes no name
ANSWER_URL = "/v1/projects/{project}/decisions/{decision_id}/answer"


def credential_run(client, hub, worker: dict, request: dict, **body) -> tuple[int, dict]:
    """A plan run of the owner's, claimed and running on ``worker``, that asked for ``request`` and waits: (its id, the
    decision)."""
    dispatched = client.post(
        f"/v1/projects/{PROJECT}/plan-runs", json={"plan_id": PLAN, "worker_id": worker["id"]}, headers=hub["owner"]
    )
    assert dispatched.status_code == 201, dispatched.text
    run_id = claim(client, worker)["id"]
    moved(client, worker, run_id, "running", session_id="0199a3c1-0000-7000-8000-00000000000d")
    question = {"category": "credential", "question": "The deploy needs its key.", "credential_request": request}
    asked = client.post(f"/v1/worker/runs/{run_id}/decisions", json={**question, **body}, headers=worker["headers"])
    assert asked.status_code == 201, asked.text
    moved(client, worker, run_id, "waiting")
    return run_id, asked.json()


def answer_of(client, headers, decision_id: int, **body):
    return client.post(ANSWER_URL.format(project=PROJECT, decision_id=decision_id), json=body, headers=headers)


def inbox_texts(client, worker: dict, run_id: int) -> list[str]:
    response = client.post(f"/v1/worker/runs/{run_id}/inbox", json={}, headers=worker["headers"])
    assert response.status_code == 200, response.text
    return [message["text"] for message in response.json()["messages"]]


def owners_secrets(client, hub) -> dict:
    listed = client.get("/v1/secrets", headers=hub["owner"])
    assert listed.status_code == 200, listed.text
    return {secret["name"]: secret for secret in listed.json()}


def test_credential_request_asks_the_owner_with_the_hubs_options_and_refuses_what_no_secret_takes(client, hub, hub_db):
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id, asked = credential_run(
        client, hub, worker, {"env_var": NEEDED, "hub_account": True}, recommended=credential_requests.BOTH
    )
    assert asked["credential_request"] == {"env_var": NEEDED, "secret_name": NEEDED_SECRET, "hub_account": True}
    assert [option["key"] for option in asked["options"]] == ["both", "value", "as_me", "refuse"]
    assert asked["recommended"] == "both" and asked["category"] == "credential"
    assert asked["asked_of"] == OWNER, "a credential is its owner's to give"
    assert "--secret" in next(option["description"] for option in asked["options"] if option["key"] == "value")
    shown = client.get(f"/v1/projects/{PROJECT}/decisions/{asked['id']}", headers=hub["other"])
    assert shown.status_code == 200 and shown.json()["credential_request"] == asked["credential_request"]
    # a decision of any other kind shows none, as before
    plain = client.post(f"/v1/worker/runs/{run_id}/decisions", json=QUESTION, headers=worker["headers"])
    assert plain.status_code == 201 and plain.json()["credential_request"] is None

    url = f"/v1/worker/runs/{run_id}/decisions"
    base = {"category": "credential", "question": "The deploy needs its key."}
    for request, extra, why in (
        ({"env_var": "EVO_HUB_TOKEN"}, {}, "steers the shell, git or the worker"),
        ({"env_var": "GIT_ASKPASS"}, {}, "steers the shell, git or the worker"),
        ({"env_var": "LD_PRELOAD"}, {}, "steers the shell, git or the worker"),
        ({"env_var": "PYTHONPATH"}, {}, "steers the shell, git or the worker"),
        ({"env_var": "lower_case"}, {}, "is not a variable name"),
        ({}, {}, "names the variable the run needs"),
        ({"secret_name": "deploy"}, {"hub_account": True}, "name the variable too"),
        ({"env_var": NEEDED, "secret_name": "Not A Name"}, {}, "is not a secret's name"),
        ({"env_var": NEEDED}, {"category": "scope"}, "is a decision of category credential"),
        ({"env_var": NEEDED}, {"options": QUESTION["options"]}, "takes the hub's options"),
        ({"env_var": NEEDED}, {"recommended": "postgres"}, "is not one of"),
    ):
        request = {**request, **{key: value for key, value in extra.items() if key == "hub_account"}}
        body = {**base, "credential_request": request, **{k: v for k, v in extra.items() if k != "hub_account"}}
        refused = client.post(url, json=body, headers=worker["headers"])
        assert refused.status_code == 422, (request, extra, refused.text)
        assert why in json.dumps(refused.json()["detail"]), (why, refused.json())
    single = {**QUESTION, "options": QUESTION["options"][:1]}
    assert client.post(url, json=single, headers=worker["headers"]).status_code == 422, "2 to 6 options, as before"


def test_credential_request_answered_with_a_pasted_value_leases_it_to_the_waiting_run_and_never_shows_it(
    client, hub, hub_db, caplog
):
    caplog.set_level("DEBUG")
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id, asked = credential_run(client, hub, worker, {"env_var": NEEDED})
    assert [option["key"] for option in asked["options"]] == ["value", "refuse"]
    assert NEEDED_SECRET not in by_name(leased(client, worker, run_id)), "nothing to lease before the answer"
    value = sample("deploy-key-")

    answered = answer_of(client, hub["owner"], asked["id"], secret=value)  # the value stands for option value
    assert answered.status_code == 200, answered.text
    decision = answered.json()
    assert (decision["state"], decision["answer_option"], decision["answered_by"]) == ("answered", "value", OWNER)
    assert decision["answer_run_id"] == run_id, "the run waits: the answer goes to its own inbox"
    assert f"secret {NEEDED_SECRET} of kind env for project {PROJECT}" in decision["answer_text"]
    assert credential_requests.ENV_COMMAND in decision["answer_text"]
    secret = owners_secrets(client, hub)[NEEDED_SECRET]
    assert (secret["kind"], secret["env_var"], secret["projects"], secret["workers"]) == (
        "env",
        NEEDED,
        [PROJECT],
        ["mac-mini"],
    ), "bound to the run's project, on the run's worker"
    (message,) = inbox_texts(client, worker, run_id)
    assert message.startswith(f"Answer to decision #{asked['id']} (credential)") and NEEDED_SECRET in message
    # the run waits on its worker: its next ask of the leases gets the value
    lease = by_name(leased(client, worker, run_id))[NEEDED_SECRET]
    assert (lease["env_var"], lease["value"]) == (NEEDED, value)
    put = audit_of(hub_db, "secret.put")
    assert put == [(f"secret:{secret_ids_of(hub_db, NEEDED_SECRET)} decision:{asked['id']}", OWNER, None, "machine")]
    assert audit_of(hub_db, "decision.answer")[-1][0].endswith(f"decision:{asked['id']} run:{run_id} option=value")
    assert audit_of(hub_db, "run_access.grant") == [], "a value lets no run run as its owner"
    # the value is nowhere but sealed: no decision, message, event, audit row or log line holds it
    assert value not in table_dump(hub_db)
    logged = caplog.text + "\n".join(f"{record.getMessage()} {vars(record)!r}" for record in caplog.records)
    assert value not in logged
    assert value not in answered.text


def secret_ids_of(db, name: str) -> int:
    s, u = tables.secrets, tables.users
    ((secret_id,),) = sql(
        db,
        select(s.c.id)
        .join_from(s, u, u.c.id == s.c.owner_id)
        .where(u.c.login == OWNER, s.c.name == name, s.c.deleted_at.is_(None)),
    )
    return secret_id


def test_credential_request_answered_with_both_replaces_the_owners_secret_and_resumes_the_parked_run_as_owner(
    client, hub, hub_db
):
    old, new = sample("old-"), sample("new-")
    credentials_api.env_secret(client, hub["owner"], "deploy", NEEDED, old)
    before = set(owners_secrets(client, hub))
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id, asked = credential_run(client, hub, worker, {"env_var": NEEDED, "hub_account": True})
    assert by_name(leased(client, worker, run_id))["deploy"]["value"] == old
    assert hub_lease(leased(client, worker, run_id)) is None, "the run does not run as its owner yet"
    r = tables.runs
    waited = func.now() - timedelta(seconds=runs.DECISION_WAIT_SECONDS + 1)
    sql(hub_db, update(r).values(waiting_since=waited).where(r.c.id == run_id))
    assert reap(client)["parked"] == 1 and state_of(hub_db, run_id) == "parked"

    answered = answer_of(client, hub["owner"], asked["id"], option="both", secret=new, text="Use the staging key.")
    assert answered.status_code == 200, answered.text
    decision = answered.json()
    assert decision["answer_option"] == "both" and decision["answer_text"].startswith("Use the staging key.\n\n")
    assert "their secret deploy of kind env" in decision["answer_text"], "the secret it had for the variable"
    assert "lets the run run as them on the hub" in decision["answer_text"]
    assert set(owners_secrets(client, hub)) == before, "replaced, not one more"
    resumed = claim(client, worker)
    assert resumed["resume_of_run_id"] == run_id and decision["answer_run_id"] == resumed["id"]
    assert view(client, hub["owner"], resumed["id"])["as_owner"] is True
    leases = leased(client, worker, resumed["id"])
    assert by_name(leases)["deploy"]["value"] == new, "the run that resumes is leased the new value"
    token = hub_lease(leases)["value"]
    assert whoami(client, token).json()["login"] == OWNER
    assert audit_of(hub_db, "run_access.grant") == [
        (f"{PROJECT}/{PLAN} run:{run_id} by=decision:{asked['id']}", OWNER, PROJECT, "machine")
    ]
    assert new not in table_dump(hub_db) and old not in table_dump(hub_db)


def test_credential_request_answer_refusals_say_why_without_the_value(client, hub, hub_db):
    credentials_api.git_secret(client, hub["owner"], "taken", GITLAB_KB, sample("glpat-"))
    worker = worker_of(client, hub["owner"], "mac-mini", slots=2)
    run_id, asked = credential_run(client, hub, worker, {"env_var": NEEDED, "secret_name": "taken"})
    plain = client.post(f"/v1/worker/runs/{run_id}/decisions", json=QUESTION, headers=worker["headers"])
    assert plain.status_code == 201, plain.text
    value = sample("deploy-key-")
    for headers, body, status, why in (
        (hub["other"], {"secret": value}, 403, f"only {OWNER}, who dispatched its run, may answer"),
        (hub["owner"], {"option": "value"}, 422, "gives the value of NORTHWIND_DEPLOY_KEY: paste it with"),
        (hub["owner"], {"option": "refuse", "secret": value}, 422, "takes no value"),
        (hub["owner"], {"secret": value, "text": f"it is {value}"}, 422, "your words hold the value"),
        (hub["owner"], {"secret": "x" * (MAX_SECRET_BYTES + 1)}, 422, "does not match the API"),
        (hub["owner"], {"secret": "a\x00b"}, 422, "may not hold a NUL"),
        (hub["owner"], {"secret": value}, 409, "your secret taken is for something else"),
    ):
        refused = answer_of(client, headers, asked["id"], **body)
        assert refused.status_code == status, (body.keys(), refused.text)
        assert why in refused.json()["message"], refused.json()
        assert value not in refused.text and "x" * 100 not in refused.text
    refused = answer_of(client, hub["owner"], plain.json()["id"], option="postgres", secret=value)
    assert refused.status_code == 422 and "asks for no variable's value" in refused.json()["message"]
    assert value not in refused.text
    d = tables.decisions
    assert sql(hub_db, select(d.c.state).where(d.c.id == asked["id"])) == [("open",)]
    assert NEEDED_SECRET not in owners_secrets(client, hub) and value not in table_dump(hub_db)
    # refusing gives nothing and needs no value
    refusal = answer_of(client, hub["owner"], asked["id"], option="refuse")
    assert refusal.status_code == 200 and refusal.json()["answer_text"] is None
    assert audit_of(hub_db, "secret.put")[-1][0].startswith("secret:") and len(audit_of(hub_db, "secret.put")) == 1


def test_credential_request_answered_from_the_command_line_with_the_value_on_stdin(
    client, hub, hub_db, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(hub_client, "_send", through(client))
    monkeypatch.setenv("HOME", str(tmp_path / "laptop"))
    saved_login(tmp_path / "laptop" / ".evo" / "hub", OWNER, token_of(hub["owner"]))
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id, asked = credential_run(client, hub, worker, {"env_var": NEEDED, "hub_account": True})
    where = ("--project", PROJECT)

    shown = command(monkeypatch, capsys, {}, "hub", "decision", "show", str(asked["id"]), *where)
    assert shown.code == 0, shown.err
    assert f"the value of {NEEDED}, kept as your secret {NEEDED_SECRET}" in shown.out
    assert "your hub account" in shown.out and "--secret" in shown.out
    contract = json.loads(
        command(monkeypatch, capsys, {}, "hub", "decision", "show", str(asked["id"]), *where, "--json").out
    )
    assert contract["credential_request"]["env_var"] == NEEDED

    wrong = command(
        monkeypatch, capsys, {}, "hub", "decision", "answer", str(asked["id"]), "--secret", "--option", "refuse", *where
    )
    assert wrong.code == 2 and "--secret goes with option value or both" in wrong.err
    value = sample("deploy-key-")
    monkeypatch.setattr("sys.stdin", io.StringIO(value + "\n"))
    answered = command(
        monkeypatch, capsys, {}, "hub", "decision", "answer", str(asked["id"]), "--secret", "--option", "both", *where
    )
    assert answered.code == 0, answered.err
    assert answered.out.startswith(f"Answered decision #{asked['id']} with option both")
    assert f"their secret {NEEDED_SECRET} of kind env" in answered.out
    assert value not in answered.out + answered.err
    lease = by_name(leased(client, worker, run_id))[NEEDED_SECRET]
    assert lease["value"] == value, "the final line break of stdin dropped"
    assert view(client, hub["owner"], run_id)["as_owner"] is True

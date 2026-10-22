"""The ledger S1 and the floor S3 on the hub (plan autonomy-levels, criterion a10): every decision of a hub gate writes
the audit row ``autonomy.decided`` naming the run, its principal, its agent, the class, the mode and the result (K7 at
a lease, K8 and K12 at the routes, a gate of ``autonomy_gates``, an act the worker's hook reports), once; the values the
hub leased a run never reach the events it stores or the text of its agent's chat messages; a stop of autonomy ends the
leases of the runs it cancels (test_autonomy_level_stop for the rest of the stop)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import tables
from evo_agents.hub.server import agent_account_mask, autonomy_gates
from tests.hub.live import sql
from tests.hub.test_autonomy_level_lease import autonomy_worker, github_repos, pref, secrets_of
from tests.hub.test_autonomy_level_routes import SECRET, as_run  # noqa: F401 (as_run is a fixture)
from tests.hub.test_autonomy_level_runs import on_db
from tests.hub.test_credentials_api import (  # noqa: F401 (app_key, client, config and hub are fixtures)
    MINE,
    OWNER,
    app_key,
    audit_rows,
    by_name,
    client,
    config,
    hub,
    leased,
    plan_run,
)

DECIDED = "autonomy.decided"


def agent_of(db, run_id: int) -> str:
    r, ap = tables.runs, tables.agent_profiles
    return sql(db, select(ap.c.handle).join_from(r, ap, ap.c.id == r.c.profile_id).where(r.c.id == run_id))[0][0]


def decided(db) -> list[str]:
    return [target for target, *_ in audit_rows(db, DECIDED)]


def test_autonomy_level_ledger_of_k7_at_a_lease_names_run_principal_agent_class_mode_and_result(
    client, hub, github, hub_db
):
    github_repos(github)
    secrets_of(client, hub)
    pref(client, hub["owner"], K7="deny")
    run_id = plan_run(client, hub["owner"], worker := autonomy_worker(client, hub["owner"]))
    leased(client, worker, run_id)
    leased(client, worker, run_id)  # the same decision again is written once
    agent = agent_of(hub_db, run_id)
    ((target, login, _, by_token),) = audit_rows(hub_db, DECIDED)
    assert target.startswith(f"run:{run_id} principal={OWNER} agent={agent} K7 mode=deny result=withheld ")
    assert "env:stripe" in target and target.endswith(" lease")
    assert login == OWNER and by_token


def test_autonomy_level_ledger_of_k8_and_k12_at_the_routes_and_of_a_gate(client, hub, hub_db, as_run):
    agent = agent_of(hub_db, as_run)
    assert client.put("/v1/secrets/stripe", json=SECRET, headers=hub["owner"]).status_code == 403
    assert client.get("/v1/secrets", headers=hub["owner"]).status_code == 200  # a read decides nothing
    head = f"run:{as_run} principal={OWNER} agent={agent}"
    assert decided(hub_db) == [f"{head} K8 mode=deny result=refused PUT /v1/secrets/{{name}}"]

    async def gate(conn):
        return await autonomy_gates.check(conn, as_run, "K5", key="merge a#1", what="merge pull request #1 of a")

    verdict = on_db(hub_db, gate)
    assert verdict.act, "K5 is auto for the owner"
    assert decided(hub_db)[-1] == f"{head} K5 mode=auto result=acted merge a#1"


def test_autonomy_level_ledger_of_an_act_the_worker_hook_reports(client, hub, hub_db):
    pref(client, hub["owner"], K9="deny", K10="notify")
    run_id = plan_run(client, hub["owner"], worker := autonomy_worker(client, hub["owner"]))
    path = f"/v1/worker/runs/{run_id}/autonomy-acts"
    for body in ({"klass": "K10", "what": "gh issue create -R a/b"}, {"klass": "K9", "what": "rm -rf /data"}):
        response = client.post(path, json=body, headers=worker["headers"])
        assert response.status_code == 200, response.text
    head = f"run:{run_id} principal={OWNER} agent={agent_of(hub_db, run_id)}"
    assert decided(hub_db) == [
        f"{head} K10 mode=notify result=allowed hook gh issue create -R a/b",
        f"{head} K9 mode=deny result=refused hook rm -rf /data",
    ]


def test_autonomy_level_secret_leak_events_the_hub_stores_have_the_runs_lease_values_masked(
    client, hub, github, hub_db
):
    github_repos(github)
    values = secrets_of(client, hub)
    run_id = plan_run(client, hub["owner"], worker := autonomy_worker(client, hub["owner"]))
    leases = by_name(leased(client, worker, run_id))
    token = leases[f"github-app:{MINE}"]["value"]
    said = f"export STRIPE_API_KEY={values['stripe']} and push with {token}"
    event = {
        "seq": 1,
        "at": datetime.now(UTC).isoformat(),
        "kind": "system",
        "body": {"text": said, "nested": [{"value": values["kb"]}], "short": "ab"},
    }
    posted = client.post(f"/v1/worker/runs/{run_id}/events", json={"events": [event]}, headers=worker["headers"])
    assert posted.status_code == 200, posted.text
    e = tables.run_events
    (stored,) = sql(hub_db, select(e.c.body).where(e.c.run_id == run_id, e.c.kind == "system"))
    text = str(stored[0])
    for value in (values["stripe"], values["kb"], token):
        assert value not in text
    assert stored[0]["text"] == "export STRIPE_API_KEY=*** and push with ***"
    assert stored[0]["nested"] == [{"value": "***"}] and stored[0]["short"] == "ab"


def test_autonomy_level_secret_leak_the_chat_text_of_a_runs_agent_has_its_lease_values_masked(
    client, hub, github, hub_db
):
    github_repos(github)
    values = secrets_of(client, hub)
    run_id = plan_run(client, hub["owner"], worker := autonomy_worker(client, hub["owner"]))
    leased(client, worker, run_id)
    sealer = client.app.state.sealer
    text = f"the key is {values['stripe']}, the test one {values['stripe-test']}"

    async def masked(conn):
        return await agent_account_mask.mask_run(conn, sealer, run_id, text)

    assert on_db(hub_db, masked) == "the key is ***, the test one ***"
    assert agent_account_mask.masked({"a": ["x-secret-x"]}, ["secret"]) == {"a": ["x-***-x"]}
    assert agent_account_mask.masked("abc", []) == "abc"


def test_autonomy_level_stop_ends_the_leases_of_the_runs_it_cancels(client, hub, github, hub_db):
    github_repos(github)
    secrets_of(client, hub)
    run_id = plan_run(client, hub["owner"], worker := autonomy_worker(client, hub["owner"]))
    held = len(leased(client, worker, run_id)["leases"])
    response = client.post("/v1/autonomy/stops", json={"scope": "login", "login": OWNER}, headers=hub["owner"])
    assert response.status_code == 201, response.text
    view = response.json()
    assert view["runs"] == [run_id] and view["leases"] == held > 0
    cl = tables.credential_leases
    assert sql(hub_db, select(cl.c.id).where(cl.c.run_id == run_id, cl.c.revoked_at.is_(None))) == []
    (target,) = [row[0] for row in audit_rows(hub_db, "autonomy.stop")]
    assert target == f"stop:{view['id']} login:{OWNER} runs=1 leases={held}"


def test_autonomy_level_trailer_of_a_pull_request_body_names_principal_and_agent_never_runtime_or_model(
    client, hub, hub_db
):
    from evo_agents.hub.server import pull_trailers

    run_id = plan_run(client, hub["owner"], autonomy_worker(client, hub["owner"]))
    agent = agent_of(hub_db, run_id)
    r = tables.runs
    runtime, model = sql(hub_db, select(r.c.runtime, r.c.model).where(r.c.id == run_id))[0]

    async def body(conn):
        return await pull_trailers.block(conn, run_id), await pull_trailers.block(conn, None)

    signed, none = on_db(hub_db, body)
    assert signed == f"\n\nEvo-Principal: {OWNER}\nEvo-Agent: {agent}" and none == ""
    for word in filter(None, (runtime, model)):
        assert word not in signed

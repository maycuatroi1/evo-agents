"""The pool of each agent, step 1 of the plan agent-dedicated-pools (criteria a1 and a7): every agent has one pool,
``agent-<handle>``, made with it and by migration 0189 for the agents that exist; a pool belongs to at most one agent;
an admin of the project or of the hub manages it with the routes of worker pools (join tokens, labels, taints, the
doctor codes it accepts) and a member who is not an admin is refused; every agent has a placement, ``legacy`` for those
that existed and ``pool`` for those made since, which an admin moves with ``PUT .../placement`` and ``hub agent
placement``; the pools of members behave as before and take no name the agents' pools use. Migration 0189 is tested in
``tests.hub.test_migrate``."""

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from psycopg import errors
from sqlalchemy import insert, select, update

from evo_agents.hub import tables
from evo_agents.hub.contract import commands
from tests.hub.live import sql
from tests.hub.test_agent_profiles import (  # noqa: F401 - the fixtures of the profile tests
    agent_cli,
    agents_path,
    app_key,
    cli_hub,
    client,
    created,
    hub,
)
from tests.hub.test_run_cli import as_json
from tests.hub.test_runs import OWNER, PROJECT, audit_rows

POOLS = "/v1/worker-pools"


def pool_path(handle: str, *rest: str) -> str:
    return "/".join([POOLS, f"agent-{handle}", *rest])


def pool_rows(db) -> list[tuple]:
    p = tables.worker_pools
    return sql(db, select(p.c.name, p.c.agent_id).where(p.c.agent_id.is_not(None)).order_by(p.c.name))


def test_every_agent_has_one_pool_made_with_it_and_placed_by_pool(client, hub, hub_db):
    (default,) = client.get(agents_path(), headers=hub["owner"]).json()
    assert (default["home_pool"], default["placement"]) == (f"agent-{PROJECT}", "pool")
    helper = created(client, hub["owner"], "helper")
    assert (helper["home_pool"], helper["placement"]) == ("agent-helper", "pool")
    shown = client.get(agents_path("helper"), headers=hub["reader"]).json()
    assert shown["home_pool"] == "agent-helper"
    assert [name for name, _ in pool_rows(hub_db)] == sorted(["agent-helper", f"agent-{PROJECT}"])
    pool = client.get(pool_path("helper"), headers=hub["owner"])
    assert pool.status_code == 200, pool.text
    assert (pool.json()["agent"], pool.json()["projects"], pool.json()["nodes"]) == ("helper", [PROJECT], [])


def test_a_pool_belongs_to_at_most_one_agent(client, hub, hub_db):
    created(client, hub["owner"], "helper")
    p = tables.worker_pools
    ((pool_id, agent_id),) = sql(hub_db, select(p.c.id, p.c.agent_id).where(p.c.name == "agent-helper"))
    ((owner_id,),) = sql(hub_db, select(p.c.owner_id).where(p.c.id == pool_id))
    with live.connect(hub_db) as conn, pytest.raises(errors.UniqueViolation):
        conn.execute(insert(p).values(owner_id=owner_id, name="agent-twin", agent_id=agent_id))


def test_an_admin_of_the_project_or_the_hub_manages_the_pool_and_others_are_refused(client, hub, hub_db):
    created(client, hub["owner"], "helper")
    for who in ("owner", "admin"):
        token = client.post(pool_path("helper", "tokens"), json={"max_uses": 2}, headers=hub[who])
        assert token.status_code == 201, token.text
        assert token.json()["token"].startswith("evj_")
        listed = client.get(pool_path("helper", "tokens"), headers=hub[who]).json()
        assert listed[0]["status"] == "live"
        revoked = client.post(pool_path("helper", "tokens", str(token.json()["id"]), "revoke"), headers=hub[who])
        assert (revoked.status_code, revoked.json()["status"]) == (200, "revoked")
        changed = client.patch(
            pool_path("helper"),
            json={"labels": ["gpu"], "taints": ["agent=helper"], "accepted_findings": ["tool-missing"], "slots": 2},
            headers=hub[who],
        )
        assert changed.status_code == 200, changed.text
        body = changed.json()
        assert (body["labels"], body["taints"], body["accepted_findings"], body["slots"]) == (
            ["gpu"],
            ["agent=helper"],
            ["tool-missing"],
            2,
        )
    for who in ("other", "reader"):  # members of the project who are no admin
        for method, path, extra in (
            ("get", pool_path("helper"), {}),
            ("post", pool_path("helper", "tokens"), {"json": {}}),
            ("get", pool_path("helper", "tokens"), {}),
            ("patch", pool_path("helper"), {"json": {"slots": 3}}),
        ):
            refused = getattr(client, method)(path, headers=hub[who], **extra)
            assert refused.status_code == 403, (who, path, refused.text)
            assert "admin role" in refused.json()["message"]
    stranger = client.get(pool_path("helper"), headers=hub["stranger"])
    assert stranger.status_code == 404
    assert client.get(pool_path("nobody"), headers=hub["owner"]).status_code == 404


def test_the_pool_of_an_agent_serves_its_project_alone(client, hub, hub_db):
    created(client, hub["owner"], "helper")
    refused = client.patch(pool_path("helper"), json={"projects": [PROJECT]}, headers=hub["owner"])
    assert refused.status_code == 422 and "its agent alone" in refused.json()["message"]


def test_the_list_holds_the_pools_of_the_agents_the_caller_administers(client, hub, hub_db):
    created(client, hub["owner"], "helper")
    names = {who: [p["name"] for p in client.get(POOLS, headers=hub[who]).json()] for who in hub}
    assert names["owner"] == sorted(["agent-helper", f"agent-{PROJECT}"])
    assert names["admin"] == sorted(["agent-helper", f"agent-{PROJECT}"])
    assert names["other"] == names["reader"] == names["stranger"] == []


def test_a_member_pool_behaves_as_before_and_never_takes_the_prefix_of_the_agents(client, hub, hub_db):
    made = client.post(POOLS, json={"name": "ci", "projects": [PROJECT]}, headers=hub["owner"])
    assert made.status_code == 201, made.text
    assert made.json()["agent"] is None
    refused = client.post(POOLS, json={"name": "Agent-ci", "projects": [PROJECT]}, headers=hub["owner"])
    assert refused.status_code == 422 and "pools of agents" in refused.json()["message"]
    assert client.get(f"{POOLS}/ci", headers=hub["admin"]).status_code == 404  # still the owner's alone


def test_an_admin_moves_an_agent_between_placements_and_a_writer_cannot(client, hub, hub_db):
    created(client, hub["owner"], "helper")
    a = tables.agent_profiles
    sql(hub_db, update(a).values(placement="legacy").where(a.c.handle == "helper"))
    assert client.get(agents_path("helper"), headers=hub["owner"]).json()["placement"] == "legacy"
    refused = client.put(agents_path("helper") + "/placement", json={"placement": "pool"}, headers=hub["other"])
    assert refused.status_code == 403
    moved = client.put(agents_path("helper") + "/placement", json={"placement": "pool"}, headers=hub["owner"])
    assert (moved.status_code, moved.json()["placement"], moved.json()["version"]) == (200, "pool", 1)
    again = client.put(agents_path("helper") + "/placement", json={"placement": "pool"}, headers=hub["owner"])
    assert again.status_code == 200
    back = client.put(agents_path("helper") + "/placement", json={"placement": "legacy"}, headers=hub["admin"])
    assert (back.status_code, back.json()["placement"]) == (200, "legacy")
    assert (
        client.put(agents_path("helper") + "/placement", json={"placement": "x"}, headers=hub["owner"]).status_code
        == 422
    )
    assert [row[1] for row in audit_rows(hub_db, "agent") if row[0] == "agent.placement"] == [
        f"{PROJECT}/@helper v1 placement=legacy->pool",
        f"{PROJECT}/@helper v1 placement=pool->legacy",
    ]


def test_hub_agent_placement_moves_an_agent_from_the_command_line(cli_hub, monkeypatch, capsys):
    grant = cli_hub.client.put(
        f"/v1/admin/projects/{PROJECT}/grants/{OWNER}",
        json={"role": "admin", "max_level": "internal"},
        headers=cli_hub.headers["admin"],
    )
    assert grant.status_code == 200, grant.text
    assert "hub agent placement" in commands()
    created(cli_hub.client, cli_hub.headers["owner"], "helper")
    moved = agent_cli(monkeypatch, capsys, cli_hub.homes["owner"], "placement", "@helper", "legacy", "--json")
    assert as_json(moved)["placement"] == "legacy"

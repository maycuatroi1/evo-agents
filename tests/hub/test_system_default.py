"""The default system of a project and the ops of a system (plan hub-steward, criterion a11;
``evo_agents.hub.server.system_ops``, ``system_seed``, the key ``ops`` and ``limits.ops_daily_usd`` of
``evo_agents.hub.system_spec``).

Without a database: a spec from before ``ops`` reads as ops off and is stored as it is; ``ops`` and ``ops_daily_usd``
are checked in their bounds; ``hub system default`` and ``hub system ops`` call their routes. With a test database: only
an admin sets the default, at most one, ``list`` marks it and deleting it leaves none; the hub gives a project with no
system its ``orchestrator`` system once; an admin pauses and resumes the ops of a system.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("sqlalchemy", reason="the hub tables need the hub-server extra, evo-ak[hub-server]")

from evo_agents.hub import cli_systems, system_spec
from tests.hub import pg
from tests.hub.test_agent_systems import SPEC, run_cli

OLD = {k: v for k, v in SPEC.items() if k != "nodes"} | {"nodes": [{"id": "in", "kind": "entry"}], "edges": []}


# The spec


def test_a_spec_from_before_ops_reads_as_ops_off_and_is_stored_as_it_is():
    found, problems = system_spec.check(OLD)
    assert not system_spec.errors(problems)
    assert "ops" not in found and "ops_daily_usd" not in found["limits"]
    assert system_spec.check(found)[0] == found, "checked again, it is the same spec: no new revision"
    assert system_spec.ops_of(found)["enabled"] is False
    assert system_spec.limit_of(found, "ops_daily_usd") == 5.0


def test_ops_and_ops_daily_usd_are_checked_in_their_bounds():
    given = {**OLD, "ops": {"enabled": True, "max_items": 3}, "limits": {"ops_daily_usd": 200}}
    found, problems = system_spec.check(given)
    assert not system_spec.errors(problems)
    assert found["ops"] == {"enabled": True, "debounce_minutes": 5, "max_items": 3}
    assert found["limits"]["ops_daily_usd"] == 200.0
    assert "ops:" in system_spec.to_yaml("dev", found)
    bad = {**OLD, "ops": {"enabled": "yes", "max_items": 0, "disk": 1}, "limits": {"ops_daily_usd": 1001}}
    paths = sorted(p.path for p in system_spec.errors(system_spec.check(bad)[1]))
    assert paths == ["limits.ops_daily_usd", "ops.disk", "ops.enabled", "ops.max_items"]


# The CLI


class FakeHub:
    def __init__(self):
        self.calls: list[tuple] = []

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.endswith("/batches?limit=50&state=ready"):
            return []
        if path.endswith("/default-system"):
            ops = {"enabled": False, "debounce_minutes": 5, "max_items": 20}
            limits = {"ops_daily_usd": 5.0}
            return {"project": "northwind", "slug": "dev", "name": "Dev", "ops": ops, "limits": limits, "can_change": 1}
        return {"project": "northwind", "slug": "dev", "default": True, "ops": {}, "ops_daily_usd": 5.0, "paused": True}


def test_default_and_ops_call_their_routes(monkeypatch, capsys):
    hub = FakeHub()
    monkeypatch.setattr(cli_systems, "signed_in", lambda: (hub, None))
    assert run_cli("system", "default", "--project", "northwind", "--json") == 0
    assert {"slug", "name", "ops", "limits"} <= set(json.loads(capsys.readouterr().out))
    assert run_cli("system", "default", "dev", "--project", "northwind") == 0
    assert run_cli("system", "ops", "pause", "dev", "--project", "northwind", "--reason", "a drill", "--json") == 0
    assert run_cli("system", "ops", "resume", "dev", "--project", "northwind", "--json") == 0
    assert run_cli("system", "ops", "batches", "dev", "--project", "northwind", "--state", "ready") == 0
    assert [(m, p) for m, p, _ in hub.calls] == [
        ("GET", "/v1/projects/northwind/default-system"),
        ("PUT", "/v1/projects/northwind/default-system"),
        ("POST", "/v1/projects/northwind/systems/dev/ops/pause"),
        ("POST", "/v1/projects/northwind/systems/dev/ops/resume"),
        ("GET", "/v1/projects/northwind/systems/dev/ops/batches?limit=50&state=ready"),
    ]
    assert hub.calls[1][2] == {"slug": "dev"} and hub.calls[2][2] == {"reason": "a drill"}


# The hub (needs a test database)

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
if pg.DSN:
    from tests.hub.test_agent_systems import client, hub  # noqa: F401 (fixtures)


def path(hub, tail: str) -> str:
    return f"/v1/projects/{hub['project']}/{tail}"


def seed_pass(client) -> int:
    from evo_agents.hub.server.system_seed import seed_pass

    return client.portal.call(seed_pass, client.app.state.engine)


@needs_pg
def test_only_an_admin_sets_the_default_list_marks_it_and_deleting_it_leaves_none(client, hub, hub_db):
    from tests.hub.test_runs import audit_rows

    c = client
    for slug in ("support", "spare"):
        made = c.put(path(hub, f"systems/{slug}"), json={"spec": {**OLD, "name": slug}}, headers=hub["owner"])
        assert made.status_code == 200, made.text
    assert c.get(path(hub, "default-system"), headers=hub["reader"]).json()["slug"] is None
    refused = c.put(path(hub, "default-system"), json={"slug": "support"}, headers=hub["other"])
    assert refused.status_code == 403, refused.text
    assert c.put(path(hub, "default-system"), json={"slug": "nope"}, headers=hub["owner"]).status_code == 404
    found = c.put(path(hub, "default-system"), json={"slug": "support"}, headers=hub["owner"]).json()
    assert (found["slug"], found["ops"]["enabled"], found["limits"]["ops_daily_usd"]) == ("support", False, 5.0)
    assert c.put(path(hub, "default-system"), json={"slug": "spare"}, headers=hub["owner"]).status_code == 200
    listed = c.get(path(hub, "systems"), headers=hub["reader"]).json()
    assert [(s["slug"], s["default"]) for s in listed] == [("spare", True), ("support", False)]
    assert c.delete(path(hub, "systems/spare"), headers=hub["owner"]).status_code == 200
    assert c.get(path(hub, "default-system"), headers=hub["reader"]).json()["slug"] is None
    assert seed_pass(client) == 0, "a project with a system gets none from the hub"
    assert c.get(path(hub, "default-system"), headers=hub["reader"]).json()["slug"] == "support", "its oldest"
    assert [row[0] for row in audit_rows(hub_db, "system") if row[0] == "system.default"] == ["system.default"] * 2


@needs_pg
def test_a_project_with_no_system_gets_its_orchestrator_system_once(client, hub):
    c = client
    assert seed_pass(client) == 1
    found = c.get(path(hub, "default-system"), headers=hub["reader"]).json()
    assert (found["slug"], found["name"], found["ops"]["enabled"]) == ("orchestrator", "Orchestrator", False)
    system = c.get(path(hub, "systems/orchestrator"), headers=hub["reader"]).json()
    assert [(p["kind"], p["profile"], p["version"]) for p in system["pins"]] == [
        ("orchestrator", f"{hub['project']}-orchestrator", 1)
    ]
    assert not [p for p in system["problems"] if p["level"] == "error"]
    assert c.delete(path(hub, "systems/orchestrator"), headers=hub["owner"]).status_code == 200
    assert seed_pass(client) == 0, "seeded once: deleting it leaves the project with no default"
    assert c.get(path(hub, "default-system"), headers=hub["reader"]).json()["slug"] is None


@needs_pg
def test_an_admin_pauses_and_resumes_the_ops_of_a_system(client, hub):
    c = client
    assert seed_pass(client) == 1
    ops = path(hub, "systems/orchestrator/ops")
    assert c.post(f"{ops}/pause", json={}, headers=hub["other"]).status_code == 403
    paused = c.post(f"{ops}/pause", json={"reason": "a drill"}, headers=hub["owner"]).json()
    assert (paused["paused"], paused["paused_reason"], paused["default"]) == (True, "a drill", True)
    assert paused["batches"] == {"open": 0, "ready": 0, "empty": 0, "running": 0, "done": 0}
    assert c.post(f"{ops}/resume", headers=hub["owner"]).json()["paused"] is False
    assert c.get(f"{ops}/batches", headers=hub["reader"]).json() == []

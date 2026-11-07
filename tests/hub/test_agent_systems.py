"""Agent systems of a project (``docs/hub.md``, Agent systems; plan multi-agent-systems, criterion a3):
``evo_agents.hub.system_spec``, the routes of ``evo_agents.hub.server.agent_systems``, ``evo-agents hub system`` and a
profile's ``runs_on``.

Checked here without a database: the spec takes the five node kinds and five edge kinds, refuses an edge whose ends do
not fit its kind, a spec without exactly one entry, an unknown key and a second curator, and names the node or edge of
each problem; the references are checked against the project (a missing or deleted profile, a version it never had, a
graph it lacks, an entry agent of another system); a node without a version is pinned at the latest; YAML goes out and
back; the CLI calls the routes, prints the problems of a refused put and takes the revision export wrote; ``hub agent
create|edit --runs-on`` sends it. With a test database (``EVO_HUB_TEST_DSN``): an admin creates, reads, replaces and
deletes a system by revision, with 409 ``revision_conflict`` and 422 problems; a member reads and cannot write; a
profile is the entry agent of one system; the API shows the latest version next to the pinned one; a chat run of an
entry agent takes the pinned version.
"""

from __future__ import annotations

import json
from functools import partial

import pytest

from evo_agents.hub import cli_agents, cli_systems, system_spec, tables_kg, tables_systems
from evo_agents.hub.client import HubError
from tests.hub import pg

SPEC = {
    "schema": system_spec.SCHEMA,
    "name": "Northwind support",
    "purpose": "Answer the customers of Northwind.",
    "nodes": [
        {"id": "in", "kind": "entry", "position": {"x": 0, "y": 0}},
        {"id": "triage", "kind": "agent", "profile": "lumen-triage", "position": {"x": 200, "y": 0}},
        {"id": "billing", "kind": "agent", "profile": "lumen-billing", "version": 1},
        {"id": "docs", "kind": "kg", "graph": "docs"},
        {"id": "curator", "kind": "curator"},
        {"id": "lead", "kind": "orchestrator", "profile": "lumen-lead"},
    ],
    "edges": [
        {"from": "in", "to": "triage", "kind": "interacts", "description": "every question"},
        {"from": "triage", "to": "billing", "kind": "interacts", "description": "invoices"},
        {"from": "triage", "to": "docs", "kind": "uses_kg", "description": "product docs"},
        {"from": "curator", "to": "docs", "kind": "optimizes", "description": ""},
        {"from": "lead", "to": "billing", "kind": "orchestrates", "description": ""},
        {"from": "billing", "to": "lead", "kind": "escalates", "description": "refunds over 100 USD"},
    ],
}
PROFILES = {
    "lumen-triage": system_spec.ProfileRef(3),
    "lumen-billing": system_spec.ProfileRef(2),
    "lumen-lead": system_spec.ProfileRef(1),
}


def problems_of(spec) -> list[system_spec.Problem]:
    return system_spec.check(spec)[1]


def errors_of(spec) -> list[tuple[str, str | None, int | None]]:
    return [(p.path, p.node, p.edge) for p in system_spec.errors(problems_of(spec))]


def with_edge(edge: dict) -> dict:
    return {**SPEC, "edges": [*SPEC["edges"], edge]}


# The spec


def test_a_spec_of_every_node_and_edge_kind_has_no_problem_and_gets_the_default_limits():
    found, problems = system_spec.check(SPEC)
    assert problems == []
    assert {n["kind"] for n in found["nodes"]} == set(system_spec.NODE_KINDS)
    assert {e["kind"] for e in found["edges"]} == set(system_spec.EDGE_KINDS)
    assert found["limits"] == {
        "trace_usd": 2.0,
        "trace_runs": 12,
        "daily_usd": 5.0,
        "idle_minutes_hub": 10,
        "idle_minutes_worker": 30,
    }
    assert system_spec.entry_agent(found) == "lumen-triage"


@pytest.mark.parametrize(
    "edge",
    [
        {"from": "triage", "to": "in", "kind": "interacts"},  # to the entry
        {"from": "docs", "to": "triage", "kind": "uses_kg"},  # from a kg
        {"from": "curator", "to": "lead", "kind": "optimizes"},  # to the orchestrator
        {"from": "triage", "to": "billing", "kind": "orchestrates"},  # not from the orchestrator
        {"from": "lead", "to": "triage", "kind": "escalates"},  # from the orchestrator
    ],
)
def test_an_edge_whose_ends_do_not_fit_its_kind_is_an_error_on_that_edge(edge):
    assert errors_of(with_edge(edge)) == [(f"edges[{len(SPEC['edges'])}]", None, len(SPEC["edges"]))]


def test_a_system_has_exactly_one_entry():
    without = {**SPEC, "nodes": SPEC["nodes"][1:], "edges": SPEC["edges"][1:]}
    assert ("nodes", None, None) in errors_of(without)
    second = {**SPEC, "nodes": [*SPEC["nodes"], {"id": "in2", "kind": "entry"}]}
    assert errors_of(second) == [("nodes", "in2", None)]


def test_a_second_curator_an_unknown_key_and_a_dangling_edge_are_errors_on_their_node_or_edge():
    spec = {
        **SPEC,
        "colour": "red",
        "nodes": [*SPEC["nodes"], {"id": "c2", "kind": "curator", "shape": "round"}],
        "edges": [*SPEC["edges"], {"from": "triage", "to": "nowhere", "kind": "interacts"}],
    }
    assert sorted(errors_of(spec), key=str) == sorted(
        [("colour", None, None), ("nodes[6].shape", "c2", None), ("nodes", "c2", None), ("edges[6].to", None, 6)],
        key=str,
    )


def test_the_entry_interacts_with_one_agent_and_none_is_a_warning():
    two = with_edge({"from": "in", "to": "billing", "kind": "interacts"})
    assert errors_of(two) == [("edges[6]", None, 6)]
    alone = {**SPEC, "edges": SPEC["edges"][1:]}
    assert errors_of(alone) == []
    assert [(p.level, p.node) for p in problems_of(alone)] == [("warning", "in")]


def test_limits_take_numbers_in_their_bounds():
    found, problems = system_spec.check({**SPEC, "limits": {"trace_usd": 1.5, "trace_runs": 20}})
    assert problems == [] and found["limits"] == {
        "trace_usd": 1.5,
        "trace_runs": 20,
        "daily_usd": 5.0,
        "idle_minutes_hub": 10,
        "idle_minutes_worker": 30,
    }
    assert errors_of({**SPEC, "limits": {"trace_runs": 0, "daily_usd": "5"}}) == [
        ("limits.trace_runs", None, None),
        ("limits.daily_usd", None, None),
    ]


def test_a_node_without_a_version_is_pinned_at_the_latest_and_refs_are_checked_against_the_project():
    found = system_spec.pin(system_spec.check(SPEC)[0], PROFILES)
    assert [(p["node"], p["version"]) for p in system_spec.pins(found)] == [("triage", 3), ("billing", 1), ("lead", 1)]
    problems = system_spec.check_refs(found, PROFILES, {"docs"})
    assert [(p.level, p.node, p.path) for p in problems] == [("warning", "billing", "nodes[2].version")]
    gone = {**PROFILES, "lumen-lead": system_spec.ProfileRef(1, deleted=True)}
    del gone["lumen-billing"]
    beyond = {**found, "nodes": [{**n, "version": 9} if n["id"] == "triage" else n for n in found["nodes"]]}
    problems = system_spec.check_refs(beyond, gone, set(), {"lumen-triage": "other"})
    assert {(p.node, p.path) for p in system_spec.errors(problems)} == {
        ("triage", "nodes[1].version"),
        ("billing", "nodes[2].profile"),
        ("docs", "nodes[3].graph"),
        ("lead", "nodes[5].profile"),
        ("triage", "edges"),
    }


def test_the_default_graph_needs_no_named_graph():
    spec = {**SPEC, "nodes": [{**n, "graph": "default"} if n["kind"] == "kg" else n for n in SPEC["nodes"]]}
    found = system_spec.pin(system_spec.check(spec)[0], PROFILES)
    assert system_spec.errors(system_spec.check_refs(found, PROFILES, set())) == []


def test_yaml_goes_out_and_back_whole():
    found = system_spec.pin(system_spec.check(SPEC)[0], PROFILES)
    text = system_spec.to_yaml("support", found)
    assert text.startswith(f"schema: {system_spec.SCHEMA}\nslug: support\n")
    back, problems = system_spec.check(system_spec.from_yaml(text))
    assert problems == [] and back == found
    with pytest.raises(ValueError):
        system_spec.from_yaml("- a list")


def test_the_patterns_the_cli_checks_are_those_of_the_tables():
    assert system_spec.GRAPH_PATTERN == tables_kg.GRAPH_NAME_PATTERN
    assert system_spec.SLUG_PATTERN == tables_systems.SLUG_PATTERN
    assert system_spec.check_slug("validate") and system_spec.check_slug("Bad Slug")
    assert system_spec.check_slug("northwind-support") is None


# The command line


class FakeHub:
    def __init__(self, refuse: HubError | None = None):
        self.calls: list[tuple] = []
        self.refuse = refuse

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self.refuse is not None:
            raise self.refuse
        found = system_spec.pin(system_spec.check(SPEC)[0], PROFILES)
        system = {
            "project": "northwind",
            "slug": "support",
            "name": found["name"],
            "revision": 2,
            "latest_revision": 2,
            "spec": found,
            "pins": [],
            "problems": [],
            "entry_agent": "lumen-triage",
            "can_change": True,
            "created_by": "ada",
            "updated_by": "ada",
            "created_at": "2026-11-07T00:00:00Z",
            "updated_at": "2026-11-07T00:00:00Z",
        }
        if method == "PUT":
            return {**system, "created": False, "changed": True}
        if method == "POST":
            return {"valid": True, "spec": found, "problems": []}
        if method == "DELETE":
            return {"project": "northwind", "deleted": "support"}
        if path.endswith("/systems"):
            keys = ("slug", "name", "revision", "entry_agent", "updated_by", "updated_at")
            return [{**{k: system[k] for k in keys}, "nodes": 6, "edges": 6}]
        return system


@pytest.fixture
def fake_hub(monkeypatch):
    hub = FakeHub()
    monkeypatch.setattr(cli_systems, "signed_in", lambda: (hub, None))
    return hub


def run_cli(*argv) -> int:
    from evo_agents.cli import main

    return main(["hub", *argv])


def test_export_writes_yaml_that_put_sends_back_with_its_revision(fake_hub, tmp_path, capsys):
    out = tmp_path / "support.yaml"
    assert run_cli("system", "export", "support", "--project", "northwind", "-o", str(out)) == 0
    text = out.read_text()
    assert "# if_revision: 2\n" in text and "slug: support\n" in text
    assert run_cli("system", "put", str(out), "--project", "northwind") == 0
    method, path, body = fake_hub.calls[-1]
    assert (method, path, body["if_revision"]) == ("PUT", "/v1/projects/northwind/systems/support", 2)
    assert "slug" not in body["spec"] and body["spec"]["name"] == "Northwind support"
    assert "stored system support at revision 2" in capsys.readouterr().out


def test_validate_and_delete_call_the_routes(fake_hub, tmp_path, capsys):
    path = tmp_path / "s.yaml"
    path.write_text(system_spec.to_yaml("support", SPEC))
    assert run_cli("system", "validate", str(path), "--project", "northwind") == 0
    assert run_cli("system", "delete", "support", "--project", "northwind", "--json") == 0
    assert run_cli("system", "list", "--project", "northwind") == 0
    assert [(m, p) for m, p, _ in fake_hub.calls] == [
        ("POST", "/v1/projects/northwind/systems/validate"),
        ("DELETE", "/v1/projects/northwind/systems/support"),
        ("GET", "/v1/projects/northwind/systems"),
    ]
    assert fake_hub.calls[0][2]["slug"] == "support"


def test_a_refused_put_prints_each_problem(monkeypatch, tmp_path, capsys):
    detail = [{"level": "error", "path": "edges[0]", "message": "uses_kg goes from agent to kg", "edge": 0}]
    refusal = HubError("agent system support has 1 error", 422, "invalid_request", {"detail": detail})
    monkeypatch.setattr(cli_systems, "signed_in", lambda: (FakeHub(refusal), None))
    path = tmp_path / "s.yaml"
    path.write_text(system_spec.to_yaml("support", SPEC))
    assert run_cli("system", "put", str(path), "--project", "northwind") == 1
    assert "error: edges[0] (edge 0): uses_kg goes from agent to kg" in capsys.readouterr().err


def test_put_needs_a_slug_before_the_hub(fake_hub, tmp_path, capsys):
    path = tmp_path / "s.yaml"
    path.write_text(json.dumps(SPEC))
    assert run_cli("system", "put", str(path), "--project", "northwind") == 2
    assert fake_hub.calls == [] and "which system?" in capsys.readouterr().err


def test_agent_create_and_edit_send_runs_on(monkeypatch):
    calls = []

    class Hub:
        def call(self, method, path, body=None):
            calls.append((method, body))
            agent = {"handle": "lumen-triage", "version": 1, "permissions": {}}
            return agent

    monkeypatch.setattr(cli_agents, "signed_in", lambda: (Hub(), None))
    monkeypatch.setattr(cli_agents, "_done", lambda args, agent, verb: 0)
    assert run_cli("agent", "create", "lumen-triage", "--name", "Triage", "--project", "northwind") == 0
    assert "runs_on" not in calls[-1][1]
    assert run_cli("agent", "create", "lumen-hub", "--name", "Hub", "--project", "northwind", "--runs-on", "hub") == 0
    assert calls[-1][1]["runs_on"] == "hub"
    assert run_cli("agent", "edit", "lumen-triage", "--project", "northwind", "--runs-on", "worker") == 0
    assert calls[-1] == ("PATCH", {"runs_on": "worker", "if_version": 1})


# The hub (needs a test database)

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
ADMIN_GRANT = {"role": "admin", "max_level": "internal"}


@pytest.fixture
def client(hub_db, tmp_path, github, s3):
    from fastapi.testclient import TestClient

    from evo_agents.hub.server.app import create_app
    from tests.hub import live

    config = live.hub_config(hub_db, tmp_path, github, **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as found:
        yield found


@pytest.fixture
def hub(client, github):
    """The members of ``tests.hub.test_runs`` (owner an admin of the project, other a writer, reader a reader) and the
    three agents of SPEC, made by the owner."""
    from tests.hub.test_runs import OWNER, PROJECT, members

    headers = members(client, github)
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=headers["admin"])
    assert granted.status_code == 200, granted.text
    for handle in PROFILES:
        made = client.post(
            f"/v1/projects/{PROJECT}/agents", json={"handle": handle, "name": handle}, headers=headers["owner"]
        )
        assert made.status_code == 201, made.text
    graph = client.post(f"/v1/kg/{PROJECT}/graphs", json={"name": "docs"}, headers=headers["owner"])
    assert graph.status_code == 201, graph.text
    return {**headers, "project": PROJECT}


def systems_path(hub, slug: str | None = None) -> str:
    path = f"/v1/projects/{hub['project']}/systems"
    return path if slug is None else f"{path}/{slug}"


def spec_at_one() -> dict:
    return {**SPEC, "nodes": [{**n, "version": 1} if n["kind"] == "agent" else n for n in SPEC["nodes"]]}


@needs_pg
def test_an_admin_creates_replaces_and_deletes_a_system_by_revision(client, hub, hub_db):
    from tests.hub.test_runs import audit_rows

    c = client
    made = c.put(systems_path(hub, "support"), json={"spec": spec_at_one()}, headers=hub["owner"])
    assert made.status_code == 200, made.text
    assert (made.json()["revision"], made.json()["created"], made.json()["entry_agent"]) == (1, True, "lumen-triage")
    assert c.put(systems_path(hub, "support"), json={"spec": spec_at_one()}, headers=hub["owner"]).status_code == 409
    same = c.put(systems_path(hub, "support"), json={"spec": spec_at_one(), "if_revision": 1}, headers=hub["owner"])
    assert same.status_code == 200 and same.json()["changed"] is False and same.json()["revision"] == 1
    renamed = {**spec_at_one(), "name": "Northwind help"}
    stale = c.put(systems_path(hub, "support"), json={"spec": renamed, "if_revision": 7}, headers=hub["owner"])
    assert stale.status_code == 409 and stale.json()["error"] == "revision_conflict"
    assert stale.json()["current"]["revision"] == 1
    second = c.put(systems_path(hub, "support"), json={"spec": renamed, "if_revision": 1}, headers=hub["owner"])
    assert second.status_code == 200 and second.json()["revision"] == 2
    old = c.get(systems_path(hub, "support"), params={"revision": 1}, headers=hub["reader"]).json()
    assert (old["revision"], old["latest_revision"], old["name"]) == (1, 2, "Northwind support")
    revisions = c.get(systems_path(hub, "support") + "/revisions", headers=hub["reader"]).json()
    assert [r["revision"] for r in revisions] == [2, 1]
    assert [s["slug"] for s in c.get(systems_path(hub), headers=hub["reader"]).json()] == ["support"]
    gone = c.delete(systems_path(hub, "support"), headers=hub["owner"])
    assert gone.status_code == 200 and gone.json() == {"project": hub["project"], "deleted": "support"}
    assert c.get(systems_path(hub, "support"), headers=hub["reader"]).status_code == 404
    assert [row[0] for row in audit_rows(hub_db, "system")] == ["system.create", "system.update", "system.delete"]


@needs_pg
def test_a_member_reads_and_validates_but_only_an_admin_writes(client, hub):
    c = client
    for who in ("other", "reader"):
        refused = c.put(systems_path(hub, "support"), json={"spec": SPEC}, headers=hub[who])
        assert refused.status_code == 403, refused.text
    checked = c.post(systems_path(hub, "validate"), json={"spec": SPEC}, headers=hub["reader"])
    assert checked.status_code == 200 and checked.json()["valid"] is True
    assert c.put(systems_path(hub, "validate"), json={"spec": SPEC}, headers=hub["owner"]).status_code == 422


@needs_pg
def test_a_spec_with_errors_is_422_with_its_problems_and_nothing_is_written(client, hub):
    bad = with_edge({"from": "docs", "to": "triage", "kind": "uses_kg"})
    refused = client.put(systems_path(hub, "support"), json={"spec": bad}, headers=hub["owner"])
    assert refused.status_code == 422
    assert [(p["path"], p["edge"]) for p in refused.json()["detail"]] == [("edges[6]", 6)]
    missing = {**SPEC, "nodes": [*SPEC["nodes"][:5], {**SPEC["nodes"][5], "profile": "lumen-nobody"}]}
    refused = client.put(systems_path(hub, "support"), json={"spec": missing}, headers=hub["owner"])
    assert refused.status_code == 422 and refused.json()["detail"][0]["node"] == "lead"
    assert client.get(systems_path(hub), headers=hub["owner"]).json() == []


@needs_pg
def test_the_api_shows_the_latest_version_next_to_the_pinned_one(client, hub):
    made = client.put(systems_path(hub, "support"), json={"spec": SPEC}, headers=hub["owner"])
    assert made.status_code == 200, made.text
    pins = {p["node"]: p for p in made.json()["pins"]}
    assert (pins["triage"]["version"], pins["triage"]["latest_version"]) == (1, 1)
    edited = client.patch(
        f"/v1/projects/{hub['project']}/agents/lumen-triage", json={"description": "v2"}, headers=hub["owner"]
    )
    assert edited.status_code == 200, edited.text
    shown = client.get(systems_path(hub, "support"), headers=hub["reader"]).json()
    pin = next(p for p in shown["pins"] if p["node"] == "triage")
    assert (pin["version"], pin["latest_version"], pin["outdated"]) == (1, 2, True)
    assert any(p["level"] == "warning" and p["node"] == "triage" for p in shown["problems"])


@needs_pg
def test_a_profile_is_the_entry_agent_of_one_system_and_its_chat_runs_take_the_pinned_version(client, hub):
    from evo_agents.hub.server import chat_runs, profile_store, system_store

    assert client.put(systems_path(hub, "support"), json={"spec": SPEC}, headers=hub["owner"]).status_code == 200
    other = client.put(systems_path(hub, "second"), json={"spec": SPEC}, headers=hub["owner"])
    assert other.status_code == 422
    assert "entry agent of system support" in json.dumps(other.json()["detail"])
    edited = client.patch(
        f"/v1/projects/{hub['project']}/agents/lumen-triage", json={"runs_on": "hub"}, headers=hub["owner"]
    )
    assert edited.status_code == 200 and edited.json()["runs_on"] == "hub" and edited.json()["version"] == 2
    engine = client.app.state.engine

    async def pinned():
        async with engine.connect() as conn:
            profile = await profile_store.by_handle(conn, "lumen-triage")
            return await system_store.entry_pin(conn, profile.id), await chat_runs._pinned(conn, profile)

    entry, (version, _) = client.portal.call(partial(pinned))
    assert entry == ("support", 1) and version == 1

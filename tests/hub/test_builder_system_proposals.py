"""@builder designs an agent system from a member's words (plan multi-agent-systems, criterion a11): the run's agent
proposes kind ``system`` with the spec evo-agent-system/1 the system would have and the agent profiles and named
graphs it needs; the hub checks it as PUT .../systems/{slug} would, keeps it pending and applies nothing. A writer
does not decide it; an admin of the project approving it makes the profiles and the graphs, then writes the spec as
the system's next revision. Rejecting changes nothing, and rolling back writes the revision before again, or deletes
a new system, while the profiles and graphs it made stay (``evo_agents.core_plugins.builder.system_changes``).
"""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from evo_agents.hub import system_spec
from tests.hub.test_builder import (  # noqa: F401 (fixtures)
    answer,
    app_key,
    builder_core,
    client,
    hub,
    made_proposal,
    proposed,
    rollback,
    shown,
    started,
)
from tests.hub.test_plugin_runtimes import message
from tests.hub.test_runs import PROJECT, audit_rows

SLUG = "support"
SPEC = {
    "schema": system_spec.SCHEMA,
    "name": "Northwind support",
    "purpose": "Answer the customers of Northwind.",
    "nodes": [
        {"id": "in", "kind": "entry", "position": {"x": 0, "y": 0}},
        {"id": "triage", "kind": "agent", "profile": "lumen-triage", "position": {"x": 240, "y": 0}},
        {"id": "review", "kind": "agent", "profile": "reviewer", "position": {"x": 480, "y": 0}},
        {"id": "docs", "kind": "kg", "graph": "lumen-docs", "position": {"x": 240, "y": 160}},
    ],
    "edges": [
        {"from": "in", "to": "triage", "kind": "interacts", "description": "every question"},
        {"from": "triage", "to": "review", "kind": "interacts", "description": "a second look"},
        {"from": "triage", "to": "docs", "kind": "uses_kg", "description": "product docs"},
    ],
}
AGENTS = [{"handle": "lumen-triage", "name": "Triage", "instructions": "Sort the questions of the customers."}]
GRAPHS = [{"name": "lumen-docs", "description": "The product docs", "sources": ["docs-site"]}]
SYSTEM = f"/v1/projects/{PROJECT}/systems"


def system(client, headers, slug: str = SLUG):
    return client.get(f"{SYSTEM}/{slug}", headers=headers)


def graphs(client, headers) -> set[str]:
    response = client.get(f"/v1/kg/{PROJECT}/graphs", headers=headers)
    assert response.status_code == 200, response.text
    return {graph["name"] for graph in response.json()["graphs"]}


def without_review(spec: dict) -> dict:
    return {
        **spec,
        "nodes": [node for node in spec["nodes"] if node["id"] != "review"],
        "edges": [edge for edge in spec["edges"] if "review" not in (edge["from"], edge["to"])],
    }


def test_builder_proposal_of_a_system_is_checked_like_its_route_and_applies_nothing_until_an_admin_approves(
    client, hub, hub_db
):
    run = started(client, hub, "@builder design a support desk for Northwind")
    wrong = {**SPEC, "edges": [*SPEC["edges"], {"from": "triage", "to": "nowhere", "kind": "interacts"}]}
    for arguments, said in (
        ({"target": {"system": "validate"}, "set": {"spec": SPEC}}, "target.system: 'validate' is a word of the API"),
        ({"set": {"spec": wrong, "agents": AGENTS, "graphs": GRAPHS}}, "spec.edges[3].to:"),
        ({"set": {"spec": SPEC, "graphs": GRAPHS}}, "spec.nodes[1].profile:"),  # @lumen-triage is no profile yet
        (
            {"set": {"spec": SPEC, "agents": [*AGENTS, {"handle": "reviewer", "name": "R"}], "graphs": GRAPHS}},
            "agents: @reviewer is taken",
        ),
        (
            {"set": {"spec": SPEC, "agents": [*AGENTS, {"handle": "lumen-spare", "name": "S"}], "graphs": GRAPHS}},
            "agents: no node of the spec names @lumen-spare",
        ),
        (
            {"set": {"spec": SPEC, "agents": AGENTS, "graphs": GRAPHS, "colour": "red"}},
            "colour: a system proposal sets spec, agents and graphs alone",
        ),
        (
            {"set": {"spec": {**SPEC, "slug": "desk"}, "agents": AGENTS, "graphs": GRAPHS}},
            "spec.slug is 'desk' and target.system 'support'",
        ),
    ):
        target = arguments.pop("target", {"system": SLUG})
        text = proposed(client, run["agent"], kind="system", summary="A support desk", target=target, **arguments)
        assert text.startswith("error: ") and said in text, text
    sets = {"spec": SPEC, "agents": AGENTS, "graphs": GRAPHS}
    first = made_proposal(
        client, run["agent"], kind="system", summary="A support desk", target={"system": SLUG}, set=sets
    )
    pending = shown(client, hub["reader"], first)
    assert (pending["state"], pending["kind"], pending["base_version"], pending["approver"], pending["target"]) == (
        "pending",
        "system",
        0,
        "project_admin",
        {"system": SLUG},
    )
    assert pending["change"]["set"]["spec"] == SPEC
    # nothing is applied yet: no system, no profile, no graph
    assert system(client, hub["reader"]).status_code == 404
    assert client.get(f"/v1/projects/{PROJECT}/agents/lumen-triage", headers=hub["reader"]).status_code == 404
    assert "lumen-docs" not in graphs(client, hub["reader"])
    # the admin's list of waiting proposals of kind system has it; a writer's has none
    waiting = client.get(f"/v1/x/builder/{PROJECT}/proposals/waiting?kind=system", headers=hub["owner"])
    assert [p["id"] for p in waiting.json()] == [first], waiting.text
    assert client.get(f"/v1/x/builder/{PROJECT}/proposals/waiting?kind=system", headers=hub["other"]).json() == []
    # a writer does not decide it
    assert "needs the admin role on project" in message(answer(client, hub["other"], first, "approve"), 403)
    approved = answer(client, hub["owner"], first, "approve")
    assert (approved.json()["state"], approved.json()["applied_version"]) == ("applied", 1), approved.text
    made = system(client, hub["reader"]).json()
    assert (made["revision"], made["entry_agent"], made["problems"]) == (1, "lumen-triage", [])
    pins = {node["id"]: node.get("version") for node in made["spec"]["nodes"]}
    assert pins == {"in": None, "triage": 1, "review": 1, "docs": None}
    triage = client.get(f"/v1/projects/{PROJECT}/agents/lumen-triage", headers=hub["reader"]).json()
    assert (triage["name"], triage["version"]) == ("Triage", 1)
    assert "lumen-docs" in graphs(client, hub["reader"])
    assert [row[0] for row in audit_rows(hub_db, "system")] == ["system.create"]
    assert any(f"graph=lumen-docs kind=create sources=1 proposal={first}" in row[1] for row in audit_rows(hub_db, "kg"))


def test_builder_proposal_of_a_change_of_a_system_rejected_changes_nothing_and_rolls_back_to_the_revision_before(
    client, hub, hub_db
):
    run = started(client, hub, "@builder design a support desk for Northwind")
    sets = {"spec": SPEC, "agents": AGENTS, "graphs": GRAPHS}
    first = made_proposal(
        client, run["agent"], kind="system", summary="A support desk", target={"system": SLUG}, set=sets
    )
    assert answer(client, hub["owner"], first, "approve").json()["applied_version"] == 1
    held = system(client, hub["reader"]).json()["spec"]
    # the same spec again changes nothing
    same = proposed(client, run["agent"], kind="system", summary="Again", target={"system": SLUG}, set={"spec": SPEC})
    assert same.startswith(f"error: agent system {SLUG} of project {PROJECT} has this spec already"), same
    # a change, rejected: the system stays at revision 1
    change = {"spec": without_review(SPEC)}
    second = made_proposal(
        client, run["agent"], kind="system", summary="No review", target={"system": SLUG}, set=change
    )
    assert shown(client, hub["owner"], second)["base_version"] == 1
    assert answer(client, hub["owner"], second, "reject").json()["state"] == "rejected"
    assert system(client, hub["reader"]).json()["revision"] == 1
    # the change again, approved, then rolled back to a copy of revision 1
    third = made_proposal(client, run["agent"], kind="system", summary="No review", target={"system": SLUG}, set=change)
    assert answer(client, hub["owner"], third, "approve").json()["applied_version"] == 2
    assert [node["id"] for node in system(client, hub["reader"]).json()["spec"]["nodes"]] == ["in", "triage", "docs"]
    assert "it changed since the proposal was applied" in message(rollback(client, hub["owner"], first), 409)
    assert "needs the admin role on project" in message(rollback(client, hub["other"], third), 403)
    back = rollback(client, hub["owner"], third)
    assert (back.json()["state"], back.json()["rollback_version"]) == ("rolled_back", 3), back.text
    again = system(client, hub["reader"]).json()
    assert (again["revision"], again["spec"]) == (3, held)
    assert [row[0] for row in audit_rows(hub_db, "system")] == ["system.create", "system.update", "system.update"]


def test_rolling_back_a_new_system_deletes_it_and_keeps_the_profiles_and_graphs_it_made(client, hub, hub_db):
    run = started(client, hub, "@builder design a support desk for Northwind")
    sets = {"spec": SPEC, "agents": AGENTS, "graphs": GRAPHS}
    first = made_proposal(
        client, run["agent"], kind="system", summary="A support desk", target={"system": SLUG}, set=sets
    )
    assert answer(client, hub["owner"], first, "approve").json()["applied_version"] == 1
    back = rollback(client, hub["owner"], first)
    assert (back.json()["state"], back.json()["rollback_version"]) == ("rolled_back", 0), back.text
    assert system(client, hub["reader"]).status_code == 404
    assert client.get(f"/v1/projects/{PROJECT}/agents/lumen-triage", headers=hub["reader"]).status_code == 200
    assert "lumen-docs" in graphs(client, hub["reader"])
    assert [row[0] for row in audit_rows(hub_db, "system")] == ["system.create", "system.delete"]
    # proposing the system again needs no new profile or graph: it names those the first one made
    again = made_proposal(
        client, run["agent"], kind="system", summary="Again", target={"system": SLUG}, set={"spec": SPEC}
    )
    assert shown(client, hub["owner"], again)["base_version"] == 0

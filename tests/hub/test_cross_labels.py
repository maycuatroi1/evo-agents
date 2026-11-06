"""The one check of what another project may put into a run (``evo_agents.hub.server.cross_labels``): the hub sink of
the run's project clears an item of another project by the names of its level and location, fails closed on a name
its ladder lacks, and says why it leaves an item out."""

from __future__ import annotations

from evo_agents.hub.access import ProjectRules
from evo_agents.hub.server import cross_labels
from evo_agents.hub.server.agent_reads import elements

LEVELS = ["public", "internal", "customer", "secret"]
LOCATIONS = ["any", "domestic-only"]


def rules(name: str, hub: str | None = "internal", levels=LEVELS, locations=LOCATIONS) -> ProjectRules:
    sinks = [{"id": "claude-code@anthropic", "kind": "agent-session", "clearance": {"level": "internal"}}]
    if hub is not None:
        sinks.append({"id": "hub", "kind": "hub", "clearance": {"level": hub, "location": "any"}})
    return ProjectRules(name, levels, locations, sinks, {"level": "internal"})


def test_the_run_sink_clears_by_names_up_to_its_level():
    gate = cross_labels.gate(rules("northwind"), rules("lumen-kb", hub="customer"))
    assert gate.passes({"level": "public"}) and gate.passes({"level": "internal", "location": "any"})
    assert gate.why({"level": "customer"}) == (
        "level customer of project lumen-kb is above what hub sink 'hub' of project northwind clears (internal/any)"
    )
    assert "location domestic-only" in gate.why({"level": "public", "location": "domestic-only"})
    assert gate.why({"level": "public", "projects": ["lumen-kb", "lumen-ops"]}) == "it names other projects (lumen-ops)"
    assert gate.why("internal") == "its label cannot be read"


def test_a_name_the_run_ladder_lacks_fails_closed():
    other = rules("lumen-kb", hub="customer", levels=["open", "internal", "customer"])
    gate = cross_labels.gate(rules("northwind"), other)
    assert gate.ceiling is None and "its lowest level open is not one northwind lets in" in gate.why({"level": "open"})
    stops = cross_labels.gate(rules("northwind", hub="secret"), rules("lumen-kb", levels=["public", "restricted"]))
    assert stops.passes({"level": "public"}) and not stops.passes({"level": "restricted"})


def test_a_run_project_without_a_hub_sink_takes_nothing():
    gate = cross_labels.gate(rules("northwind", hub=None), rules("lumen-kb"))
    assert gate.ceiling is None and "declares no usable hub sink" in gate.why({"level": "public"})


def test_the_agent_is_told_how_many_items_were_left_out_and_why():
    assert cross_labels.withheld([]) == ""
    assert cross_labels.withheld(["a"]) == "1 item of what you may read was left out, since a."
    assert cross_labels.withheld(["a", "b", "a"]) == "3 items of what you may read were left out, since 2: a; 1: b."


def test_elements_reads_ids_and_labels_and_skips_properties():
    data = {
        "nodes": [
            {"id": "n1", "label": {"level": "internal"}, "props": {"id": "p", "label": {"level": "secret"}}},
            {"id": "n2", "label": {"level": "customer"}, "edges": [{"id": "e1", "label": {"level": "public"}}]},
        ]
    }
    assert elements(data) == {"n1": {"level": "internal"}, "n2": {"level": "customer"}, "e1": {"level": "public"}}

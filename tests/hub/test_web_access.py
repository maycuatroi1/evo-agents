"""The web's read rule: the grant alone, on the lattice the sink rule meets it on (``evo_agents.hub.web_access``)."""

from __future__ import annotations

import pytest

from evo_agents.hub.access import ProjectRules
from evo_agents.hub.web_access import grant_ceiling

LEVELS = ["public", "internal", "customer", "secret"]
SINKS = [
    {"id": "agent", "kind": "agent-session", "clearance": {"level": "customer", "location": "domestic"}},
    {"id": "narrow", "kind": "agent-session", "clearance": {"level": "public"}},
    {"id": "hub", "kind": "hub", "clearance": {"level": "internal"}},
]


@pytest.fixture
def rules() -> ProjectRules:
    return ProjectRules("alpha", LEVELS, ["any", "domestic"], SINKS)


@pytest.mark.parametrize("max_level", LEVELS)
def test_the_grant_reaches_its_level_everywhere_in_its_project(rules, max_level):
    ceiling = grant_ceiling(rules, max_level)
    assert (ceiling.level, ceiling.location, ceiling.projects) == (LEVELS.index(max_level), 1, frozenset({"alpha"}))
    for sink in ("agent", "narrow", "hub"):
        clearance = rules.policy.clearance(sink)
        sink_label = type(ceiling)(clearance.level, clearance.location, "U", frozenset({"alpha"}))
        assert rules.ceiling(max_level, sink) == ceiling.meet(sink_label)


@pytest.mark.parametrize("max_level", [None, "", "top-secret", 2])
def test_no_grant_or_an_unknown_level_lets_nothing_through(rules, max_level):
    assert grant_ceiling(rules, max_level) is None


def test_a_label_above_the_grant_or_of_another_project_is_not_below_it(rules):
    ceiling = grant_ceiling(rules, "internal")
    assert rules.stored({"level": "internal", "location": "domestic"}).below(ceiling)
    assert not rules.stored({"level": "customer"}).below(ceiling)
    assert not rules.stored({"level": "public", "projects": ["alpha", "beta"]}).below(ceiling)

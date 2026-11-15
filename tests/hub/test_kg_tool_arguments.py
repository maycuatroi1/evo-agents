"""The arguments the hub puts in a kg tool call of a run's agent (``evo_agents.hub.server.kg``): the default graph of
the run's system goes only to a tool that takes ``graph``, so kg_status and kg_more answer instead of refusing an
argument they do not have; ``branch`` is read on the hub, and a local session refuses one."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from evo_agents.hub.server import kg as server_kg
from evo_agents.kg.tools import SIGNATURES


@pytest.mark.parametrize("tool", ["kg_status", "kg_more"])
def test_a_tool_without_graph_gets_no_default_graph(tool):
    assert not server_kg._takes_graph(tool)
    assert "graph" not in SIGNATURES[tool][0] and "branch" not in SIGNATURES[tool][0]


@pytest.mark.parametrize("tool", ["kg_search", "kg_context", "kg_node", "kg_impact", "kg_path"])
def test_a_reading_tool_takes_graph_and_branch(tool):
    assert server_kg._takes_graph(tool)
    assert {"graph", "branch"} <= SIGNATURES[tool][0]


def test_on_branch_takes_branch_out_of_the_arguments_and_main_reads_main():
    import asyncio

    found = asyncio.run(server_kg._on_branch(None, None, 1, {"query": "kiwi", "branch": "main"}))
    assert found == ({"query": "kiwi"}, None)
    found = asyncio.run(server_kg._on_branch(None, None, 1, {"query": "kiwi"}))
    assert found == ({"query": "kiwi"}, None)
    arguments, error = asyncio.run(server_kg._on_branch(None, None, 1, {"query": "kiwi", "branch": 3}))
    assert arguments == {"query": "kiwi"} and error.startswith("error:")

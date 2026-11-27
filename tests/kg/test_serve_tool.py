"""The kg_* tools as commands: one table of the tools a session answers, each with its schema."""

import pytest

from evo_agents.hub.kg_graph import HubSession, HubStatus
from evo_agents.kg.budget import Budget
from evo_agents.kg.serve import KgStatus, Session
from evo_agents.kg.serve_tool import Answered, ToolTable
from evo_agents.kg.tools import TOOLS
from evo_agents.kg.visibility import Visibility


def test_the_table_lists_every_tool_with_its_schema():
    assert [tool["name"] for tool in Session.tools.schemas()] == [tool["name"] for tool in TOOLS]
    assert all(Session.tools[name].schema["name"] == name for name in Session.tools)


def test_the_hub_extends_the_table_with_its_status():
    assert HubSession.tools["kg_status"] is HubStatus
    assert Session.tools["kg_status"] is KgStatus
    assert list(HubSession.tools) == list(Session.tools)


def test_a_tool_is_registered_by_its_own_name():
    with pytest.raises(ValueError):
        ToolTable({"kg_node": HubStatus})
    assert ToolTable.of().with_(kg_search=Answered)["kg_search"] is Answered


def test_visibility_reads_labels_within_the_clearance():
    everything = Visibility.everything()
    assert everything.label((9, 9, "T", frozenset({"x"})))
    assert not Visibility().label((0, 0, "T"))
    asked = everything.asking(deprecated=True)
    assert not asked.sees_all and asked.deprecated


def test_a_budget_counts_three_bytes_a_token():
    assert Budget(500).bytes == 1500
    assert Budget.tokens_of("ab") == 1
    assert Budget.tokens_of("abcd") == 2

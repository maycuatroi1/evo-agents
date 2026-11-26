"""``kg query``'s first argument: a kg_ tool, its short name, or alone the text of kg_search (curator proposal #83)."""

import pytest

from evo_agents.cli import main


class FakeSession:
    def __init__(self, calls):
        self.calls = calls

    def call(self, tool, params):
        self.calls.append((tool, params))
        return {"content": [{"text": f"{tool} {params}"}]}


@pytest.fixture
def calls(monkeypatch):
    found = []
    monkeypatch.setattr("evo_agents.hub.kg_remote.open_session", lambda backend, project, sink: FakeSession(found))
    return found


def test_query_tool_a_bare_query_runs_kg_search(calls):
    assert main(["kg", "query", "SnapshotCoordinator"]) == 0
    assert calls == [("kg_search", {"query": "SnapshotCoordinator"})]


def test_query_tool_a_short_name_gives_what_its_kg_tool_gives(calls, capsys):
    assert main(["kg", "query", "search", "foo"]) == 0
    short = capsys.readouterr().out
    assert main(["kg", "query", "kg_search", "foo"]) == 0
    assert capsys.readouterr().out == short
    assert calls == [("kg_search", {"query": "foo"})] * 2


@pytest.mark.parametrize(
    "short, tool, params",
    [
        ("context", "kg_context", {"query": "x"}),
        ("node", "kg_node", {"id": "x"}),
        ("impact", "kg_impact", {"id": "x"}),
        ("path", "kg_path", {"from": "x"}),
        ("more", "kg_more", {"handle": "x"}),
    ],
)
def test_query_tool_each_short_name_maps_to_its_kg_tool(calls, short, tool, params):
    assert main(["kg", "query", short, "x"]) == 0
    assert calls == [(tool, params)]


def test_query_tool_status_alone_is_kg_status_not_a_search(calls):
    assert main(["kg", "query", "status"]) == 0
    assert calls == [("kg_status", {})]


def test_query_tool_an_unknown_tool_before_a_text_is_an_invalid_choice(calls, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["kg", "query", "kg_find", "foo"])
    assert exc.value.code == 2
    assert "argument tool: invalid choice: 'kg_find'" in capsys.readouterr().err
    assert calls == []

"""The extension point rankers (``evo_agents.hub.server.rankers``; plan jev-decisions, step 2, criterion a3), with the
sample plugin northwind-ranker (tests/fixtures/core_plugins/northwind_ranker): history_search (REST and MCP) and the
hub's kg_search call the ranker of a plugin the project enabled, with the hits its sink may read, in a budget of
seconds for the whole search; a ranker that fails, is late or answers no order leaves the order as it was; the response
says whether it was ranked again, and a project that enabled nothing gets what it always got."""

from __future__ import annotations

import asyncio
from importlib.metadata import EntryPoint
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import insert, select, update

from evo_agents.hub import mcp_tools, plugin_calls, tables
from evo_agents.hub.server import rankers
from evo_agents.plugins import core
from evo_agents.plugins.core import ENTRY_POINT_GROUP
from tests.hub.conftest import github, hub_db, hub_template, pg_server, s3  # noqa: F401 (fixtures)
from tests.hub.live import sql
from tests.hub.test_history_runs import lab  # noqa: F401 (a fixture)
from tests.hub.test_history_search import MCP_HEADERS, search, two_sessions
from tests.hub.test_runs import PROJECT

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "core_plugins"
PLUGIN = "northwind-ranker"
SINK = "ranker@northwind"
RANKER = EntryPoint(PLUGIN, "northwind_ranker:manifest", ENTRY_POINT_GROUP)


@pytest.fixture
def installed(monkeypatch):
    monkeypatch.syspath_prepend(str(FIXTURES))
    monkeypatch.setattr(core, "entry_points", lambda *, group: [RANKER] if group == ENTRY_POINT_GROUP else [])
    plugin_calls.core_registry.cache_clear()
    import northwind_ranker

    monkeypatch.setattr(northwind_ranker, "CALLS", [])
    monkeypatch.setattr(northwind_ranker, "MODE", "reverse")
    yield northwind_ranker
    plugin_calls.core_registry.cache_clear()


def enable(lab, level: str | None = "internal") -> None:
    """The project enabling the plugin, and declaring its sink at ``level`` (no sink when None)."""
    response = lab.client.post(
        f"/v1/projects/{PROJECT}/plugins/{PLUGIN}/enable", json={"permissions": []}, headers=lab.headers["owner"]
    )
    assert response.status_code == 200, response.text
    if level is not None:
        project_id = sql(lab.db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
        sql(
            lab.db,
            insert(tables.project_sinks).values(
                project_id=project_id, sink_id=SINK, kind="llm-backend", clearance={"level": level}
            ),
        )


def evidence(found: dict) -> list[str]:
    return [hit["evidence"] for hit in found["hits"]]


# history_search


def test_ranker_puts_the_hits_of_history_search_in_the_plugins_order_and_says_so(installed, lab):  # noqa: F811
    two_sessions(lab)
    before = search(lab, q="AssertionError")
    assert before["reranked"] is False and installed.CALLS == []  # nothing enabled: as always
    enable(lab)
    found = search(lab, q="AssertionError")
    assert found["reranked"] is True
    assert evidence(found) == evidence(before)[::-1]
    assert [hit["rank"] for hit in found["hits"]] == [hit["rank"] for hit in before["hits"]][::-1]  # ts_rank kept
    assert (found["total"], found["limit"], found["offset"]) == (before["total"], before["limit"], before["offset"])
    sent = installed.CALLS[-1]
    assert (sent["query"], sent["source"], sent["sink"], sent["project"]) == (
        "AssertionError",
        "history",
        SINK,
        PROJECT,
    )
    assert [hit["id"] for hit in sent["hits"]] == evidence(before)
    assert all(set(hit) == {"id", "text", "source"} for hit in sent["hits"])


def test_ranker_keeps_the_meaning_of_limit_and_offset_of_history_search(installed, lab):  # noqa: F811
    two_sessions(lab)
    enable(lab)
    plain = search(lab, q="AssertionError", limit=1)
    assert plain["reranked"] is False and len(plain["hits"]) == 1 and plain["total"] == 2  # one hit: nothing to rank
    second = search(lab, q="AssertionError", limit=1, offset=1)
    assert len(second["hits"]) == 1 and second["total"] == 2 and second["offset"] == 1
    assert installed.CALLS == []


@pytest.mark.parametrize("mode", ["boom", "junk", "late"])
def test_ranker_that_fails_answers_no_order_or_is_late_leaves_the_order_of_history_search(
    installed,
    lab,
    monkeypatch,
    mode,  # noqa: F811
):
    two_sessions(lab)
    before = search(lab, q="AssertionError")
    enable(lab)
    monkeypatch.setattr(installed, "MODE", mode)
    monkeypatch.setattr(rankers, "BUDGET", 0.3)
    found = search(lab, q="AssertionError")
    assert found["reranked"] is False and evidence(found) == evidence(before)
    assert len(installed.CALLS) == 1


def test_ranker_gets_no_hit_the_sink_may_not_read_and_a_project_without_the_sink_is_not_asked(installed, lab):  # noqa: F811
    two_sessions(lab)
    enable(lab, level="public")  # the transcripts are internal
    found = search(lab, q="AssertionError")
    assert found["reranked"] is False and installed.CALLS == []


def test_ranker_is_not_asked_when_the_project_declares_no_sink(installed, lab):  # noqa: F811
    two_sessions(lab)
    enable(lab, level=None)
    assert search(lab, q="AssertionError")["reranked"] is False and installed.CALLS == []


def mcp_search(lab, query: str) -> dict:
    """The structured result of history_search on /mcp, as the owner's own session reads it."""
    params = {"name": "history_search", "arguments": {"query": query}, "_meta": {"evo-agents/data": True}}
    headers = {**lab.headers["owner"], **MCP_HEADERS, mcp_tools.PROJECT_HEADER: PROJECT}
    response = lab.client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": params}, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]["structuredContent"]


def test_ranker_of_history_search_on_mcp_carries_the_field_and_the_order(installed, lab):  # noqa: F811
    two_sessions(lab)
    plain = mcp_search(lab, "AssertionError")
    assert plain["reranked"] is False
    enable(lab)
    found = mcp_search(lab, "AssertionError")
    assert found["reranked"] is True
    assert [hit["evidence"] for hit in found["hits"]] == [hit["evidence"] for hit in plain["hits"]][::-1]
    assert installed.CALLS[-1]["source"] == "history"


# the order, as rerank() decides it


def _hits(*ids: str) -> list[rankers.RankHit]:
    return [rankers.RankHit(each, f"text of {each}", "history") for each in ids]


def _ranker(handler, sink: str = SINK) -> rankers.Ranker:
    return rankers.Ranker(PLUGIN, "rank", sink, handler, PROJECT, 1)


STATE = SimpleNamespace(engine=None, config=None)


def test_ranker_hits_the_sink_may_not_read_keep_their_places_and_are_never_sent():
    seen = []

    async def handler(params, context):
        seen.append([hit["id"] for hit in params["hits"]])
        return {"order": [hit["id"] for hit in params["hits"]][::-1]}

    ids = ["a", "b", "c", "d", "e"]
    cleared = {SINK: [True, False, True, False, True]}
    order = asyncio.run(rankers.rerank(STATE, [_ranker(handler)], "q", "history", _hits(*ids), cleared))
    assert seen == [["a", "c", "e"]] and order == ["e", "b", "c", "d", "a"]


def test_ranker_ids_it_leaves_out_follow_the_ones_it_names_and_a_sync_handler_runs():
    def handler(params, context):
        return {"order": ["c"]}

    order = asyncio.run(rankers.rerank(STATE, [_ranker(handler)], "q", "kg", _hits("a", "b", "c"), {SINK: [True] * 3}))
    assert order == ["c", "a", "b"]


@pytest.mark.parametrize("answer", [None, {}, {"order": "ab"}, {"order": ["a", "a"]}, {"order": ["z"]}, {"order": [1]}])
def test_ranker_answer_that_is_no_order_changes_nothing(answer):
    async def handler(params, context):
        return answer

    assert (
        asyncio.run(rankers.rerank(STATE, [_ranker(handler)], "q", "kg", _hits("a", "b"), {SINK: [True] * 2})) is None
    )


def test_ranker_budget_covers_every_ranker_of_the_search_together(monkeypatch):
    monkeypatch.setattr(rankers, "BUDGET", 0.3)
    calls = []

    async def slow(params, context):
        calls.append("slow")
        await asyncio.sleep(5)

    async def after(params, context):
        calls.append("after")
        return {"order": ["b", "a"]}

    found = asyncio.run(
        rankers.rerank(STATE, [_ranker(slow), _ranker(after)], "q", "kg", _hits("a", "b"), {SINK: [True] * 2})
    )
    assert found is None and calls == ["slow"]  # the second never got its turn: the search had no time left


def test_ranker_sends_a_hit_cut_and_redacted():
    text = rankers.cut("x " * 1000 + "sk" + "-abcdefghijklmnopqrstuvwxyz0123456789")
    assert len(text) <= rankers.MAX_HIT_CHARS and "sk-abcdefghijkl" not in text


def test_ranker_reorders_the_lines_of_a_kg_search_text_with_their_snippets():
    text = "3 result(s) for 'q'\nno result holds all\n  one\n    > snippet one\n  two\n  three\n    > snippet three"
    assert rankers._reordered_text(text, 3, [2, 0, 1]) == (
        "3 result(s) for 'q'\nno result holds all\n  three\n    > snippet three\n  one\n    > snippet one\n  two"
    )
    assert rankers._reordered_text("1 result", 3, [0, 1, 2]) is None


# kg_search, on the hub: REST tools, /mcp and kg serve --backend hub answer through one function


@pytest.fixture(scope="module")
def graph(pg_server, tmp_path_factory):  # noqa: F811
    """test_kg_api's hub, built once, with northwind-ranker installed but enabled by no project."""
    from fastapi.testclient import TestClient

    from evo_agents.hub.server.app import create_app
    from tests.hub import live
    from tests.hub.kg_fixture import Harness, registration
    from tests.hub.s3 import fake_s3
    from tests.hub.test_kg import InProcessHub, make_config, run_jobs

    patch = pytest.MonkeyPatch()
    patch.syspath_prepend(str(FIXTURES))
    patch.setattr(core, "entry_points", lambda *, group: [RANKER] if group == ENTRY_POINT_GROUP else [])
    plugin_calls.core_registry.cache_clear()
    base = tmp_path_factory.mktemp("kg-ranker")
    db = pg.create_database()
    try:
        with fake_s3() as s3:
            with TestClient(create_app(make_config(db, base / "api", s3))) as client:
                admin = live.bearer(live.insert_token(db, live.ADMIN))
                response = client.put(f"/v1/projects/{KG_PROJECT}", json=registration(KG_PROJECT), headers=admin)
                assert response.status_code == 200, response.text
                grant = {"role": "admin", "max_level": "customer"}
                response = client.put(f"/v1/admin/projects/{KG_PROJECT}/grants/wendy", json=grant, headers=admin)
                assert response.status_code in (200, 201), response.text
                wendy = live.bearer(live.insert_token(db, "wendy"))
                harness = Harness(base / "machine", KG_PROJECT)
                harness.sync()
                assert harness.push(InProcessHub(client, wendy)).build["status"] == "queued"
                run_jobs(db, s3, base / "worker")
                yield SimpleNamespace(client=client, db=db, admin=admin, wendy=wendy)
    finally:
        pg.drop_database(db)
        patch.undo()
        plugin_calls.core_registry.cache_clear()


KG_PROJECT = "kgrank"


def kg_search(graph, query: str, limit: int = 25) -> dict:
    from tests.hub.kg_fixture import AGENT

    response = graph.client.post(
        f"/v1/kg/{KG_PROJECT}/tools/kg_search",
        json={"arguments": {"query": query, "limit": limit}, "sink": AGENT},
        headers=graph.wendy,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_ranker_puts_the_results_of_kg_search_in_the_plugins_order_in_data_and_text(installed, graph):
    plain = kg_search(graph, "note")
    ids = [node["id"] for node in plain["structuredContent"]["results"]]
    assert len(ids) > 3 and "reranked" not in plain["structuredContent"]  # no ranker enabled: as always
    response = graph.client.post(
        f"/v1/projects/{KG_PROJECT}/plugins/{PLUGIN}/enable", json={"permissions": []}, headers=graph.wendy
    )
    assert response.status_code == 200, response.text
    project_id = sql(graph.db, select(tables.projects.c.id).where(tables.projects.c.name == KG_PROJECT))[0][0]
    sql(
        graph.db,
        insert(tables.project_sinks).values(
            project_id=project_id, sink_id=SINK, kind="llm-backend", clearance={"level": "internal"}
        ),
    )
    found = kg_search(graph, "note")
    data = found["structuredContent"]
    cleared = [node["id"] for node in plain["structuredContent"]["results"] if node["label"]["level"] != "customer"]
    sent = [hit["id"] for hit in installed.CALLS[-1]["hits"]]
    assert sent == cleared and installed.CALLS[-1]["source"] == "kg"  # nothing above the sink's clearance goes out
    assert data["reranked"] is True and {n["id"] for n in data["results"]} == set(ids)
    expected = list(ids)
    slots = [i for i, each in enumerate(ids) if each in set(sent)]
    for slot, each in zip(slots, sent[::-1], strict=True):
        expected[slot] = each
    assert [node["id"] for node in data["results"]] == expected
    lines = found["content"][0]["text"].split("\n")
    assert lines[:2] == plain["content"][0]["text"].split("\n")[:2]  # the header is the search's own
    assert [line.split("]")[0].removeprefix("[") for line in lines[2:]] == expected  # the text shows the data's order
    calls = len(installed.CALLS)
    sql(
        graph.db,
        update(tables.project_sinks)
        .where(tables.project_sinks.c.sink_id == SINK)
        .values(clearance={"level": "public"}),
    )
    again = kg_search(graph, "note")["structuredContent"]
    assert again["reranked"] is False and [n["id"] for n in again["results"]] == ids and len(installed.CALLS) == calls

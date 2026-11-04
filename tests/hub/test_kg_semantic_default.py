"""A project registered now starts at semantic mode index, and ``hub kg semantic`` reports what its build recorded.
Fake connections stand in for Postgres."""

import asyncio

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy.dialects import postgresql

from evo_agents.hub import kg_semantic, kg_semantic_cli


class Conn:
    def __init__(self):
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)


def test_a_new_project_gets_a_row_at_index_that_never_overwrites_a_choice():
    conn = Conn()
    asyncio.run(kg_semantic.start(conn, 7))
    statement = conn.statements[0]
    assert statement.table.name == "kg_semantic" and statement.__visit_name__ == "insert"
    assert statement._post_values_clause.__visit_name__ == "on_conflict_do_nothing"
    assert conn.statements[0].compile(dialect=postgresql.dialect()).params["mode"] == "index"


def test_semantic_json_says_mode_model_availability_and_coverage():
    held = {"model": "m", "mode": "index", "rows": 10, "vectors": 4, "partial": True}
    found = kg_semantic_cli.semantic_json("demo", 3, held)
    assert found["mode"] == "index" and found["model_available"] and found["coverage"] == 0.4 and found["partial"]
    failed = kg_semantic_cli.semantic_json("demo", 3, {**held, "vectors": 0, "error": "model m cannot be loaded"})
    assert not failed["model_available"] and failed["error"]
    off = kg_semantic_cli.semantic_json("demo", 3, {})
    assert off["mode"] == "off" and not off["model_available"] and off["coverage"] is None


class PartStore:
    reader = None

    def top_audience(self, b):
        return ["chat:9"]

    def seen(self, b):
        return {"nodes": 2, "edges": 1, "units": 1, "items": {"chat": 1}, "audience": {"chat"}}


def test_a_reader_below_the_build_still_learns_whether_it_embedded_but_not_how_many_rows():
    from evo_agents.hub.kg_graph import HubSession

    session = type("S", (), {"graph_store": PartStore(), "audience": ["chat:1"], "whole": lambda self, b: False})()
    vectors = {"model": "m", "mode": "index", "rows": 10, "vectors": 4, "partial": True}
    summary = {"ready": True, "build_id": 3, "built_at": "t", "content_hash": "h", "stats": {"size": 9}}
    summary["coverage"] = {"sources": [{"id": "chat", "items": 4}], "warnings": [], "vectors": vectors}
    graph = HubSession.within(session, summary)
    assert graph["coverage"]["vectors"] == {"model": "m", "mode": "index"}
    found = kg_semantic_cli.semantic_json("demo", 3, graph["coverage"]["vectors"])
    assert found["model_available"] and found["mode"] == "index" and found["rows"] == 0 and found["coverage"] is None


def test_kg_status_takes_the_vectors_from_the_hubs_record_of_the_build_since_the_graph_file_lacks_them():
    from datetime import UTC, datetime

    from evo_agents.hub.kg_graph import BuiltGraph, HubSession

    vectors = {"model": "m", "mode": "index", "rows": 10, "vectors": 4, "partial": True}
    built = BuiltGraph(3, "0" * 64, 0, "h", 2, 1, datetime.now(UTC), vectors)
    assert built == BuiltGraph(3, "0" * 64, 0, "h", 2, 1, built.finished_at) and hash(built)
    session = type("S", (), {"graph": built})()
    summary = {"ready": True, "build_id": 1, "coverage": {"sources": [], "warnings": []}}
    found = HubSession.recorded(session, summary)
    assert (
        found["coverage"] == {"sources": [], "warnings": [], "vectors": vectors}
        and "vectors" not in summary["coverage"]
    )
    assert kg_semantic_cli.semantic_json("demo", 3, found["coverage"]["vectors"])["model_available"]
    session.graph = BuiltGraph(3, "0" * 64, 0, "h", 2, 1, built.finished_at)  # a build that did not embed
    assert HubSession.recorded(session, summary) is summary
    assert HubSession.recorded(type("S", (), {"graph": built})(), {"ready": False}) == {"ready": False}


def test_the_hub_embeds_for_fifteen_minutes_at_most():
    assert kg_semantic.EMBED_BUDGET_SECONDS == 900.0

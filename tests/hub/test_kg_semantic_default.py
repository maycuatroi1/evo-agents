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


def test_the_hub_embeds_for_fifteen_minutes_at_most():
    assert kg_semantic.EMBED_BUDGET_SECONDS == 900.0

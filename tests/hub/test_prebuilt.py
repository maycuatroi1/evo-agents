"""Writes the hub builds once and runs with bind parameters (docs/hub.md, Data access).

An INSERT or UPDATE takes an execution parameter named after a column of its table as a value to set. SQLAlchemy
refuses a bind parameter with a column's name in a plain INSERT or UPDATE, but not in one inside a CTE: a parameter
named kind in the token check, whose CTE touches tokens, would set tokens.kind. Each statement built once that writes
compiles to the same SQL whether or not the execution names its parameters.
"""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from sqlalchemy.dialects import postgresql

from evo_agents.hub.server import audit, memories, security

DIALECT = postgresql.psycopg.dialect()
WRITES = {
    "token check": security.AUTHENTICATE,
    "memory change": memories._CHANGED,
    "memory revision": memories._REVISION,
    "audit row in a project by id": audit._IN_PROJECT,
    "audit row in a project by name": audit._IN_NAMED,
    "audit row of a memory": audit._OF_MEMORY,
}


@pytest.mark.parametrize("name", sorted(WRITES))
def test_a_parameter_of_a_write_built_once_sets_no_column(name):
    statement = WRITES[name]
    plain = statement.compile(dialect=DIALECT)
    passed = [key for key, value in plain.params.items() if value is None]  # those each execution gives
    assert passed, "the statement takes no parameter at execution"
    assert statement.compile(dialect=DIALECT, column_keys=passed).string == plain.string

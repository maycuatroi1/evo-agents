"""No SQL in the hub's Python: queries are SQLAlchemy Core on the tables of ``evo_agents.hub.tables``.

The guard reads every module under evo_agents/hub and tests/hub and reports, by file and line:

- a string constant, or the static text of an f-string, holding uppercase SQL keywords in the shape a statement
  gives them (``SELECT ... FROM``, ``WHERE``, ``RETURNING``, ``INSERT INTO``, ``SAVEPOINT x`` and the like); a
  docstring is not code and is left alone, and a lone word such as ``"DELETE"`` (an HTTP method) or
  ``ondelete="CASCADE"`` is not SQL;
- an import or use of ``psycopg.sql`` or of SQLAlchemy's ``text``, and any ``exec_driver_sql``;
- a call of ``driver(``, the bridge to the raw psycopg connection of a transaction (``evo_agents.hub.db``), and any
  other reach for that connection (``get_raw_connection``, ``driver_connection``) outside ``evo_agents.hub.db``.

A few places keep SQL for good, each for a reason no query builder changes: the migrations that have run
(MIGRATIONS_RUN), the LISTEN of the listener and the test databases' CREATE and DROP (ALLOWED_SQL,
ALLOWED_PSYCOPG_SQL), and procrastinate's connection (ALLOWED_DRIVER). LEGACY lists the files that still hold SQL
while the hub moves to Core, each with the number of findings it may keep: the number only goes down, a file whose
findings rise or fall fails until the number says so, and a file left with none fails until it leaves the list.
docs/hub.md, section "Data access", says how to write the query instead.

Standard library only: this runs on a core install, without SQLAlchemy or psycopg.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCANNED = ("evo_agents/hub", "tests/hub")
GUIDE = "docs/hub.md, section Data access"

# Uppercase SQL keywords in the shape they take in a statement. Lowercase prose, log messages and identifiers do
# not match; nor does a single keyword on its own.
SQL = re.compile(
    r"""
    ^\s*(?:SELECT|INSERT|UPDATE|DELETE|WITH|CREATE|ALTER|DROP|TRUNCATE|SAVEPOINT|ROLLBACK|RELEASE|LISTEN|UNLISTEN
          |NOTIFY|SET|RESET|SHOW|BEGIN|COMMIT|COPY|LOCK|GRANT|REVOKE|VACUUM|ANALYZE|EXPLAIN|VALUES|PRAGMA)\s+[\w(*'"%{]
    | \bSELECT\b[\s\S]*\bFROM\b
    | \bINSERT\s+INTO\b
    | \bDELETE\s+FROM\b
    | \bUPDATE\s+\w+\s+SET\b
    | \b(?:WHERE|RETURNING|HAVING)\b
    | \bON\s+CONFLICT\b
    | \bFOR\s+(?:NO\s+KEY\s+)?(?:UPDATE|SHARE)\b
    | \b(?:GROUP|ORDER|PARTITION)\s+BY\b
    | \bJOIN\b
    | \bUNION\b
    | \bIS\s+(?:NOT\s+)?(?:NULL|TRUE|FALSE|DISTINCT)\b
    | \b(?:AND|OR)\s+(?:NOT\s+)?\(?\w+(?:\.\w+)?\s*(?:=|<>|!=|<=|>=|<|>|@>|\?|IS\b|IN\b|LIKE\b|ILIKE\b|BETWEEN\b)
    | (?:=|<>|!=)\s*(?:ANY|ALL)\s*\(
    | \b\w+\s+(?:NOT\s+)?IN\s*\(
    | \bNOT\s+EXISTS\b
    | \bCASE\s+WHEN\b
    | \b(?:LIMIT|OFFSET)\s+(?:%|:|\d|\{|ALL\b)
    | %(?:\(\w+\))?s::\w+
    | \bVALUES\s*\(
    """,
    re.VERBOSE,
)
# Placeholder for each value an f-string interpolates, so its static text still reads as one statement.
HOLE = "x"

MIGRATIONS_RUN = (
    "0001_initial.py",
    "0002_project_paths.py",
    "0003_memory_bounds.py",
    "0004_blobs_and_jobs.py",
    "0005_skill_bundles.py",
    "0006_kg_builds.py",
    "0007_audit_project.py",
    "0008_kg_artifact_retention.py",
    "0009_workers.py",
    "0010_plan_runs.py",
    "0011_credentials.py",
)
# Migrations that have run: each is the record of how databases at its revision were built, so the guard leaves
# them as they are. A new migration is written with Alembic's operations on evo_agents.hub.tables.
MIGRATIONS_RUN_PATHS = frozenset(f"evo_agents/hub/migrations/versions/{name}" for name in MIGRATIONS_RUN)
# The SQL other files keep for good, with the reason. A change that only raw SQL can make adds its place here, in
# the same pull request.
ALLOWED_SQL: dict[str, re.Pattern] = {
    # LISTEN has no SQLAlchemy construct; the listener keeps a psycopg connection of its own for it.
    "evo_agents/hub/server/listen.py": re.compile(r"^LISTEN\b"),
    # A test's database and its owner role, made and dropped by a superuser outside any hub database.
    "tests/hub/pg.py": re.compile(r"^(?:CREATE|DROP)\s+(?:DATABASE|ROLE)\b"),
}
ALLOWED_PSYCOPG_SQL = frozenset({"evo_agents/hub/server/listen.py", "tests/hub/pg.py"})  # quotes their identifiers
ALLOWED_DRIVER = frozenset(
    {
        "evo_agents/hub/jobs.py",  # procrastinate defers a job on the psycopg connection of the caller's transaction
        "tests/hub/test_db_bridge.py",  # the test of the bridge itself
    }
)
RAW_CONNECTION = frozenset({"get_raw_connection", "driver_connection"})
RAW_CONNECTION_HOME = "evo_agents/hub/db.py"  # where driver() is defined

# Files that still hold SQL, and how many findings each may keep. Only goes down; empty, it goes away.
LEGACY: dict[str, int] = {
    "evo_agents/hub/blob_gc.py": 10,
    "evo_agents/hub/db.py": 3,
    "evo_agents/hub/kg_build.py": 19,
    "evo_agents/hub/kg_graph.py": 1,
    "evo_agents/hub/kg_prune.py": 4,
    "evo_agents/hub/kg_web.py": 1,
    "evo_agents/hub/migrate.py": 8,
    "evo_agents/hub/server/admin.py": 17,
    "evo_agents/hub/server/admin_console.py": 10,
    "evo_agents/hub/server/audit.py": 1,
    "evo_agents/hub/server/auth.py": 10,
    "evo_agents/hub/server/blobs.py": 9,
    "evo_agents/hub/server/credentials.py": 21,
    "evo_agents/hub/server/decisions.py": 18,
    "evo_agents/hub/server/kg.py": 18,
    "evo_agents/hub/server/mcp.py": 1,
    "evo_agents/hub/server/memories.py": 16,
    "evo_agents/hub/server/notifications.py": 16,
    "evo_agents/hub/server/overview.py": 9,
    "evo_agents/hub/server/plans.py": 10,
    "evo_agents/hub/server/projects.py": 19,
    "evo_agents/hub/server/run_events.py": 15,
    "evo_agents/hub/server/run_state.py": 27,
    "evo_agents/hub/server/runs.py": 41,
    "evo_agents/hub/server/secrets.py": 11,
    "evo_agents/hub/server/security.py": 3,
    "evo_agents/hub/server/skills.py": 12,
    "evo_agents/hub/server/terminal.py": 4,
    "evo_agents/hub/server/tokens.py": 1,
    "evo_agents/hub/server/workers.py": 23,
    "evo_agents/hub/worker.py": 1,
    "tests/hub/conftest.py": 1,
    "tests/hub/live.py": 8,
    "tests/hub/pg.py": 4,
    "tests/hub/test_admin_api.py": 17,
    "tests/hub/test_admin_overview.py": 13,
    "tests/hub/test_auth.py": 23,
    "tests/hub/test_blobs.py": 10,
    "tests/hub/test_credentials_api.py": 10,
    "tests/hub/test_decisions.py": 16,
    "tests/hub/test_hooks.py": 6,
    "tests/hub/test_kg.py": 25,
    "tests/hub/test_kg_retention.py": 14,
    "tests/hub/test_mcp.py": 9,
    "tests/hub/test_memory.py": 17,
    "tests/hub/test_memory_api.py": 2,
    "tests/hub/test_migrate.py": 68,
    "tests/hub/test_notifications.py": 12,
    "tests/hub/test_overview.py": 7,
    "tests/hub/test_plan_runs.py": 18,
    "tests/hub/test_plans.py": 3,
    "tests/hub/test_plans_api.py": 2,
    "tests/hub/test_projects.py": 9,
    "tests/hub/test_run_stats.py": 5,
    "tests/hub/test_run_stream.py": 12,
    "tests/hub/test_run_tables.py": 219,
    "tests/hub/test_runs.py": 39,
    "tests/hub/test_sealing.py": 88,
    "tests/hub/test_secrets_api.py": 17,
    "tests/hub/test_secrets_cli.py": 1,
    "tests/hub/test_skills.py": 11,
    "tests/hub/test_skills_api.py": 1,
    "tests/hub/test_terminal.py": 5,
    "tests/hub/test_web_auth.py": 8,
    "tests/hub/test_wheel.py": 2,
    "tests/hub/test_worker.py": 8,
    "tests/hub/test_workers.py": 32,
}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    what: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.what}"


def _excerpt(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= 80 else flat[:77] + "..."


class _Scanner(ast.NodeVisitor):
    def __init__(self, path: str):
        self.path = path
        self.findings: list[Finding] = []
        self.allowed_sql: re.Pattern | None = ALLOWED_SQL.get(path)  # the SQL this file may keep for good
        self.sqlalchemy_names: set[str] = set()  # local names of the sqlalchemy and sqlalchemy.sql modules
        self.psycopg_names: set[str] = set()  # local names of the psycopg module

    def _add(self, node: ast.AST, what: str) -> None:
        self.findings.append(Finding(self.path, node.lineno, what))

    def _text(self, node: ast.AST, text: str) -> None:
        if not SQL.search(text) or (self.allowed_sql and self.allowed_sql.search(text.strip())):
            return
        self._add(node, f"SQL in a string: {_excerpt(text)!r}")

    def visit_Expr(self, node: ast.Expr) -> None:
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return  # a docstring, or another bare string: not code
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self._text(node, node.value)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append(HOLE)
                if isinstance(value, ast.FormattedValue):
                    self.visit(value.value)
        self._text(node, "".join(parts))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "psycopg.sql" or alias.name.startswith("psycopg.sql."):
                self._psycopg_sql(node, "imports psycopg.sql")
            elif alias.name in ("sqlalchemy", "sqlalchemy.sql"):
                self.sqlalchemy_names.add(alias.asname or alias.name.split(".")[0])
            elif alias.name == "psycopg":
                self.psycopg_names.add(alias.asname or "psycopg")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        names = {alias.name for alias in node.names}
        if module == "psycopg.sql" or module.startswith("psycopg.sql.") or (module == "psycopg" and "sql" in names):
            self._psycopg_sql(node, "imports psycopg.sql")
        if module.split(".")[0] == "sqlalchemy" and "text" in names:
            self._add(node, f"imports text from {module}, which runs a SQL string")
        if module == "sqlalchemy" and "sql" in names:
            self.sqlalchemy_names.update(alias.asname or "sql" for alias in node.names if alias.name == "sql")

    def _psycopg_sql(self, node: ast.AST, what: str) -> None:
        if self.path not in ALLOWED_PSYCOPG_SQL:
            self._add(node, f"{what}, which builds SQL strings")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        base = node.value.id if isinstance(node.value, ast.Name) else None
        if node.attr == "exec_driver_sql":
            self._add(node, "exec_driver_sql runs a SQL string")
        elif node.attr in RAW_CONNECTION and self.path != RAW_CONNECTION_HOME:
            self._add(node, f"{node.attr} reaches the raw psycopg connection, which only evo_agents.hub.db does")
        elif node.attr == "text" and base in self.sqlalchemy_names:
            self._add(node, f"{base}.text runs a SQL string")
        elif node.attr == "sql" and base in self.psycopg_names:
            self._psycopg_sql(node, "uses psycopg.sql")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        if name == "driver" and self.path not in ALLOWED_DRIVER:
            self._add(node, "driver() hands out the raw psycopg connection, which only evo_agents.hub.jobs takes")
        self.generic_visit(node)


def scan_source(path: str, source: str) -> list[Finding]:
    """What the guard reports in ``source``, the text of the module at ``path`` (relative to the repository)."""
    if path in MIGRATIONS_RUN_PATHS:
        return []  # a migration that has run stays as it ran
    scanner = _Scanner(path)
    scanner.visit(ast.parse(source, filename=path))
    return scanner.findings


def scan(path: Path) -> list[Finding]:
    return scan_source(path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8"))


def modules() -> list[Path]:
    found = []
    for top in SCANNED:
        found += [p for p in (ROOT / top).rglob("*.py") if "__pycache__" not in p.parts]
    return sorted(found)


def _report(findings: list[Finding]) -> str:
    lines = "\n".join(f"  {finding}" for finding in findings)
    return f"SQL outside the allowed places; write it with SQLAlchemy Core ({GUIDE}):\n{lines}"


def test_the_hub_holds_no_sql_outside_the_allowed_places():
    findings = [f for path in modules() if path.relative_to(ROOT).as_posix() not in LEGACY for f in scan(path)]
    assert not findings, _report(findings)


def test_legacy_only_shrinks():
    wrong = []
    for path, allowed in sorted(LEGACY.items()):
        if not (ROOT / path).is_file():
            wrong.append(f"  {path}: no such file; take it out of LEGACY")
            continue
        found = scan(ROOT / path)
        if not found:
            wrong.append(f"  {path}: no SQL left; take it out of LEGACY")
        elif len(found) > allowed:
            wrong.append(f"  {path}: {len(found)} findings, LEGACY allows {allowed}; new SQL ({GUIDE}):")
            wrong += [f"    {finding}" for finding in found]
        elif len(found) < allowed:
            wrong.append(
                f"  {path}: {len(found)} findings left; lower its LEGACY number from {allowed} to {len(found)}"
            )
    assert not wrong, "LEGACY is out of date:\n" + "\n".join(wrong)


def test_the_places_allowed_for_good_exist():
    for path in [*MIGRATIONS_RUN_PATHS, *ALLOWED_SQL, *ALLOWED_PSYCOPG_SQL, *ALLOWED_DRIVER, RAW_CONNECTION_HOME]:
        assert (ROOT / path).is_file(), f"{path} is allowed in tests/test_no_raw_sql.py but does not exist"


@pytest.mark.parametrize(
    "source",
    [
        'QUERY = "SELECT id FROM runs WHERE state = %s"',
        'conn.execute("UPDATE runs SET state = %s WHERE id = %s", (state, run))',
        'conn.execute(f"DELETE FROM {table} WHERE id = %s", (row,))',
        'sql = f"UPDATE runs SET {columns} WHERE id = %s"',
        'where = " AND kind = %s"',
        'clause = "state = ANY(%s)"',
        'conn.execute("SAVEPOINT hub_defer")',
        'conn.execute("INSERT INTO audit (action) VALUES (%s) RETURNING id", (a,))',
        'conn.execute("SELECT count(*) FILTER (WHERE x) FROM t")',
        'rows = "SELECT unnest(%s::bigint[])"',
        'lock = "SELECT 1 FROM runs FOR UPDATE SKIP LOCKED"',
        "from psycopg import sql",
        "import psycopg.sql",
        "from psycopg.sql import SQL, Identifier",
        "import psycopg\npsycopg.sql.SQL('x')",
        "from sqlalchemy import text",
        "import sqlalchemy as sa\nsa.text('x')",
        "await conn.exec_driver_sql('x')",
        "raw = await driver(conn)",
        "raw = await db.driver(conn)",
        "raw = (await conn.get_raw_connection()).driver_connection",
    ],
)
def test_the_guard_reports_sql_in_each_shape(source):
    assert scan_source("evo_agents/hub/server/example.py", source)


@pytest.mark.parametrize(
    "source",
    [
        '"""A docstring may say SELECT ... FOR UPDATE SKIP LOCKED: it is not code."""',
        'def f():\n    """Locks the row with SELECT ... FOR UPDATE, then UPDATE runs SET state."""',
        'response = client.request("DELETE", "/v1/runs/1")',
        'fk = ForeignKey("users.id", ondelete="CASCADE")',
        'log.info("run deleted", extra={"state": "DELETE"})',
        'message = "the run is queued; select a worker from the list"',
        "stmt = select(runs.c.id).where(runs.c.state == 'queued').with_for_update(skip_locked=True)",
        'HELP = "Update the plan, then select the step to run."',
    ],
)
def test_the_guard_leaves_prose_and_core_alone(source):
    assert scan_source("evo_agents/hub/server/example.py", source) == []


def test_the_allowed_places_keep_only_what_they_are_allowed():
    listen = "evo_agents/hub/server/listen.py"
    assert scan_source(listen, 'from psycopg import sql\nsql.SQL("LISTEN {}")') == []
    assert scan_source(listen, 'conn.execute("SELECT 1 FROM runs WHERE id = %s")')
    pg = "tests/hub/pg.py"
    assert scan_source(pg, 'sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}")\nsql.SQL("DROP DATABASE IF EXISTS {}")') == []
    assert scan_source(pg, 'conn.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = %s")')
    migration = "evo_agents/hub/migrations/versions/0001_initial.py"
    assert scan_source(migration, 'op.execute("CREATE TABLE users (id bigint)")') == []
    assert scan_source("evo_agents/hub/migrations/versions/0012_next.py", 'op.execute("CREATE TABLE t (id bigint)")')
    assert scan_source("evo_agents/hub/jobs.py", "raw = await driver(conn)") == []
    assert scan_source("evo_agents/hub/db.py", "raw = (await conn.get_raw_connection()).driver_connection") == []

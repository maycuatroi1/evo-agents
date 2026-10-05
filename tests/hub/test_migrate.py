"""Migrations: a fresh database gets the twenty-four hub tables, a second run changes nothing, processes that start
together apply each revision once, a database at 0001 with rows in it moves to 0002, and a database the code
cannot read is refused. Then the constraints schemas 0001 and 0002 promise."""

import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from alembic import command
from alembic.script import ScriptDirectory
from psycopg import errors
from psycopg.types.json import Jsonb

from evo_agents.hub import migrate as hub_migrate
from evo_agents.hub.migrate import LOCK_KEY, MigrationError, alembic_config, head_revision, migrate, revisions

TABLES = {
    "users",
    "tokens",
    "projects",
    "project_repos",
    "project_sinks",
    "grants",
    "memories",
    "memory_revisions",
    "plans",
    "plan_revisions",
    "skills",
    "skill_versions",
    "kg_ingests",
    "audit",
}
TABLES |= pg.BLOB_TABLES | pg.QUEUE_TABLES  # migration 0004
TABLES |= pg.KG_TABLES  # migration 0006
TABLES |= pg.RETENTION_TABLES  # migration 0008
ALL = revisions()  # every revision the package ships, in order
HEAD = ALL[-1]
SNAPSHOT = """
SELECT 'column', table_name || '.' || column_name || ' ' || data_type || ' ' || is_nullable || ' '
       || coalesce(column_default, '')
  FROM information_schema.columns WHERE table_schema = 'public'
UNION ALL
SELECT 'constraint', conrelid::regclass::text || ' ' || conname || ' ' || pg_get_constraintdef(oid)
  FROM pg_constraint WHERE connamespace = 'public'::regnamespace
UNION ALL
SELECT 'index', indexdef FROM pg_indexes WHERE schemaname = 'public'
UNION ALL
SELECT 'trigger', tgrelid::regclass::text || ' ' || tgname FROM pg_trigger WHERE NOT tgisinternal
UNION ALL
SELECT 'revision', version_num FROM alembic_version
ORDER BY 1, 2
"""


def query(db, sql, params=()):
    with pg.admin(db.admin_dsn) as conn:
        return conn.execute(sql, params).fetchall()


def tables(db) -> set[str]:
    return {
        row[0] for row in query(db, "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
    }


def test_a_fresh_database_gets_the_twenty_four_tables(hub_db):
    assert len(TABLES) == 24  # the name of this test counts them: a new table renames it
    result = migrate(hub_db.dsn)
    assert result.before == ()
    assert result.applied == ALL and result.after == (head_revision(),) == (HEAD,)
    assert tables(hub_db) == TABLES | {"alembic_version"}
    assert query(hub_db, "SELECT version_num FROM alembic_version") == [(HEAD,)]
    # skills and skill_versions keep metadata and the blob key, never the bundle's bytes
    sql = "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public' AND {}"
    assert query(hub_db, sql.format("data_type = 'bytea'")) == []
    assert {row[1] for row in query(hub_db, sql.format("table_name = 'skill_versions'"))} >= {
        "sha256",
        "size",
        "r2_key",
    }


def test_a_second_migrate_changes_nothing(hub_db):
    migrate(hub_db.dsn)
    before = query(hub_db, SNAPSHOT)
    result = migrate(hub_db.dsn)
    assert (result.before, result.applied, result.after) == ((HEAD,), (), (HEAD,))
    assert query(hub_db, SNAPSHOT) == before

    run = pg.cli(["hub", "migrate"], env=pg.clean_env(EVO_HUB_DSN=hub_db.dsn))
    assert run.returncode == 0, run.stderr
    messages = [line["msg"] for line in pg.log_lines(run.stderr)]
    assert "schema already at head" in messages
    assert not any(m.startswith("Running upgrade") for m in messages)
    assert query(hub_db, SNAPSHOT) == before


def _waiting_on_lock(db) -> int:
    return query(
        db,
        "SELECT count(*) FROM pg_locks l JOIN pg_database d ON d.oid = l.database "
        "WHERE l.locktype = 'advisory' AND NOT l.granted AND d.datname = %s",
        (db.name,),
    )[0][0]


def outcome(lines: list[dict]) -> dict:
    """The line where a migrate process says what it did."""
    (line,) = [line for line in lines if line["msg"] in ("migrations applied", "schema already at head")]
    return line


def test_two_processes_migrating_at_once_both_exit_zero_and_apply_once(hub_db):
    env = pg.clean_env(EVO_HUB_DSN=hub_db.dsn)
    # Hold the lock until both processes wait on it, so they really race for it when it is released.
    holder = pg.admin(hub_db.admin_dsn)
    try:
        holder.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        procs = [
            subprocess.Popen(
                [sys.executable, "-m", "evo_agents", "hub", "migrate"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        deadline = time.monotonic() + 60
        while _waiting_on_lock(hub_db) < 2:
            assert time.monotonic() < deadline, "the two migrate processes never queued on the lock"
            assert all(p.poll() is None for p in procs), "a migrate process exited before taking the lock"
            time.sleep(0.1)
    finally:
        holder.close()
    outputs = [p.communicate(timeout=60) for p in procs]
    assert [p.returncode for p in procs] == [0, 0], [err for _, err in outputs]

    logs = [pg.log_lines(err) for _, err in outputs]
    upgrades = [line for lines in logs for line in lines if line["msg"].startswith("Running upgrade")]
    assert len(upgrades) == len(ALL)  # one line per revision, all from the process that took the lock first
    outcomes = [outcome(lines) for lines in logs]
    assert sorted(line.get("applied", []) for line in outcomes) == [[], list(ALL)]
    assert all(any(line["msg"].startswith("waiting for the migration lock") for line in lines) for lines in logs)
    assert query(hub_db, "SELECT version_num FROM alembic_version") == [(HEAD,)]
    assert tables(hub_db) == TABLES | {"alembic_version"}


def test_a_database_at_an_unknown_revision_is_refused(hub_db):
    migrate(hub_db.dsn)
    query(hub_db, "UPDATE alembic_version SET version_num = '9999' RETURNING version_num")
    before = query(hub_db, SNAPSHOT)
    with pytest.raises(
        MigrationError, match=rf"revision 9999, which evo-agents .* does not know \(its newest is {HEAD}\)"
    ):
        migrate(hub_db.dsn)
    run = pg.cli(["hub", "migrate", "--dsn", hub_db.dsn], env=pg.clean_env())
    assert run.returncode == 1
    (error,) = [line for line in pg.log_lines(run.stderr) if line["level"] == "error"]
    assert "9999" in error["msg"] and hub_db.password not in run.stderr
    assert query(hub_db, SNAPSHOT) == before


def test_waiting_for_the_lock_is_bounded(hub_db):
    with pg.admin(hub_db.admin_dsn) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        started = time.monotonic()
        with pytest.raises(MigrationError, match="held the migration lock for more than 1s"):
            migrate(hub_db.dsn, lock_timeout=1)
        assert time.monotonic() - started < 15
    assert "alembic_version" not in tables(hub_db)


def test_an_unreachable_database_fails_the_command_without_its_password():
    dsn = f"postgresql://hub:Unreachable-Secret-7@127.0.0.1:{pg.free_port()}/hub"
    run = pg.cli(["hub", "migrate"], env=pg.clean_env(EVO_HUB_DSN=dsn))
    assert run.returncode == 1
    (error,) = [line for line in pg.log_lines(run.stderr) if line["level"] == "error"]
    assert error["msg"].startswith("migration failed: OperationalError")
    assert error["db"].startswith("postgresql://hub:***@127.0.0.1:")
    assert "Unreachable-Secret-7" not in run.stderr


def test_revisions_form_one_chain_of_numbered_files():
    script = ScriptDirectory.from_config(alembic_config())
    chain = list(reversed(list(script.walk_revisions())))
    assert script.get_heads() == [head_revision()]
    assert tuple(rev.revision for rev in chain) == ALL
    previous = None
    for number, rev in enumerate(chain, start=1):
        assert rev.revision == f"{number:04d}"
        assert re.fullmatch(rf"{rev.revision}_[a-z0-9_]+\.py", Path(rev.path).name)
        assert rev.down_revision == previous
        previous = rev.revision


def move_to(db, revision: str, *, down: bool = False) -> None:
    """Upgrade or downgrade the database to ``revision`` with Alembic, as ``migrate`` drives it."""
    engine = hub_migrate._engine(db.dsn)
    try:
        with engine.begin() as conn:
            config = alembic_config()
            config.attributes["connection"] = conn
            (command.downgrade if down else command.upgrade)(config, revision)
    finally:
        engine.dispose()


def columns(db, table: str) -> set[str]:
    sql = "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s"
    return {row[0] for row in query(db, sql, (table,))}


def test_0002_adds_the_harness_paths_to_a_database_holding_projects(hub_db):
    move_to(hub_db, "0001")
    with pg.admin(hub_db.admin_dsn) as conn:
        ids = seed(conn)
        conn.execute("INSERT INTO project_repos (project_id, name) VALUES (%s, 'app')", (ids["project"],))
    move_to(hub_db, "0002")
    assert query(hub_db, "SELECT version_num FROM alembic_version") == [("0002",)]
    assert {"cluster", "workspace", "harness_path"} <= columns(hub_db, "projects")
    assert "path" in columns(hub_db, "project_repos")
    # Projects registered before 0002 keep their rows, without paths, which `hub registry pull` skips.
    assert query(hub_db, "SELECT name, cluster, workspace, harness_path FROM projects") == [("demo", None, None, None)]
    assert query(hub_db, "SELECT name, path FROM project_repos") == [("app", None)]

    move_to(hub_db, "0001", down=True)
    assert not {"cluster", "workspace", "harness_path"} & columns(hub_db, "projects")
    assert query(hub_db, "SELECT name FROM projects") == [("demo",)]
    assert query(hub_db, "SELECT name FROM project_repos") == [("app",)]

    # From 0001 with rows in it, migrate runs 0002 and every later revision, and says so.
    result = migrate(hub_db.dsn)
    assert (result.before, result.applied, result.after) == (("0001",), ALL[ALL.index("0002") :], (HEAD,))


BUILD = (
    "INSERT INTO kg_builds (project_id, status, artifact_sha256, artifact_size, content_hash, nodes, edges, "
    "started_at, finished_at) VALUES (%s, 'succeeded', %s, 10, %s, 1, 0, now(), now()) RETURNING id"
)


def test_0008_keeps_the_builds_and_going_back_fails_the_pruned_ones(hub_db):
    move_to(hub_db, "0007")
    with pg.admin(hub_db.admin_dsn) as conn:
        ids = seed(conn)
        first = one(conn, BUILD, ids["project"], "a" * 64, "sha256:" + "c" * 64)
        second = one(conn, BUILD, ids["project"], "b" * 64, "sha256:" + "c" * 64)
    move_to(hub_db, "0008")
    assert {"artifact_reused_from", "artifact_pruned_at"} <= columns(hub_db, "kg_builds")
    with pg.admin(hub_db.admin_dsn) as conn:
        conn.execute("UPDATE kg_builds SET artifact_reused_from = %s WHERE id = %s", (first, second))
        conn.execute("UPDATE kg_builds SET artifact_sha256 = NULL, artifact_pruned_at = now() WHERE id = %s", (first,))
        for bad in (
            "UPDATE kg_builds SET artifact_pruned_at = NULL WHERE id = {first}",  # succeeded without any artifact
            "UPDATE kg_builds SET artifact_pruned_at = now() WHERE id = {second}",  # pruned yet holding one
            "UPDATE kg_builds SET artifact_reused_from = id WHERE id = {second}",
            "INSERT INTO blob_deletions (sha256, size, kind) VALUES ('not-a-hash', 1, 'kg-graph')",
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(bad.format(first=first, second=second))
        conn.execute("INSERT INTO blob_deletions (sha256, size, kind) VALUES (%s, 10, 'kg-graph')", ("a" * 64,))

    move_to(hub_db, "0007", down=True)
    assert "blob_deletions" not in tables(hub_db)
    builds = query(hub_db, "SELECT id, status, artifact_sha256, error IS NOT NULL FROM kg_builds ORDER BY id")
    assert builds == [(first, "failed", None, True), (second, "succeeded", "b" * 64, False)]
    assert migrate(hub_db.dsn).applied == ("0008",)


def test_downgrade_to_base_removes_everything_and_upgrade_restores_it(hub_db):
    migrate(hub_db.dsn)
    move_to(hub_db, "base", down=True)
    assert tables(hub_db) == {"alembic_version"}
    assert query(hub_db, "SELECT count(*) FROM pg_proc WHERE proname = 'hub_reject_update'") == [(0,)]
    assert migrate(hub_db.dsn).applied == ALL
    assert tables(hub_db) == TABLES | {"alembic_version"}


@pytest.fixture
def schema(hub_db):
    """A migrated database and a superuser connection to it, in autocommit."""
    migrate(hub_db.dsn)
    with pg.admin(hub_db.admin_dsn) as conn:
        yield conn


def one(conn, sql, *params):
    return conn.execute(sql, params).fetchone()[0]


def seed(conn) -> dict:
    ids = {"user": one(conn, "INSERT INTO users (login, github_id) VALUES ('Octo', 1) RETURNING id")}
    ids["token"] = one(
        conn,
        "INSERT INTO tokens (user_id, kind, token_hash, host, expires_at) "
        "VALUES (%s, 'machine', %s, 'mac', now() + interval '90 days') RETURNING id",
        ids["user"],
        "a" * 64,
    )
    ids["project"] = one(
        conn,
        "INSERT INTO projects (name, levels, locations, default_label, created_by) "
        "VALUES ('demo', %s, %s, %s, %s) RETURNING id",
        ["public", "internal"],
        ["any"],
        Jsonb({"level": "internal"}),
        ids["user"],
    )
    ids["memory"] = one(
        conn,
        "INSERT INTO memories (scope, project_id, location, name, type, owner_id, label, body, updated_by) "
        "VALUES ('project', %s, 'harness', 'note.md', 'project', %s, %s, 'body text', %s) RETURNING id",
        ids["project"],
        ids["user"],
        Jsonb({"level": "internal"}),
        ids["user"],
    )
    conn.execute(
        "INSERT INTO memory_revisions (memory_id, revision, type, label, body, deleted, actor_id) "
        "VALUES (%s, 1, 'project', %s, 'body text', false, %s)",
        (ids["memory"], Jsonb({"level": "internal"}), ids["user"]),
    )
    ids["skill"] = one(
        conn,
        "INSERT INTO skills (scope, name, created_by) VALUES ('global', 'stop-slop', %s) RETURNING id",
        ids["user"],
    )
    conn.execute(
        "INSERT INTO audit (actor_id, token_id, action, target) VALUES (%s, %s, 'memory.put', 'demo/note.md')",
        (ids["user"], ids["token"]),
    )
    return ids


@pytest.mark.parametrize(
    "sql, error",
    [
        # identity
        ("INSERT INTO users (login) VALUES ('octo')", errors.UniqueViolation),
        (
            "INSERT INTO tokens (user_id, kind, token_hash, host, expires_at) "
            "VALUES ({user}, 'machine', repeat('a', 64), 'pc', now() + interval '1 day')",
            errors.UniqueViolation,
        ),
        (
            "INSERT INTO memories (scope, project_id, location, name, type, owner_id, label, body, updated_by) "
            "VALUES ('project', {project}, 'harness', 'note.md', 'reference', {user}, '{{}}', 'x', {user})",
            errors.UniqueViolation,
        ),
        ("INSERT INTO skills (scope, name, created_by) VALUES ('global', 'stop-slop', {user})", errors.UniqueViolation),
        # enum-like columns and formats
        (
            "INSERT INTO tokens (user_id, kind, token_hash, host, expires_at) "
            "VALUES ({user}, 'cli', repeat('b', 64), 'pc', now() + interval '1 day')",
            errors.CheckViolation,
        ),
        (
            "INSERT INTO tokens (user_id, kind, token_hash, host, expires_at) "
            "VALUES ({user}, 'machine', 'evh_plaintext', 'pc', now() + interval '1 day')",
            errors.CheckViolation,
        ),
        (
            "INSERT INTO tokens (user_id, kind, token_hash, expires_at) "
            "VALUES ({user}, 'machine', repeat('c', 64), now() + interval '1 day')",
            errors.CheckViolation,
        ),
        (
            "INSERT INTO grants (user_id, project_id, role, max_level, granted_by) "
            "VALUES ({user}, {project}, 'owner', 'internal', {user})",
            errors.CheckViolation,
        ),
        (
            "INSERT INTO memories (scope, location, name, type, owner_id, label, body, updated_by) "
            "VALUES ('project', 'harness', 'x.md', 'project', {user}, '{{}}', 'x', {user})",
            errors.CheckViolation,
        ),
        (
            "INSERT INTO project_sinks (project_id, sink_id, kind, clearance) "
            "VALUES ({project}, 's', 'printer', '{{\"level\": \"internal\"}}')",
            errors.CheckViolation,
        ),
        (
            "INSERT INTO skill_versions (skill_id, version, name, sha256, size, r2_key, published_by) "
            "VALUES ({skill}, 1, 'stop-slop', repeat('d', 64), 10, 'bundles/other', {user})",
            errors.CheckViolation,
        ),
        (
            "INSERT INTO skill_versions (skill_id, version, name, sha256, size, r2_key, published_by) "
            "VALUES ({skill}, 1, 'stop-slop', repeat('d', 64), 10485761, 'blobs/sha256/' || repeat('d', 64), {user})",
            errors.CheckViolation,
        ),
        # history is never edited, and keeps its authors
        ("UPDATE memory_revisions SET body = 'edited'", errors.RestrictViolation),
        ("UPDATE audit SET target = 'edited'", errors.RestrictViolation),
        ("DELETE FROM users WHERE id = {user}", errors.ForeignKeyViolation),
        ("DELETE FROM projects WHERE id = {project}", errors.ForeignKeyViolation),
        # a project has one hub sink, and its harness paths all or none (0002)
        (
            "INSERT INTO project_sinks (project_id, sink_id, kind, clearance) VALUES "
            "({project}, 'hub', 'hub', '{{\"level\": \"internal\"}}'), "
            "({project}, 'hub-2', 'hub', '{{\"level\": \"public\"}}')",
            errors.UniqueViolation,
        ),
        ("UPDATE projects SET cluster = 'demo' WHERE id = {project}", errors.CheckViolation),
        (
            "UPDATE projects SET cluster = 'Demo Cluster', workspace = '~/github', harness_path = 'demo-harness' "
            "WHERE id = {project}",
            errors.CheckViolation,
        ),
        (
            "UPDATE projects SET cluster = 'demo', workspace = '', harness_path = 'demo-harness' WHERE id = {project}",
            errors.CheckViolation,
        ),
        ("INSERT INTO project_repos (project_id, name, path) VALUES ({project}, 'a', E'a\\nb')", errors.CheckViolation),
    ],
)
def test_constraints_refuse_bad_rows(schema, sql, error):
    ids = seed(schema)
    with pytest.raises(error):
        schema.execute(sql.format(**ids))


def test_constraints_accept_good_rows(schema):
    ids = seed(schema)
    fmt = {**ids, "hash": "e" * 64}
    for sql in (
        # a feedback memory and a personal memory may reuse the name of a shared memory
        "INSERT INTO memories (scope, project_id, location, name, type, owner_id, label, body, updated_by) "
        "VALUES ('project', {project}, 'harness', 'note.md', 'feedback', {user}, '{{}}', 'x', {user})",
        "INSERT INTO memories (scope, location, name, type, owner_id, label, body, updated_by) "
        "VALUES ('personal', 'notes', 'note.md', 'user', {user}, '{{}}', 'x', {user})",
        "INSERT INTO tokens (user_id, kind, token_hash, expires_at) "
        "VALUES ({user}, 'web', '{hash}', now() + interval '1 day')",
        "INSERT INTO skill_versions "
        "(skill_id, version, name, sha256, size, r2_key, source_repo, source_commit, published_by) "
        "VALUES ({skill}, 1, 'stop-slop', '{hash}', 10485760, 'blobs/sha256/{hash}', 'skills', 'abc1234', {user})",
        "INSERT INTO kg_ingests (project_id, run_id, source, log_sha256, log_size, pushed_by) "
        "VALUES ({project}, '0190f5e0-0000-7000-8000-000000000001', 'git', '{hash}', 1, {user})",
        "INSERT INTO plans (project_id, plan_id, area, label, body, digest, updated_by) "
        "VALUES ({project}, 'agent-hub', 'active', '{{}}', '{{\"id\": \"agent-hub\"}}', 'sha256:{hash}', {user})",
        "INSERT INTO plan_revisions (project_id, plan_id, revision, area, label, body, digest, actor_id) "
        "VALUES ({project}, 'agent-hub', 1, 'active', '{{}}', '{{}}', 'sha256:{hash}', {user})",
        "INSERT INTO grants (user_id, project_id, role, max_level, granted_by) "
        "VALUES ({user}, {project}, 'reader', 'internal', {user})",
        "UPDATE projects SET cluster = 'demo', workspace = '~/github', harness_path = 'demo-harness' "
        "WHERE id = {project}",
        "INSERT INTO project_repos (project_id, name, origin, default_branch, path) "
        "VALUES ({project}, 'app', 'https://example.org/app.git', 'main', 'app')",
        "INSERT INTO project_sinks (project_id, sink_id, kind, clearance) "
        "VALUES ({project}, 'hub', 'hub', '{{\"level\": \"internal\"}}')",
    ):
        schema.execute(sql.format(**fmt))
    assert one(schema, "SELECT count(*) FROM memories WHERE search @@ to_tsquery('simple', 'body')") == 1
    # a memory's history goes with it
    schema.execute("DELETE FROM memories WHERE id = %s", (ids["memory"],))
    assert one(schema, "SELECT count(*) FROM memory_revisions") == 0

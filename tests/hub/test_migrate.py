"""Migrations: a fresh database gets the forty-six hub tables, a second run changes nothing, processes that start
together apply each revision once, a database at 0001 with rows in it moves to 0002, and a database the code
cannot read is refused. Then the constraints schemas 0001 and 0002 promise. Schemas 0009 and 0010 have their checks in
``tests.hub.test_run_tables``, 0011 in ``tests.hub.test_sealing``, 0012 in ``tests.hub.test_curator``, 0013 in
``tests.hub.test_session_digests``, 0014 in ``tests.hub.test_review_runs``, 0015 in ``tests.hub.test_brief``."""

import re
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from alembic import command
from alembic.script import ScriptDirectory
from psycopg import errors
from sqlalchemy import (
    Boolean,
    Text,
    cast,
    column,
    delete,
    func,
    insert,
    literal,
    not_,
    select,
    table,
    union_all,
    update,
)
from sqlalchemy.dialects.postgresql import REGCLASS

from evo_agents.hub import migrate as hub_migrate
from evo_agents.hub import tables as hub_tables
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
TABLES |= pg.RUN_TABLES  # migration 0009
TABLES |= pg.NOTIFICATION_TABLES  # migration 0010
TABLES |= pg.CREDENTIAL_TABLES  # migration 0011
TABLES |= pg.CURATOR_TABLES  # migration 0012
TABLES |= pg.DIGEST_TABLES  # migration 0013
TABLES |= pg.REVIEW_TABLES  # migration 0014
TABLES |= pg.BRIEF_TABLES  # migration 0015
ALL = revisions()  # every revision the package ships, in order
HEAD = ALL[-1]
# The catalogs the tests read, as much of each as they use.
INFO_TABLES = table("tables", column("table_schema", Text), column("table_name", Text), schema="information_schema")
INFO_COLUMNS = table(
    "columns",
    column("table_schema", Text),
    column("table_name", Text),
    column("column_name", Text),
    column("data_type", Text),
    column("is_nullable", Text),
    column("column_default", Text),
    schema="information_schema",
)
PG_CONSTRAINT = table(
    "pg_constraint", column("oid"), column("conrelid"), column("conname", Text), column("connamespace")
)
PG_INDEXES = table("pg_indexes", column("schemaname", Text), column("indexdef", Text))
PG_TRIGGER = table("pg_trigger", column("tgrelid"), column("tgname", Text), column("tgisinternal", Boolean))
PG_LOCKS = table("pg_locks", column("database"), column("locktype", Text), column("granted", Boolean))
PG_DATABASE = table("pg_database", column("oid"), column("datname", Text))
PG_PROC = table("pg_proc", column("proname", Text))
ALEMBIC_VERSION = table("alembic_version", column("version_num", Text))


def _snapshot():
    """Every column, constraint, index and trigger of the public schema and the revision, one (kind, text) row each,
    sorted: two databases with the same rows have the same schema."""
    columns, constraints, triggers = INFO_COLUMNS.c, PG_CONSTRAINT.c, PG_TRIGGER.c
    column_text = (
        columns.table_name
        + "."
        + columns.column_name
        + " "
        + columns.data_type
        + " "
        + columns.is_nullable
        + " "
        + func.coalesce(columns.column_default, "")
    )
    constraint_text = (
        cast(cast(constraints.conrelid, REGCLASS), Text)
        + " "
        + constraints.conname
        + " "
        + func.pg_get_constraintdef(constraints.oid, type_=Text)
    )
    trigger_text = cast(cast(triggers.tgrelid, REGCLASS), Text) + " " + triggers.tgname
    snapshot = union_all(
        select(literal("column", Text).label("kind"), column_text.label("item")).where(
            columns.table_schema == "public"
        ),
        select(literal("constraint", Text), constraint_text).where(
            constraints.connamespace == func.to_regnamespace("public")
        ),
        select(literal("index", Text), PG_INDEXES.c.indexdef).where(PG_INDEXES.c.schemaname == "public"),
        select(literal("trigger", Text), trigger_text).where(not_(triggers.tgisinternal)),
        select(literal("revision", Text), ALEMBIC_VERSION.c.version_num),
    )
    return snapshot.order_by(snapshot.selected_columns.kind, snapshot.selected_columns.item)


SNAPSHOT = _snapshot()


def query(db, statement) -> list[tuple]:
    """The rows of ``statement``, a Core statement, as tuples, run as the database's owner."""
    return live.sql(db, statement)


def tables(db) -> set[str]:
    return {row[0] for row in query(db, select(INFO_TABLES.c.table_name).where(INFO_TABLES.c.table_schema == "public"))}


def test_a_fresh_database_gets_the_forty_six_tables(hub_db):
    assert len(TABLES) == 46  # the name of this test counts them: a new table renames it
    result = migrate(hub_db.dsn)
    assert result.before == ()
    assert result.applied == ALL and result.after == (head_revision(),) == (HEAD,)
    assert tables(hub_db) == TABLES | {"alembic_version"}
    assert query(hub_db, select(ALEMBIC_VERSION.c.version_num)) == [(HEAD,)]
    # skills and skill_versions keep metadata and the blob key, never the bundle's bytes: the only bytes the hub keeps
    # are the sealed values of credentials (0011) and their nonces
    columns = INFO_COLUMNS.c
    public = select(columns.table_name, columns.column_name).where(columns.table_schema == "public")
    assert sorted(query(hub_db, public.where(columns.data_type == "bytea"))) == [
        ("credential_leases", "nonce"),
        ("credential_leases", "sealed_value"),
        ("secrets", "nonce"),
        ("secrets", "sealed"),
    ]
    assert {row[1] for row in query(hub_db, public.where(columns.table_name == "skill_versions"))} >= {
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
    locks, databases = PG_LOCKS, PG_DATABASE
    waiting = (
        select(func.count())
        .select_from(locks.join(databases, databases.c.oid == locks.c.database))
        .where(locks.c.locktype == "advisory", not_(locks.c.granted), databases.c.datname == db.name)
    )
    return query(db, waiting)[0][0]


def outcome(lines: list[dict]) -> dict:
    """The line where a migrate process says what it did."""
    (line,) = [line for line in lines if line["msg"] in ("migrations applied", "schema already at head")]
    return line


def test_two_processes_migrating_at_once_both_exit_zero_and_apply_once(hub_db):
    env = pg.clean_env(EVO_HUB_DSN=hub_db.dsn)
    # Hold the lock until both processes wait on it, so they really race for it when it is released.
    with live.connect(hub_db) as holder:
        holder.execute(select(func.pg_advisory_lock(LOCK_KEY)))
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
    outputs = [p.communicate(timeout=60) for p in procs]
    assert [p.returncode for p in procs] == [0, 0], [err for _, err in outputs]

    logs = [pg.log_lines(err) for _, err in outputs]
    upgrades = [line for lines in logs for line in lines if line["msg"].startswith("Running upgrade")]
    assert len(upgrades) == len(ALL)  # one line per revision, all from the process that took the lock first
    outcomes = [outcome(lines) for lines in logs]
    assert sorted(line.get("applied", []) for line in outcomes) == [[], list(ALL)]
    assert all(any(line["msg"].startswith("waiting for the migration lock") for line in lines) for lines in logs)
    assert query(hub_db, select(ALEMBIC_VERSION.c.version_num)) == [(HEAD,)]
    assert tables(hub_db) == TABLES | {"alembic_version"}


def test_a_database_at_an_unknown_revision_is_refused(hub_db):
    migrate(hub_db.dsn)
    query(hub_db, update(ALEMBIC_VERSION).values(version_num="9999").returning(ALEMBIC_VERSION.c.version_num))
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
    with live.connect(hub_db) as holder:
        holder.execute(select(func.pg_advisory_lock(LOCK_KEY)))
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


def columns(db, name: str) -> set[str]:
    columns = INFO_COLUMNS.c
    named = select(columns.column_name).where(columns.table_schema == "public", columns.table_name == name)
    return {row[0] for row in query(db, named)}


def test_0002_adds_the_harness_paths_to_a_database_holding_projects(hub_db):
    move_to(hub_db, "0001")
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        conn.execute(insert(hub_tables.project_repos).values(project_id=ids["project"], name="app"))
    move_to(hub_db, "0002")
    assert query(hub_db, select(ALEMBIC_VERSION.c.version_num)) == [("0002",)]
    assert {"cluster", "workspace", "harness_path"} <= columns(hub_db, "projects")
    assert "path" in columns(hub_db, "project_repos")
    # Projects registered before 0002 keep their rows, without paths, which `hub registry pull` skips.
    projects, repos = hub_tables.projects.c, hub_tables.project_repos.c
    paths = select(projects.name, projects.cluster, projects.workspace, projects.harness_path)
    assert query(hub_db, paths) == [("demo", None, None, None)]
    assert query(hub_db, select(repos.name, repos.path)) == [("app", None)]

    move_to(hub_db, "0001", down=True)
    assert not {"cluster", "workspace", "harness_path"} & columns(hub_db, "projects")
    assert query(hub_db, select(projects.name)) == [("demo",)]
    assert query(hub_db, select(repos.name)) == [("app",)]

    # From 0001 with rows in it, migrate runs 0002 and every later revision, and says so.
    result = migrate(hub_db.dsn)
    assert (result.before, result.applied, result.after) == (("0001",), ALL[ALL.index("0002") :], (HEAD,))


def build(project_id: int, artifact_sha256: str, content_hash: str):
    """A succeeded build of ``project_id`` as revision 0007 knows it, returning its id."""
    builds = hub_tables.kg_builds
    return (
        insert(builds)
        .values(
            project_id=project_id,
            status="succeeded",
            artifact_sha256=artifact_sha256,
            artifact_size=10,
            content_hash=content_hash,
            nodes=1,
            edges=0,
            started_at=func.now(),
            finished_at=func.now(),
        )
        .returning(builds.c.id)
    )


def test_0008_keeps_the_builds_and_going_back_fails_the_pruned_ones(hub_db):
    move_to(hub_db, "0007")
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        first = one(conn, build(ids["project"], "a" * 64, "sha256:" + "c" * 64))
        second = one(conn, build(ids["project"], "b" * 64, "sha256:" + "c" * 64))
    move_to(hub_db, "0008")
    assert {"artifact_reused_from", "artifact_pruned_at"} <= columns(hub_db, "kg_builds")
    builds, deletions = hub_tables.kg_builds, hub_tables.blob_deletions
    with live.connect(hub_db) as conn:
        conn.execute(update(builds).values(artifact_reused_from=first).where(builds.c.id == second))
        conn.execute(
            update(builds).values(artifact_sha256=None, artifact_pruned_at=func.now()).where(builds.c.id == first)
        )
        for bad in (
            update(builds)
            .values(artifact_pruned_at=None)
            .where(builds.c.id == first),  # succeeded without any artifact
            update(builds).values(artifact_pruned_at=func.now()).where(builds.c.id == second),  # pruned yet holding one
            update(builds).values(artifact_reused_from=builds.c.id).where(builds.c.id == second),
            insert(deletions).values(sha256="not-a-hash", size=1, kind="kg-graph"),
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(bad)
        conn.execute(insert(deletions).values(sha256="a" * 64, size=10, kind="kg-graph"))

    move_to(hub_db, "0007", down=True)
    assert "blob_deletions" not in tables(hub_db)
    kept = select(builds.c.id, builds.c.status, builds.c.artifact_sha256, builds.c.error.is_not(None))
    assert query(hub_db, kept.order_by(builds.c.id)) == [
        (first, "failed", None, True),
        (second, "succeeded", "b" * 64, False),
    ]
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0008") :]


def test_downgrade_to_base_removes_everything_and_upgrade_restores_it(hub_db):
    migrate(hub_db.dsn)
    move_to(hub_db, "base", down=True)
    assert tables(hub_db) == {"alembic_version"}
    functions = select(func.count()).select_from(PG_PROC).where(PG_PROC.c.proname == "hub_reject_update")
    assert query(hub_db, functions) == [(0,)]
    assert migrate(hub_db.dsn).applied == ALL
    assert tables(hub_db) == TABLES | {"alembic_version"}


@pytest.fixture
def schema(hub_db):
    """A migrated database and a connection to it as its owner, in autocommit."""
    migrate(hub_db.dsn)
    with live.connect(hub_db) as conn:
        yield conn


def one(conn, statement):
    """The first column of the first row ``statement`` returns on ``conn``."""
    return conn.execute(statement).first()[0]


def seed(conn) -> dict:
    """A user, a machine token, the project demo with a memory and its first revision, the skill stop-slop and a line
    of audit, written on ``conn``; their ids."""
    t = hub_tables
    label = {"level": "internal"}
    ids = {"user": one(conn, insert(t.users).values(login="Octo", github_id=1).returning(t.users.c.id))}
    token = insert(t.tokens).values(
        user_id=ids["user"], kind="machine", token_hash="a" * 64, host="mac", expires_at=func.now() + timedelta(days=90)
    )
    ids["token"] = one(conn, token.returning(t.tokens.c.id))
    project = insert(t.projects).values(
        name="demo", levels=["public", "internal"], locations=["any"], default_label=label, created_by=ids["user"]
    )
    ids["project"] = one(conn, project.returning(t.projects.c.id))
    memory = insert(t.memories).values(
        scope="project",
        project_id=ids["project"],
        location="harness",
        name="note.md",
        type="project",
        owner_id=ids["user"],
        label=label,
        body="body text",
        updated_by=ids["user"],
    )
    ids["memory"] = one(conn, memory.returning(t.memories.c.id))
    conn.execute(
        insert(t.memory_revisions).values(
            memory_id=ids["memory"],
            revision=1,
            type="project",
            label=label,
            body="body text",
            deleted=False,
            actor_id=ids["user"],
        )
    )
    skill = insert(t.skills).values(scope="global", name="stop-slop", created_by=ids["user"])
    ids["skill"] = one(conn, skill.returning(t.skills.c.id))
    conn.execute(
        insert(t.audit).values(actor_id=ids["user"], token_id=ids["token"], action="memory.put", target="demo/note.md")
    )
    return ids


DAY = timedelta(days=1)


def _token(ids, **values):
    """A token of the seeded user, valid for a day, with ``values``."""
    return insert(hub_tables.tokens).values({"user_id": ids["user"], "expires_at": func.now() + DAY, **values})


def _memory(ids, **values):
    """A memory of the seeded user in the location harness with an empty label, unless ``values`` say otherwise."""
    user = ids["user"]
    memory = {"location": "harness", "owner_id": user, "label": {}, "body": "x", "updated_by": user}
    return insert(hub_tables.memories).values({**memory, **values})


def _skill_version(ids, **values):
    """Version 1 of the seeded skill stop-slop, with ``values``."""
    version = {"skill_id": ids["skill"], "version": 1, "name": "stop-slop", "published_by": ids["user"]}
    return insert(hub_tables.skill_versions).values({**version, **values})


def _paths(ids, **values):
    """The seeded project's harness paths set to ``values``."""
    return update(hub_tables.projects).values(**values).where(hub_tables.projects.c.id == ids["project"])


@pytest.mark.parametrize(
    "statement, error",
    [
        # identity
        (lambda ids: insert(hub_tables.users).values(login="octo"), errors.UniqueViolation),
        (lambda ids: _token(ids, kind="machine", token_hash="a" * 64, host="pc"), errors.UniqueViolation),
        (
            lambda ids: _memory(ids, scope="project", project_id=ids["project"], name="note.md", type="reference"),
            errors.UniqueViolation,
        ),
        (
            lambda ids: insert(hub_tables.skills).values(scope="global", name="stop-slop", created_by=ids["user"]),
            errors.UniqueViolation,
        ),
        # enum-like columns and formats
        (lambda ids: _token(ids, kind="cli", token_hash="b" * 64, host="pc"), errors.CheckViolation),
        (lambda ids: _token(ids, kind="machine", token_hash="evh_plaintext", host="pc"), errors.CheckViolation),
        (lambda ids: _token(ids, kind="machine", token_hash="c" * 64), errors.CheckViolation),
        (
            lambda ids: insert(hub_tables.grants).values(
                user_id=ids["user"],
                project_id=ids["project"],
                role="owner",
                max_level="internal",
                granted_by=ids["user"],
            ),
            errors.CheckViolation,
        ),
        (lambda ids: _memory(ids, scope="project", name="x.md", type="project"), errors.CheckViolation),
        (
            lambda ids: insert(hub_tables.project_sinks).values(
                project_id=ids["project"], sink_id="s", kind="printer", clearance={"level": "internal"}
            ),
            errors.CheckViolation,
        ),
        (
            lambda ids: _skill_version(ids, sha256="d" * 64, size=10, r2_key="bundles/other"),
            errors.CheckViolation,
        ),
        (
            lambda ids: _skill_version(ids, sha256="d" * 64, size=10485761, r2_key="blobs/sha256/" + "d" * 64),
            errors.CheckViolation,
        ),
        # history is never edited, and keeps its authors
        (lambda ids: update(hub_tables.memory_revisions).values(body="edited"), errors.RestrictViolation),
        (lambda ids: update(hub_tables.audit).values(target="edited"), errors.RestrictViolation),
        (
            lambda ids: delete(hub_tables.users).where(hub_tables.users.c.id == ids["user"]),
            errors.ForeignKeyViolation,
        ),
        (
            lambda ids: delete(hub_tables.projects).where(hub_tables.projects.c.id == ids["project"]),
            errors.ForeignKeyViolation,
        ),
        # a project has one hub sink, and its harness paths all or none (0002)
        (
            lambda ids: insert(hub_tables.project_sinks).values(
                [
                    {"project_id": ids["project"], "sink_id": "hub", "kind": "hub", "clearance": {"level": "internal"}},
                    {"project_id": ids["project"], "sink_id": "hub-2", "kind": "hub", "clearance": {"level": "public"}},
                ]
            ),
            errors.UniqueViolation,
        ),
        (lambda ids: _paths(ids, cluster="demo"), errors.CheckViolation),
        (
            lambda ids: _paths(ids, cluster="Demo Cluster", workspace="~/github", harness_path="demo-harness"),
            errors.CheckViolation,
        ),
        (lambda ids: _paths(ids, cluster="demo", workspace="", harness_path="demo-harness"), errors.CheckViolation),
        (
            lambda ids: insert(hub_tables.project_repos).values(project_id=ids["project"], name="a", path="a\nb"),
            errors.CheckViolation,
        ),
    ],
)
def test_constraints_refuse_bad_rows(schema, statement, error):
    ids = seed(schema)
    with pytest.raises(error):
        schema.execute(statement(ids))


def test_constraints_accept_good_rows(schema):
    ids = seed(schema)
    t, digest, user, project = hub_tables, "e" * 64, ids["user"], ids["project"]
    for statement in (
        # a feedback memory and a personal memory may reuse the name of a shared memory
        _memory(ids, scope="project", project_id=project, name="note.md", type="feedback"),
        _memory(ids, scope="personal", location="notes", name="note.md", type="user"),
        insert(t.tokens).values(user_id=user, kind="web", token_hash=digest, expires_at=func.now() + DAY),
        _skill_version(
            ids,
            sha256=digest,
            size=10485760,
            r2_key=f"blobs/sha256/{digest}",
            source_repo="skills",
            source_commit="abc1234",
        ),
        insert(t.kg_ingests).values(
            project_id=project,
            run_id=UUID("0190f5e0-0000-7000-8000-000000000001"),
            source="git",
            log_sha256=digest,
            log_size=1,
            pushed_by=user,
        ),
        insert(t.plans).values(
            project_id=project,
            plan_id="agent-hub",
            area="active",
            label={},
            body={"id": "agent-hub"},
            digest=f"sha256:{digest}",
            updated_by=user,
        ),
        insert(t.plan_revisions).values(
            project_id=project,
            plan_id="agent-hub",
            revision=1,
            area="active",
            label={},
            body={},
            digest=f"sha256:{digest}",
            actor_id=user,
        ),
        insert(t.grants).values(user_id=user, project_id=project, role="reader", max_level="internal", granted_by=user),
        _paths(ids, cluster="demo", workspace="~/github", harness_path="demo-harness"),
        insert(t.project_repos).values(
            project_id=project, name="app", origin="https://example.org/app.git", default_branch="main", path="app"
        ),
        insert(t.project_sinks).values(project_id=project, sink_id="hub", kind="hub", clearance={"level": "internal"}),
    ):
        schema.execute(statement)
    memories = t.memories
    matching = memories.c.search.bool_op("@@")(func.to_tsquery("simple", "body"))
    assert one(schema, select(func.count()).select_from(memories).where(matching)) == 1
    # a memory's history goes with it
    schema.execute(delete(memories).where(memories.c.id == ids["memory"]))
    assert one(schema, select(func.count()).select_from(t.memory_revisions)) == 0

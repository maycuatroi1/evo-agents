"""Migrations: a fresh database gets the sixty-five hub tables, a second run changes nothing, processes that start
together apply each revision once, a database at 0001 with rows in it moves to 0002, and a database the code
cannot read is refused. Then the constraints schemas 0001 and 0002 promise. Schemas 0009 and 0010 have their checks in
``tests.hub.test_run_tables``, 0011 in ``tests.hub.test_sealing``, 0012 in ``tests.hub.test_curator``, 0013 in
``tests.hub.test_session_digests``, 0014 in ``tests.hub.test_review_runs``, 0015 in ``tests.hub.test_brief``, 0017 in
``tests.hub.test_curator_changes``, 0018 in ``tests.hub.test_curator_ledger``.

A test of one revision starts from a copy of a template near the revision before it, which the pytest process migrated
once (``move_to``), and goes on to head one revision a transaction (``pg.migrate_in_steps``); the few tests that
migrate the whole chain in one transaction take turns on the server (``migrate`` here, ``whole_chain``). That keeps
the module fast and lets it run under ``pytest -n auto``."""

import re
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from alembic.script import ScriptDirectory
from psycopg import errors
from sqlalchemy import (
    Boolean,
    MetaData,
    Text,
    cast,
    column,
    delete,
    func,
    insert,
    literal,
    not_,
    null,
    select,
    table,
    union_all,
    update,
)
from sqlalchemy.dialects.postgresql import REGCLASS

from evo_agents.hub import migrate as hub_migrate
from evo_agents.hub import tables as hub_tables
from evo_agents.hub.migrate import LOCK_KEY, MigrationError, alembic_config, head_revision, revisions

# Every test here gets an empty database of its own, not the copy of the migrated template: it migrates it, or move_to
# makes it a copy of a template below the revision the test starts from.
pytestmark = pytest.mark.empty_db

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
TABLES |= pg.CHANGE_TABLES  # migration 0017
TABLES |= pg.LEDGER_TABLES  # migration 0018
TABLES |= pg.ACCOUNT_TABLES  # migration 0027
TABLES |= pg.PROFILE_TABLES  # migration 0030
TABLES |= pg.CHAT_TABLES  # migration 0031
TABLES |= pg.PLUGIN_TABLES  # migration 0034
TABLES |= pg.PLUGIN_PROJECT_TABLES  # migration 0035
TABLES |= pg.PLUGIN_BACKEND_TABLES  # migration 0036
TABLES |= pg.SETTINGS_TABLES  # migration 0037
TABLES |= pg.PLUGIN_POINT_TABLES  # migration 0038
TABLES |= pg.AGENT_TYPE_TABLES  # migration 0042
TABLES |= pg.BUILDER_TABLES  # migration 0043
TABLES |= pg.TRIAGE_TABLES  # migration 0044
TABLES |= pg.SRS_TABLES  # migration 0046
TABLES |= pg.TASKS_TABLES  # migration 0047
TABLES |= pg.TRANSCRIPT_TABLES  # migration 0052
TABLES |= pg.FACET_TABLES  # migration 0054
TABLES |= pg.KG_HEALTH_TABLES  # migration 0055
TABLES |= pg.REPLAY_TABLES  # migration 0056
TABLES |= pg.HISTORY_TABLES  # migration 0059
TABLES |= pg.SEARCH_TABLES  # migration 0060
TABLES |= pg.NIGHTLY_TABLES  # migration 0065
TABLES |= pg.RELEASE_TABLES  # migration 0067
TABLES |= pg.USER_EMAIL_TABLES  # migration 0068
TABLES |= pg.SRS_FINDING_TABLES  # migration 0069
TABLES |= pg.SRS_SETTLE_TABLES  # migration 0070
TABLES |= pg.SRS_APPROVAL_TASK_TABLES  # migration 0071
TABLES |= pg.CANVAS_TABLES  # migration 0073
TABLES |= pg.SIGNAL_TABLES  # migration 0074
TABLES |= pg.CANVAS_PROPOSAL_TABLES  # migration 0075
TABLES |= pg.CANVAS_JUDGING_TABLES  # migration 0076
TABLES |= pg.CANVAS_BASELINE_TABLES  # migration 0077
TABLES |= pg.POLICY_TABLES  # migration 0079
TABLES |= pg.MERGE_TABLES  # migration 0081
TABLES |= pg.SAMPLE_TABLES  # migration 0083
TABLES |= pg.BREAKER_TABLES  # migration 0084
TABLES |= pg.TASKS_BLOCKER_TABLES  # migration 0085
TABLES |= pg.PM_TABLES  # migration 0086
TABLES |= pg.OUTPUT_TABLES  # migration 0089
TABLES |= pg.MUTE_TABLES  # migration 0090
TABLES |= pg.GOAL_TABLES  # migration 0091
TABLES |= pg.LOOP_TABLES  # migration 0093
TABLES |= pg.PLUGIN_NOTICE_TABLES  # migration 0097
TABLES |= pg.SRS_USE_CASE_REVIEW_TABLES  # migration 0098
TABLES |= pg.ORG_TABLES  # migration 0099
TABLES |= pg.AGENT_QUESTION_TABLES  # migration 0100
TABLES |= pg.SHARE_TABLES  # migration 0105
TABLES |= pg.WORKER_SHARE_TABLES  # migration 0107
TABLES |= pg.ATTACHMENT_TABLES  # migration 0109
TABLES |= pg.INVITE_TABLES  # migration 0110
TABLES |= pg.AUTONOMY_LEVEL_TABLES  # migration 0112
TABLES |= pg.AUTONOMY_STOP_TABLES  # migration 0116
TABLES |= pg.BROWSER_TABLES  # migration 0117
TABLES |= pg.CRM_TABLES  # migration 0124
TABLES |= pg.MARKETING_TABLES  # migration 0125
TABLES |= pg.GROWTH_TABLES  # migration 0127
TABLES |= pg.PROBE_TABLES  # migration 0130
TABLES |= pg.PROBE_RUN_TABLES  # migration 0131
TABLES |= pg.PROBLEM_TABLES  # migration 0134
TABLES |= pg.PROBLEM_EVIDENCE_TABLES  # migration 0137
TABLES |= pg.INTAKE_TABLES  # migration 0138
TABLES |= pg.KG_REVIEW_TABLES  # migration 0141
TABLES |= pg.KG_DECISION_TABLES  # migration 0142
TABLES |= pg.OPPORTUNITY_TABLES  # migration 0143
TABLES |= pg.RETIRED_PLUGIN_TABLES  # migration 0147
TABLES |= pg.POOL_TABLES  # migration 0148
TABLES |= pg.PROVIDER_ACCOUNT_TABLES  # migration 0151
TABLES |= pg.MARKET_TABLES  # migration 0154
TABLES |= pg.PUSH_TABLES  # migration 0156
TABLES |= pg.TODAY_TABLES  # migration 0158
TABLES |= pg.SELLER_TABLES  # migration 0159
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


def migrate(dsn: str, **kwargs) -> hub_migrate.MigrationResult:
    """``evo_agents.hub.migrate.migrate`` in its turn on the server (``pg.one_whole_chain``): from an empty database,
    or from 0001, it migrates the whole chain in one transaction."""
    with pg.one_whole_chain():
        return hub_migrate.migrate(dsn, **kwargs)


def tables(db) -> set[str]:
    return {row[0] for row in query(db, select(INFO_TABLES.c.table_name).where(INFO_TABLES.c.table_schema == "public"))}


def plugin_tables(db, *names: str) -> SimpleNamespace:
    """The tables ``names`` as the database holds them at the revision the test moved it to: tables the hub's chain
    made and a core plugin now keeps (``evo_agents.hub.migrations.owned``), which the hub's metadata no longer holds."""
    metadata = MetaData()
    with live.engine(db).connect() as conn:
        metadata.reflect(conn, only=names)
    return SimpleNamespace(**metadata.tables)


def test_a_fresh_database_gets_the_one_hundred_and_ninety_six_tables(hub_db):
    assert len(TABLES) == 196  # the name of this test counts them: a new table renames it
    result = migrate(hub_db.dsn)
    assert result.before == ()
    assert result.applied == ALL and result.after == (head_revision(),) == (HEAD,)
    assert tables(hub_db) == TABLES | {"alembic_version"} | pg.plugin_version_tables()
    assert query(hub_db, select(ALEMBIC_VERSION.c.version_num)) == [(HEAD,)]
    # skills and skill_versions keep metadata and the blob key, never the bundle's bytes: the only bytes the hub keeps
    # are the sealed values of credentials (0011) and their nonces, the sealed tokens of OAuth accounts (0066), the
    # sealed data keys of browser profiles (0117), whose profiles are in the bucket, sealed, and the screenshot (at
    # most 2 MiB) a browser script took of a marketing post (0126)
    columns = INFO_COLUMNS.c
    public = select(columns.table_name, columns.column_name).where(columns.table_schema == "public")
    assert sorted(query(hub_db, public.where(columns.data_type == "bytea"))) == [
        ("accounts", "oauth_nonce"),
        ("accounts", "oauth_sealed"),
        ("browser_identities", "key_nonce"),
        ("browser_identities", "previous_key_nonce"),
        ("browser_identities", "previous_sealed_key"),
        ("browser_identities", "sealed_key"),
        ("credential_leases", "nonce"),
        ("credential_leases", "sealed_value"),
        ("credential_leases", "token_hash"),
        ("marketing_posts", "screenshot"),
        ("secrets", "nonce"),
        ("secrets", "sealed"),
    ]
    assert {row[1] for row in query(hub_db, public.where(columns.table_name == "skill_versions"))} >= {
        "sha256",
        "size",
        "r2_key",
    }


def test_a_second_migrate_changes_nothing(hub_db):
    move_to(hub_db, "head")
    migrate(hub_db.dsn)  # the hub's chain is at head: this runs the installed core plugins' chains
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


def test_two_processes_migrating_at_once_both_exit_zero_and_apply_once(hub_db, whole_chain):
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
    # one line per revision of the hub's chain and of the installed core plugins', all from the process that took the
    # lock first
    assert len(upgrades) == len(ALL) + pg.plugin_revision_count()
    outcomes = [outcome(lines) for lines in logs]
    assert sorted(line.get("applied", []) for line in outcomes) == [[], list(ALL)]
    assert all(any(line["msg"].startswith("waiting for the migration lock") for line in lines) for lines in logs)
    assert query(hub_db, select(ALEMBIC_VERSION.c.version_num)) == [(HEAD,)]
    assert tables(hub_db) == TABLES | {"alembic_version"} | pg.plugin_version_tables()


def test_a_database_at_an_unknown_revision_is_refused(hub_db):
    move_to(hub_db, "head")
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
    """Upgrade or downgrade the database to ``revision`` with Alembic, every revision in one transaction, as
    ``migrate`` drives it. An empty database first becomes a copy of the nearest template at or below ``revision``
    (``pg.template_below``), which the pytest process migrated once: from nothing, every test would migrate again the
    chain below the revision it tests."""
    if not down and query(db, select(func.to_regclass("public.alembic_version")))[0][0] is None:
        if below := pg.template_below(revision):
            pg.copy_into(db, below[1])
    pg.move(db, revision, down=down)


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
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0008") :]


def _worker_row(ids, token_id: int, name: str, slots: int, **values):
    """The INSERT of a worker of the seeded user with ``slots``, returning its id."""
    host = {"hostname": "mac", "os": "darwin", "arch": "arm64", "agent_version": "0.13.0"}
    workers = hub_tables.workers
    row = {"owner_id": ids["user"], "token_id": token_id, "name": name, "slots": slots, **host, **values}
    return insert(workers).values(row).returning(workers.c.id)


def _pairing_row(ids, selector: str, slots: int, made_ago: timedelta):
    """The INSERT of an unused pairing of the seeded user for demo with ``slots``, made ``made_ago`` and lasting ten
    minutes, returning its id."""
    pairings = hub_tables.worker_pairings
    row = {"code_selector": selector, "code_hash": "2" * 64, "owner_id": ids["user"], "name": "paired"}
    made = func.now() - made_ago
    row |= {
        "projects": [ids["project"]],
        "slots": slots,
        "created_at": made,
        "expires_at": made + timedelta(minutes=10),
    }
    return insert(pairings).values(row).returning(pairings.c.id)


def test_0026_takes_up_to_sixteen_slots_and_going_back_refuses_a_live_worker_over_eight(hub_db):
    move_to(hub_db, "0025")
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        token_id = hub_tables.tokens.c.id
        tokens = [
            one(conn, _token(ids, kind="worker", token_hash=f"{n}" * 64, host="mac").returning(token_id))
            for n in range(3)
        ]
        with pytest.raises(errors.CheckViolation):  # eight at most before 0026
            conn.execute(_worker_row(ids, tokens[0], "big", 9))
    move_to(hub_db, "0026")
    workers, pairings = hub_tables.workers, hub_tables.worker_pairings
    with live.connect(hub_db) as conn:
        live_id = one(conn, _worker_row(ids, tokens[0], "big", 16, free_slots=16))
        gone_id = one(conn, _worker_row(ids, tokens[1], "gone", 12, free_slots=12, revoked_at=func.now()))
        small_id = one(conn, _worker_row(ids, tokens[2], "small", 2, free_slots=2))
        waiting_id = one(conn, _pairing_row(ids, "WXYZ", 16, timedelta(minutes=1)))
        expired_id = one(conn, _pairing_row(ids, "ABCD", 9, timedelta(minutes=20)))
        for bad in (
            update(workers).values(slots=17).where(workers.c.id == small_id),
            update(workers).values(slots=0).where(workers.c.id == small_id),
            update(workers).values(free_slots=17).where(workers.c.id == small_id),
            update(pairings).values(slots=17).where(pairings.c.id == waiting_id),
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(bad)

    # Going back refuses, naming them, while a live worker or a live pairing holds more than eight; nothing changed.
    with pytest.raises(RuntimeError, match=rf"worker {live_id}, pairing {waiting_id} have more"):
        move_to(hub_db, "0025", down=True)
    assert query(hub_db, select(ALEMBIC_VERSION.c.version_num)) == [("0026",)]
    live.sql(hub_db, update(workers).values(slots=8).where(workers.c.id == live_id))
    with pytest.raises(RuntimeError, match=rf"pairing {waiting_id} has more"):
        move_to(hub_db, "0025", down=True)
    live.sql(hub_db, update(pairings).values(expires_at=func.now()).where(pairings.c.id == waiting_id))

    # Then it goes back: the revoked worker and the used or expired pairings keep eight, free_slots at most eight.
    move_to(hub_db, "0025", down=True)
    kept = select(workers.c.id, workers.c.slots, workers.c.free_slots).order_by(workers.c.id)
    assert query(hub_db, kept) == [(live_id, 8, 8), (gone_id, 8, 8), (small_id, 2, 2)]
    assert query(hub_db, select(pairings.c.id, pairings.c.slots).order_by(pairings.c.id)) == [
        (waiting_id, 8),
        (expired_id, 8),
    ]
    with live.connect(hub_db) as conn, pytest.raises(errors.CheckViolation):
        conn.execute(update(workers).values(slots=9).where(workers.c.id == small_id))
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0026") :]


def _author_run(ids, **extra):
    """The INSERT of a queued author run of the seeded user in demo, on no plan, with ``extra``, returning its id."""
    runs = hub_tables.runs
    row = {"project_id": ids["project"], "dispatched_by": ids["user"], "kind": "author", "request": "A wait helper."}
    row |= {"requested_runtime": "claude-code", "runtime": "claude-code", "mode": "headless", "approval": "auto"}
    row |= {"timeout_s": 3600, "state": "queued", "repos": [{"repo": "harness", "branch": "main"}]} | extra
    return insert(runs).values(row).returning(runs.c.id)


def _decision_row(ids, run_id: int, plan_id: str | None):
    """The INSERT of an open decision of ``run_id`` on ``plan_id``, returning its id."""
    decisions = hub_tables.decisions
    options = [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}]
    row = {"run_id": run_id, "project_id": ids["project"], "plan_id": plan_id, "category": "scope"}
    row |= {"question": "Which one?", "options": options}
    return insert(decisions).values(row).returning(decisions.c.id)


def test_0029_lets_an_author_decision_have_no_plan_and_going_back_deletes_those_with_their_inbox_rows(hub_db):
    move_to(hub_db, "0028")
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        run_id = one(conn, _author_run(ids))
        with pytest.raises(errors.NotNullViolation):  # every decision had a plan before 0029
            conn.execute(_decision_row(ids, run_id, None))
    move_to(hub_db, "0029")
    t = hub_tables
    with live.connect(hub_db) as conn:
        planless = one(conn, _decision_row(ids, run_id, None))
        planned = one(conn, _decision_row(ids, run_id, "wait-helper"))
        for decision_id in (planless, planned):
            conn.execute(
                insert(t.run_inbox).values(run_id=run_id, sent_by=ids["user"], body="A.", decision_id=decision_id)
            )
            notice = {"user_id": ids["user"], "kind": "decision", "project_id": ids["project"], "title": "Asks"}
            conn.execute(insert(t.notifications).values(**notice, run_id=run_id, decision_id=decision_id))

    # Going back deletes the decision without a plan, the answer in the inbox and the notification naming it.
    move_to(hub_db, "0028", down=True)
    assert query(hub_db, select(t.decisions.c.id, t.decisions.c.plan_id)) == [(planned, "wait-helper")]
    assert query(hub_db, select(t.run_inbox.c.decision_id)) == [(planned,)]
    assert query(hub_db, select(t.notifications.c.decision_id)) == [(planned,)]
    with live.connect(hub_db) as conn, pytest.raises(errors.NotNullViolation):
        conn.execute(_decision_row(ids, run_id, None))
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0029") :]


def _chat_run(ids, chat_id: int, message_id: int | None, **extra):
    """The INSERT of a queued chat run of the seeded user in demo, started by ``message_id`` of ``chat_id``."""
    runs = hub_tables.runs
    row = {"project_id": ids["project"], "dispatched_by": ids["user"], "kind": "chat", "branch": "chat/1"}
    row |= {"requested_runtime": "claude-code", "runtime": "claude-code", "mode": "headless", "approval": "auto"}
    row |= {"timeout_s": 3600, "state": "queued", "chat_id": chat_id, "chat_message_id": message_id, "hop": 1}
    return insert(runs).values(row | extra).returning(runs.c.id)


CHATS_0032 = table("chats", column("id"), column("budget_usd"))  # the chat's budget, from 0032 until 0072


def test_0032_takes_chat_runs_of_a_chat_message_and_going_back_deletes_them(hub_db):
    move_to(hub_db, "0031")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        chat_id = one(
            conn, insert(t.chats).values(title="Release talk", created_by=ids["user"]).returning(t.chats.c.id)
        )
        message = insert(t.chat_messages).values(chat_id=chat_id, seq=1, body="@reviewer look", label=[])
        message_id = one(conn, message.returning(t.chat_messages.c.id))
        runs = t.runs
        no_chat_kind = {"project_id": ids["project"], "dispatched_by": ids["user"], "kind": "chat", "timeout_s": 3600}
        no_chat_kind |= {"requested_runtime": "any", "runtime": "any", "mode": "headless", "approval": "auto"}
        with pytest.raises(errors.CheckViolation):  # no chat run before 0032
            conn.execute(insert(runs).values(no_chat_kind))
    move_to(hub_db, "0032")
    with live.connect(hub_db) as conn:
        first = one(conn, _chat_run(ids, chat_id, message_id))
        resumed = one(conn, _chat_run(ids, chat_id, message_id, resume_of_run_id=first, hop=2))
        one(conn, _chat_run(ids, chat_id, message_id, attempt=2, parent_run_id=resumed, repos=null()))
        author = one(conn, _author_run(ids))
        for bad in (
            _chat_run(ids, chat_id, None),
            _chat_run(ids, chat_id, message_id, hop=4),
            _chat_run(ids, chat_id, message_id, hop=None),
            insert(runs).values(
                project_id=ids["project"],
                dispatched_by=ids["user"],
                kind="chat",
                timeout_s=3600,
                requested_runtime="any",
                runtime="any",
                mode="headless",
                approval="auto",
            ),
            update(runs).values(chat_id=chat_id, chat_message_id=message_id, hop=1).where(runs.c.id == author),
            update(CHATS_0032).values(budget_usd=0).where(CHATS_0032.c.id == chat_id),
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(bad)
    assert query(hub_db, select(CHATS_0032.c.budget_usd)) == [(5.0,)]

    # Going back deletes the chat runs, the one going on in another's session and the retry included; the rest stays.
    move_to(hub_db, "0031", down=True)
    assert query(hub_db, select(t.runs.c.id, t.runs.c.kind).order_by(t.runs.c.id)) == [(author, "author")]
    assert query(hub_db, select(t.chat_messages.c.id)) == [(message_id,)]
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0032") :]


def _local_run(ids, session_id: str, **extra):
    """The INSERT of a running local run of the seeded user in demo, returning its id."""
    runs = hub_tables.runs
    row = {"project_id": ids["project"], "dispatched_by": ids["user"], "kind": "local", "session_id": session_id}
    row |= {"requested_runtime": "claude-code", "runtime": "claude-code", "mode": "interactive", "approval": "auto"}
    row |= {"timeout_s": 86400, "state": "running", "machine": "mac", "cwd": "/work", "claude_email": "me@example.com"}
    row |= {"last_activity_at": func.now()} | extra
    return insert(runs).values(row).returning(runs.c.id)


def test_0033_takes_local_runs_once_per_session_and_going_back_deletes_them(hub_db):
    move_to(hub_db, "0032")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        author = one(conn, _author_run(ids))
        with pytest.raises(errors.CheckViolation):  # no local run before 0033
            old = {"project_id": ids["project"], "dispatched_by": ids["user"], "kind": "local", "session_id": "s0"}
            old |= {"requested_runtime": "claude-code", "runtime": "claude-code", "mode": "interactive"}
            old |= {"approval": "auto", "timeout_s": 86400, "state": "running"}
            conn.execute(insert(table("runs", *(column(name) for name in old))).values(old))
    move_to(hub_db, "0033")
    with live.connect(hub_db) as conn:
        one(conn, _local_run(ids, "s1"))
        for state in ("waiting", "idle", "ended"):
            extra = {"state": state}
            if state == "waiting":
                extra["waiting_since"] = func.now()
            if state == "ended":
                extra["finished_at"] = func.now()
            one(conn, _local_run(ids, f"s-{state}", **extra))
        for bad in (
            _local_run(ids, "s1"),  # one run per session
            _local_run(ids, "b1", machine=None),
            _local_run(ids, "b2", cwd=""),
            _local_run(ids, "b3", claude_email="a\nb"),
            _local_run(ids, "b4", state="queued"),
            _local_run(ids, "b5", last_activity_at=None),
            update(t.runs).values(state="idle").where(t.runs.c.id == author),
            update(t.runs).values(machine="mac").where(t.runs.c.id == author),
        ):
            with pytest.raises(
                errors.CheckViolation if bad.is_update else (errors.CheckViolation, errors.UniqueViolation)
            ):
                conn.execute(bad)
    move_to(hub_db, "0032", down=True)
    assert query(hub_db, select(t.runs.c.id, t.runs.c.kind).order_by(t.runs.c.id)) == [(author, "author")]
    assert "machine" not in columns(hub_db, "runs")
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0033") :]


def _plugin_version(plugin_id: int, user_id: int, **extra):
    """The INSERT of version 1.0.0 of plugin ``plugin_id``, signed with an Ed25519 key."""
    row = {"plugin_id": plugin_id, "version": "1.0.0", "sha256": "b" * 64, "size": 10, "r2_key": "blobs/sha256/b"}
    row |= {"publisher": "acme", "tier": "isolated", "manifest": {"id": "sandbox"}, "signature": {"kind": "ed25519"}}
    row |= {"signature_kind": "ed25519", "signed_by": "ed25519:0123", "published_by": user_id}
    return insert(hub_tables.plugin_versions).values(row | extra)


def test_0034_keeps_the_plugin_store_and_going_back_drops_it_with_the_hubs_plugin_bundles(hub_db):
    move_to(hub_db, "0033")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        bundle = {"project_id": None, "sha256": "b" * 64, "size": 10, "kind": "plugin-bundle"}
        with pytest.raises(errors.CheckViolation):  # the hub held only skill bundles itself before 0034
            conn.execute(insert(t.blobs).values(bundle))
    move_to(hub_db, "0034")
    plugins = t.plugins
    with live.connect(hub_db) as conn:
        conn.execute(insert(t.blobs).values(bundle))
        plugin_id = one(conn, insert(plugins).values(name="sandbox", publisher="acme").returning(plugins.c.id))
        conn.execute(_plugin_version(plugin_id, ids["user"]))
        installed = {"installed_version": "1.0.0", "installed_by": ids["user"], "installed_at": func.now()}
        conn.execute(update(plugins).values(installed).where(plugins.c.id == plugin_id))
        notice = {"user_id": ids["user"], "kind": "notice", "notice_kind": "plugin_disabled", "title": "Off"}
        conn.execute(insert(t.notifications).values(notice))
        for bad in (
            insert(plugins).values(name="Bad_Name", publisher="acme"),
            update(plugins).values(installed_version="9.9.9").where(plugins.c.id == plugin_id),
            update(plugins).values(installed_by=None).where(plugins.c.id == plugin_id),
            update(plugins).values(disabled_reason="old").where(plugins.c.id == plugin_id),
            _plugin_version(plugin_id, ids["user"], version="2.0.0", tier="trusted"),
            _plugin_version(plugin_id, ids["user"], version="2.0.0", signature_kind="gpg"),
            _plugin_version(plugin_id, ids["user"], version="2.0.0", sha256="B" * 64),
        ):
            with pytest.raises((errors.CheckViolation, errors.ForeignKeyViolation)):
                conn.execute(bad)

    # Going back drops the store, the hub's plugin bundles and the notices that a plugin was turned off.
    move_to(hub_db, "0033", down=True)
    assert "plugins" not in tables(hub_db) and "plugin_versions" not in tables(hub_db)
    assert query(hub_db, select(t.blobs.c.sha256).where(t.blobs.c.project_id.is_(None))) == []
    assert query(hub_db, select(t.notifications.c.id)) == []
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0034") :]


def test_0035_places_plugins_in_projects_and_going_back_drops_the_grants_configs_and_run_records(hub_db):
    move_to(hub_db, "0034")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        plugins = t.plugins
        plugin_id = one(conn, insert(plugins).values(name="sandbox", publisher="acme").returning(plugins.c.id))
        conn.execute(_plugin_version(plugin_id, ids["user"]))
        profile = {"project_id": ids["project"], "handle": "helper", "name": "Helper", "color": "#336699"}
        profile |= {"permissions": {}, "created_by": ids["user"]}
        profile_id = one(conn, insert(t.agent_profiles).values(profile).returning(t.agent_profiles.c.id))
        run_id = one(conn, _author_run(ids))
    move_to(hub_db, "0035")
    configs, runs = t.plugin_configs, t.runs
    by = {"changed_by": ids["user"], "plugin_id": plugin_id}
    with live.connect(hub_db) as conn:
        enabled = {"project_id": ids["project"], "plugin_id": plugin_id, "permissions": ["runs:read"]}
        conn.execute(insert(t.project_plugins).values(enabled | {"enabled_by": ids["user"], "granted_by": ids["user"]}))
        conn.execute(insert(configs).values(by | {"layer": "instance", "version": 1, "values": {"greeting": "hi"}}))
        conn.execute(insert(configs).values(by | {"layer": "instance", "version": 2, "rolled_back_to": 1}))
        conn.execute(insert(configs).values(by | {"layer": "project", "project_id": ids["project"], "version": 1}))
        agent = {"layer": "agent", "project_id": ids["project"], "profile_id": profile_id, "version": 1}
        conn.execute(insert(configs).values(by | agent))
        conn.execute(update(runs).values(plugin_config={"sandbox": {"greeting": "run"}}).where(runs.c.id == run_id))
        record = {"run_id": run_id, "plugin_id": plugin_id, "version": "1.0.0", "config_digest": "sha256:" + "a" * 64}
        conn.execute(insert(t.run_plugins).values(record))
        for bad in (
            insert(t.project_plugins).values(enabled | {"permissions": ["root:all"], "enabled_by": 1, "granted_by": 1}),
            insert(configs).values(by | {"layer": "instance", "version": 1}),  # one version 1 per layer
            insert(configs).values(by | {"layer": "instance", "project_id": ids["project"], "version": 3}),
            insert(configs).values(by | {"layer": "agent", "project_id": ids["project"], "version": 2}),
            insert(configs).values(by | {"layer": "hub", "version": 1}),
            insert(configs).values(by | {"layer": "instance", "version": 3, "rolled_back_to": 3}),
            insert(configs).values(by | {"layer": "instance", "version": 3, "values": [1]}),
            update(runs).values(plugin_config=[1]).where(runs.c.id == run_id),
            insert(t.run_plugins).values(record | {"version": "9.9.9"}),
            update(t.run_plugins).values(config_digest="md5:00"),
        ):
            with pytest.raises((errors.CheckViolation, errors.UniqueViolation, errors.ForeignKeyViolation)):
                conn.execute(bad)

    # Going back drops the grants, the config versions, the run's own layer and the run records; the run stays.
    move_to(hub_db, "0034", down=True)
    gone = {"project_plugins", "plugin_configs", "run_plugins"}
    assert not gone & tables(hub_db)
    assert query(hub_db, select(t.runs.c.id)) == [(run_id,)]
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0035") :]


def test_0036_keeps_plugin_tokens_and_backends_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0035")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        plugin_id = one(conn, insert(t.plugins).values(name="sandbox", publisher="acme").returning(t.plugins.c.id))
        conn.execute(_plugin_version(plugin_id, ids["user"]))
    move_to(hub_db, "0036")
    backend = {"plugin_id": plugin_id, "version": "1.0.0", "state": "running", "isolation": "uid", "pid": 42}
    with live.connect(hub_db) as conn:
        conn.execute(insert(t.plugin_tokens).values(plugin_id=plugin_id, token_hash="a" * 64))
        conn.execute(insert(t.plugin_backends).values(backend))
        for bad in (
            insert(t.plugin_tokens).values(plugin_id=plugin_id, token_hash="a" * 64),  # one row per token
            insert(t.plugin_tokens).values(plugin_id=plugin_id, token_hash="evp_plaintext"),
            insert(t.plugin_tokens).values(plugin_id=plugin_id + 1, token_hash="b" * 64),
            update(t.plugin_backends).values(state="crashed"),
            update(t.plugin_backends).values(isolation="none"),
            update(t.plugin_backends).values(restarts=-1),
            update(t.plugin_backends).values(error="x" * 4001),
        ):
            with pytest.raises((errors.CheckViolation, errors.UniqueViolation, errors.ForeignKeyViolation)):
                conn.execute(bad)
        conn.execute(delete(t.plugins).where(t.plugins.c.id == plugin_id))  # removing a plugin takes its rows
        assert one(conn, select(func.count()).select_from(t.plugin_tokens)) == 0
        assert one(conn, select(func.count()).select_from(t.plugin_backends)) == 0

    move_to(hub_db, "0035", down=True)
    assert not {"plugin_tokens", "plugin_backends"} & tables(hub_db)
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0036") :]


def test_0037_keeps_the_versions_of_branding_and_feature_toggles_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0036")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    move_to(hub_db, "0037")
    row = {"kind": "features", "project_id": ids["project"], "version": 1, "values": {"kg": False}}
    row["changed_by"] = ids["user"]
    image = {"project_id": None, "size": 10, "created_by": ids["user"]}
    with live.connect(hub_db) as conn:
        conn.execute(insert(t.hub_settings).values(row))
        conn.execute(insert(t.hub_settings).values({**row, "kind": "branding", "project_id": None, "values": {}}))
        conn.execute(insert(t.blobs).values({**image, "sha256": "c" * 64, "kind": "branding-image"}))
        for bad in (
            insert(t.hub_settings).values(row),  # one row per version of a layer
            insert(t.hub_settings).values({**row, "kind": "theme", "version": 2}),
            insert(t.hub_settings).values({**row, "kind": "branding"}),  # the hub's alone, never a project's
            insert(t.hub_settings).values({**row, "version": 0}),
            insert(t.hub_settings).values({**row, "version": 2, "rolled_back_to": 2}),
            insert(t.hub_settings).values({**row, "version": 2, "values": [1]}),
            insert(t.blobs).values({**image, "sha256": "d" * 64, "kind": "agent-avatar"}),  # a project's kind
        ):
            with pytest.raises((errors.CheckViolation, errors.UniqueViolation)):
                conn.execute(bad)

    move_to(hub_db, "0036", down=True)
    assert "hub_settings" not in tables(hub_db)
    assert query(hub_db, select(t.blobs.c.sha256).where(t.blobs.c.kind == "branding-image")) == []
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0037") :]


def test_0038_keeps_plugin_deliveries_and_image_plugins_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0037")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        plugin_id = one(conn, insert(t.plugins).values(name="sandbox", publisher="acme").returning(t.plugins.c.id))
        conn.execute(_plugin_version(plugin_id, ids["user"]))
        image = {"signature_kind": "image", "signed_by": "image:pkg:MANIFEST", "published_by": None}
        with pytest.raises((errors.CheckViolation, errors.NotNullViolation)):  # a version had a publisher before 0038
            conn.execute(_plugin_version(plugin_id, ids["user"], version="2.0.0", **image))
    move_to(hub_db, "0038")
    core = {"name": "notes", "publisher": "evo-agents"}
    delivery = {"plugin_id": plugin_id, "project_id": ids["project"], "kind": "run.state", "contribution": "runs"}
    delivery["payload"] = {"run_id": 1}
    with live.connect(hub_db) as conn:
        core_id = one(conn, insert(t.plugins).values(core).returning(t.plugins.c.id))
        conn.execute(_plugin_version(core_id, ids["user"], tier="core", **image))
        installed = {"installed_version": "1.0.0", "installed_by": None, "installed_at": func.now()}  # by nobody
        conn.execute(update(t.plugins).values(installed).where(t.plugins.c.id == core_id))
        conn.execute(insert(t.plugin_deliveries).values(delivery))
        for bad in (
            _plugin_version(plugin_id, ids["user"], version="2.0.0", published_by=None),  # signed, by nobody
            _plugin_version(plugin_id, ids["user"], version="2.0.0", signature_kind="image"),  # an image's, by someone
            update(t.plugins).values(installed_at=None).where(t.plugins.c.id == core_id),
            insert(t.plugin_deliveries).values({**delivery, "kind": "run.started"}),
            insert(t.plugin_deliveries).values({**delivery, "contribution": "Runs!"}),
            insert(t.plugin_deliveries).values({**delivery, "state": "lost"}),
            insert(t.plugin_deliveries).values({**delivery, "state": "delivered"}),  # without its time
            insert(t.plugin_deliveries).values({**delivery, "attempts": -1}),
            insert(t.plugin_deliveries).values({**delivery, "last_error": "x" * 2001}),
        ):
            with pytest.raises((errors.CheckViolation, errors.NotNullViolation)):
                conn.execute(bad)

    move_to(hub_db, "0037", down=True)
    assert "plugin_deliveries" not in tables(hub_db)
    assert query(hub_db, select(t.plugins.c.name)) == [("sandbox",)]  # the image's plugin went with its versions
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0038") :]


def test_0039_takes_runs_of_plugins_runtimes_and_the_plugins_of_a_worker_and_going_back_deletes_those_runs(hub_db):
    move_to(hub_db, "0038")
    t = hub_tables
    plugin = {"requested_runtime": "sandbox/fake", "runtime": "sandbox/fake"}
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        core = one(conn, _author_run(ids))
        token = one(conn, _token(ids, kind="worker", token_hash="7" * 64, host="mac").returning(t.tokens.c.id))
        with pytest.raises(errors.CheckViolation):  # the core's runtimes alone before 0039
            conn.execute(_author_run(ids, **plugin))
    move_to(hub_db, "0039")
    with live.connect(hub_db) as conn:
        first = one(conn, _author_run(ids, **plugin))
        retry = one(conn, _author_run(ids, **plugin, attempt=2, parent_run_id=first))
        worker = one(conn, _worker_row(ids, token, "mac", 1))
        assert query(hub_db, select(t.workers.c.plugins).where(t.workers.c.id == worker)) == [([],)]
        conn.execute(update(t.workers).values(plugins=["sandbox", "srs-docs"]).where(t.workers.c.id == worker))
        for bad in (
            _author_run(ids, requested_runtime="Sandbox/fake", runtime="Sandbox/fake"),
            _author_run(ids, requested_runtime="sandbox/fake/x", runtime="sandbox/fake/x"),
            update(t.workers).values(plugins=["Sandbox"]).where(t.workers.c.id == worker),
            update(t.workers).values(plugins=['sandbox","x']).where(t.workers.c.id == worker),
            update(t.workers).values(plugins=[f"p{n}" for n in range(17)]).where(t.workers.c.id == worker),
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(bad)
    assert retry > first
    move_to(hub_db, "0038", down=True)
    assert query(hub_db, select(t.runs.c.id)) == [(core,)], "the runs of the plugin's runtime went, retries first"
    assert "plugins" not in columns(hub_db, "workers")
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0039") :]


def _profile_row(ids, handle: str, permissions: dict, *, default: bool = False):
    """The INSERT of an agent profile of demo, the default one with ``default``, returning its id."""
    ap = hub_tables.agent_profiles
    row = {"project_id": ids["project"], "handle": handle, "name": handle, "color": "#475569"}
    row |= {"is_default": default, "permissions": permissions, "created_by": ids["user"]}
    return insert(ap).values(row).returning(ap.c.id)


def test_0040_gives_every_profile_the_environment_axis_and_runs_their_environment_and_going_back_takes_them(hub_db):
    move_to(hub_db, "0039")
    t, ap = hub_tables, hub_tables.agent_profiles
    seven = {axis: {"mode": "all"} for axis in ("credential", "tool", "skill", "worker", "project", "model", "runtime")}
    eight = {**seven, "environment": {"mode": "all"}}
    limited = {**seven, "tool": {"mode": "allowlist", "allow": ["read"]}}
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        default = one(conn, _profile_row(ids, "demo", seven, default=True))
        reader = one(conn, _profile_row(ids, "reader", limited))
        with pytest.raises(errors.CheckViolation):  # a default profile's seven axes, all, before 0040
            conn.execute(update(ap).values(permissions=eight).where(ap.c.id == default))
    move_to(hub_db, "0040")
    held = dict(query(hub_db, select(ap.c.id, ap.c.permissions)))
    assert held == {default: eight, reader: {**limited, "environment": {"mode": "all"}}}
    boxed = {**eight, "environment": {"mode": "allowlist", "allow": ["sandbox/box", "host"]}}
    with live.connect(hub_db) as conn:
        conn.execute(update(ap).values(permissions=boxed).where(ap.c.id == reader))
        for bad in (
            update(ap).values(permissions=seven).where(ap.c.id == default),  # the eight axes, all, now
            update(ap).values(permissions=boxed).where(ap.c.id == default),
            _author_run(ids, environment="host"),  # NULL is the host
            _author_run(ids, environment="Sandbox/box"),
            _author_run(ids, environment="sandbox/box/x"),
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(bad)
        boxed_run = one(conn, _author_run(ids, environment="sandbox/box"))
        host_run = one(conn, _author_run(ids))
    on = dict(query(hub_db, select(t.runs.c.id, t.runs.c.environment)))
    assert on == {boxed_run: "sandbox/box", host_run: None}
    move_to(hub_db, "0039", down=True)
    assert "environment" not in columns(hub_db, "runs")
    held = dict(query(hub_db, select(ap.c.id, ap.c.permissions)))
    assert held == {default: seven, reader: seven}, "the axis went, its allowlist with it"
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0040") :]


def test_0041_gives_every_profile_an_empty_list_of_agent_packs_and_going_back_drops_it(hub_db):
    move_to(hub_db, "0040")
    ap = hub_tables.agent_profiles
    axes = ("credential", "tool", "skill", "worker", "project", "model", "runtime", "environment")
    eight = {axis: {"mode": "all"} for axis in axes}
    pack = {"plugin": "sandbox", "name": "review", "version": "1.0.0"}
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        default = one(conn, _profile_row(ids, "demo", eight, default=True))
        reader = one(conn, _profile_row(ids, "reader", {**eight, "tool": {"mode": "allowlist", "allow": ["read"]}}))
    move_to(hub_db, "0041")
    assert dict(query(hub_db, select(ap.c.id, ap.c.packs))) == {default: [], reader: []}
    with live.connect(hub_db) as conn:
        conn.execute(update(ap).values(packs=[pack]).where(ap.c.id == reader))
        for bad in (
            update(ap).values(packs=[pack]).where(ap.c.id == default),  # a default profile attaches none
            update(ap).values(packs={"plugin": "sandbox"}).where(ap.c.id == reader),  # a list only
            update(ap).values(packs=[pack] * 17).where(ap.c.id == reader),  # at most 16
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(bad)
    assert dict(query(hub_db, select(ap.c.id, ap.c.packs))) == {default: [], reader: [pack]}
    move_to(hub_db, "0040", down=True)
    assert "packs" not in columns(hub_db, "agent_profiles")
    with live.connect(hub_db) as conn:
        with pytest.raises(errors.CheckViolation):  # the check of schema 0040 is back
            conn.execute(update(ap).values(instructions="be brief").where(ap.c.id == default))
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0041") :]


def _task_run(ids, agent_type_id: int | None, **extra):
    """The INSERT of a queued task run of the seeded user in demo for agent type ``agent_type_id``."""
    runs = hub_tables.runs
    row = {"project_id": ids["project"], "dispatched_by": ids["user"], "kind": "task", "request": "Fix the wait."}
    row |= {"requested_runtime": "any", "runtime": "any", "mode": "headless", "approval": "auto", "timeout_s": 3600}
    row |= {"state": "queued", "branch": "task/1", "agent_type_id": agent_type_id, "pulls": []}
    return insert(runs).values(row | extra).returning(runs.c.id)


def test_0042_takes_agent_types_and_task_runs_and_going_back_deletes_them(hub_db):
    move_to(hub_db, "0041")
    t = hub_tables
    eight = {
        axis: {"mode": "all"}
        for axis in ("credential", "tool", "skill", "worker", "project", "model", "runtime", "environment")
    }
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        plugin_id = one(conn, insert(t.plugins).values(name="sandbox", publisher="acme").returning(t.plugins.c.id))
        conn.execute(_plugin_version(plugin_id, ids["user"]))
        profile_id = one(conn, _profile_row(ids, "triager", eight))
        author = one(conn, _author_run(ids))
        delivery = {"plugin_id": plugin_id, "project_id": ids["project"], "kind": "task.result", "contribution": "runs"}
        delivery["payload"] = {"task_id": 1}
        with pytest.raises(errors.CheckViolation):  # no event task.result before 0042
            conn.execute(insert(t.plugin_deliveries).values(delivery))
    move_to(hub_db, "0042")
    agent_types = t.project_agent_types
    row = {"project_id": ids["project"], "plugin_id": plugin_id, "name": "triage", "profile_id": profile_id}
    row |= {"run_as": ids["user"], "daily_usd": 5.0, "enabled_by": ids["user"]}
    pull = {"repo": "app", "url": "https://github.com/acme/app/pull/1", "number": 1}
    with live.connect(hub_db) as conn:
        type_id = one(conn, insert(agent_types).values(row).returning(agent_types.c.id))
        first = one(conn, _task_run(ids, type_id))
        one(conn, _task_run(ids, type_id, attempt=2, parent_run_id=first, pulls=[pull], repos=null()))
        conn.execute(insert(t.plugin_deliveries).values(delivery))
        conn.execute(insert(t.plugin_deliveries).values({**delivery, "kind": "run.state"}))
        for bad in (
            _task_run(ids, None),  # a task run names its agent type
            _task_run(ids, type_id, request=None),  # and its task
            _author_run(ids, agent_type_id=type_id),  # and only a task run names one
            _task_run(ids, type_id, pulls={"repo": "app"}),  # a list of pull requests only
            _task_run(ids, type_id, pulls=[pull] * 51),  # at most 50
            insert(agent_types).values({**row, "name": "other", "daily_usd": 0}),
            insert(agent_types).values({**row, "name": "other", "daily_usd": 1000.5}),
            insert(agent_types).values({**row, "name": "Other"}),
            insert(agent_types).values({**row, "name": "other", "workers": [f"w{n}" for n in range(17)]}),
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(bad)
        with pytest.raises(errors.UniqueViolation):  # a project enables a type once
            conn.execute(insert(agent_types).values(row))
    assert query(hub_db, select(t.runs.c.kind).order_by(t.runs.c.id)) == [("author",), ("task",), ("task",)]

    # Going back deletes the task runs, the retry included, and the deliveries of task.result; the rest stays.
    move_to(hub_db, "0041", down=True)
    assert "project_agent_types" not in tables(hub_db)
    assert not {"agent_type_id", "pulls"} & columns(hub_db, "runs")
    assert query(hub_db, select(t.runs.c.id, t.runs.c.kind)) == [(author, "author")]
    assert query(hub_db, select(t.plugin_deliveries.c.kind)) == [("run.state",)]
    with live.connect(hub_db) as conn:
        with pytest.raises(errors.CheckViolation):  # the checks of schema 0041 are back
            conn.execute(insert(t.plugin_deliveries).values(delivery))
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0042") :]


def test_0044_keeps_triage_threads_and_a_project_switch_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0043")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    move_to(hub_db, "0044")
    with live.connect(hub_db) as conn:
        assert one(conn, select(t.projects.c.triage_enabled).where(t.projects.c.id == ids["project"])) is True
        chat = one(conn, insert(t.chats).values(title="Triage: p", created_by=ids["user"]).returning(t.chats.c.id))
        row = {"project_id": ids["project"], "chat_id": chat}
        conn.execute(insert(t.triage_chats).values({**row, "plan_id": "p"}))
        other = one(conn, insert(t.chats).values(title="Triage: demo", created_by=ids["user"]).returning(t.chats.c.id))
        conn.execute(insert(t.triage_chats).values({**row, "chat_id": other, "plan_id": None}))
        with pytest.raises(errors.UniqueViolation):  # a project without a plan has one triage chat, NULL counting once
            conn.execute(insert(t.triage_chats).values({**row, "plan_id": None, "chat_id": chat}))
    move_to(hub_db, "0043", down=True)
    assert not {"triage_chats", "run_triage"} & tables(hub_db)
    assert "triage_enabled" not in columns(hub_db, "projects")
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0044") :]


def test_0045_lets_a_decision_be_of_category_triage_and_going_back_deletes_those(hub_db):
    move_to(hub_db, "0044")
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        run_id = one(conn, _author_run(ids))
        with pytest.raises(errors.CheckViolation):
            conn.execute(_decision_row(ids, run_id, "p").values(category="triage"))
    move_to(hub_db, "0045")
    with live.connect(hub_db) as conn:
        conn.execute(_decision_row(ids, run_id, "p").values(category="triage"))
        kept = one(conn, _decision_row(ids, run_id, "p"))
    move_to(hub_db, "0044", down=True)
    assert query(hub_db, select(hub_tables.decisions.c.id)) == [(kept,)]
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0045") :]


def test_0047_keeps_tasks_one_active_sprint_and_links_of_one_kind_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0047")
    k = hub_tables.tasks
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        sprint = {"project_id": ids["project"], "created_by": ids["user"], "state": "active"}
        conn.execute(insert(k.sprints).values(name="Sprint 1", **sprint))
        with pytest.raises(errors.UniqueViolation):  # one active sprint per project
            conn.execute(insert(k.sprints).values(name="Sprint 2", **sprint))
    with live.connect(hub_db) as conn:
        values = {"project_id": ids["project"], "number": 1, "title": "Cancel", "status": "todo", "label": {}}
        task = one(conn, insert(k.items).values(**values, created_by=ids["user"]).returning(k.items.c.id))
        link = {"task_id": task, "created_by": ids["user"]}
        conn.execute(insert(k.links).values(kind="pull", url="https://example.invalid/pull/1", **link))
        with pytest.raises(errors.CheckViolation):  # a plan step link names no URL
            conn.execute(insert(k.links).values(kind="plan_step", plan_id="p", step_id="1", url="https://x", **link))
    move_to(hub_db, "0046", down=True)
    assert not pg.TASKS_TABLES & tables(hub_db)
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0047") :]


def test_0048_holds_every_worker_unlocked_checks_the_commit_and_the_report_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0047")
    workers = hub_tables.workers
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        token_id = one(
            conn, _token(ids, kind="worker", token_hash="4" * 64, host="mac").returning(hub_tables.tokens.c.id)
        )
        worker = one(conn, _worker_row(ids, token_id, "mac", 1))
    move_to(hub_db, "0048")
    added = {"version_locked", "agent_commit", "self_update", "self_update_since"}
    assert added <= columns(hub_db, "workers")
    shown = select(workers.c.version_locked, workers.c.agent_commit, workers.c.self_update, workers.c.self_update_since)
    assert query(hub_db, shown.where(workers.c.id == worker)) == [(False, None, None, None)]
    report = {"state": "waiting", "nightly_commit": "f" * 40}
    with live.connect(hub_db) as conn:
        for bad in ({"agent_commit": "ca3e9af"}, {"agent_commit": "F" * 40}, {"self_update": ["waiting"]}):
            with pytest.raises(errors.CheckViolation):
                conn.execute(update(workers).values(**bad).where(workers.c.id == worker))
        good = {
            "version_locked": True,
            "agent_commit": "a" * 64,
            "self_update": report,
            "self_update_since": func.now(),
        }
        conn.execute(update(workers).values(**good).where(workers.c.id == worker))
    assert query(hub_db, shown.where(workers.c.id == worker))[0][:3] == (True, "a" * 64, report)
    move_to(hub_db, "0047", down=True)
    assert not added & columns(hub_db, "workers")
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0048") :]


def test_0049_gives_every_run_an_empty_seen_audience_and_going_back_drops_it(hub_db):
    move_to(hub_db, "0048")
    runs = hub_tables.runs
    with live.connect(hub_db) as conn:
        run_id = one(conn, _author_run(seed(conn)))
    move_to(hub_db, "0049")
    assert "seen_audience" in columns(hub_db, "runs")
    assert query(hub_db, select(runs.c.seen_audience).where(runs.c.id == run_id)) == [([],)]
    with live.connect(hub_db) as conn:
        conn.execute(update(runs).values(seen_audience=["chat:5", "grant:demo:internal"]).where(runs.c.id == run_id))
    assert query(hub_db, select(runs.c.seen_audience).where(runs.c.id == run_id)) == [
        (["chat:5", "grant:demo:internal"],)
    ]
    move_to(hub_db, "0048", down=True)
    assert "seen_audience" not in columns(hub_db, "runs")
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0049") :]


def test_0053_keeps_a_workers_kg_sync_report_an_object_and_going_back_drops_it(hub_db):
    move_to(hub_db, "0052")
    workers = hub_tables.workers
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        token_id = one(
            conn, _token(ids, kind="worker", token_hash="5" * 64, host="mac").returning(hub_tables.tokens.c.id)
        )
        worker = one(conn, _worker_row(ids, token_id, "mac", 1))
    move_to(hub_db, "0053")
    assert "kg_sync" in columns(hub_db, "workers")
    shown = select(workers.c.kg_sync).where(workers.c.id == worker)
    assert query(hub_db, shown) == [(None,)]
    report = {"projects": [{"project": "demo", "checked_at": "2026-10-19T00:00:00+00:00", "sources": []}]}
    with live.connect(hub_db) as conn:
        with pytest.raises(errors.CheckViolation):
            conn.execute(update(workers).values(kg_sync=["demo"]).where(workers.c.id == worker))
    with live.connect(hub_db) as conn:
        conn.execute(update(workers).values(kg_sync=report).where(workers.c.id == worker))
    assert query(hub_db, shown) == [(report,)]
    move_to(hub_db, "0052", down=True)
    assert "kg_sync" not in columns(hub_db, "workers")
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0053") :]


def test_0062_holds_every_worker_unshared_and_going_back_drops_the_column(hub_db):
    move_to(hub_db, "0061")
    workers = hub_tables.workers
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        token_id = one(
            conn, _token(ids, kind="worker", token_hash="6" * 64, host="mac").returning(hub_tables.tokens.c.id)
        )
        worker = one(conn, _worker_row(ids, token_id, "mac", 1))
    move_to(hub_db, "0062")
    shown = select(workers.c.shared).where(workers.c.id == worker)
    assert query(hub_db, shown) == [(False,)]
    with live.connect(hub_db) as conn:
        conn.execute(update(workers).values(shared=True).where(workers.c.id == worker))
    assert query(hub_db, shown) == [(True,)]
    move_to(hub_db, "0061", down=True)
    assert "shared" not in columns(hub_db, "workers")
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0062") :]


def test_0063_makes_telegrams_codes_link_codes_of_kind_telegram_and_going_back_keeps_those_alone(hub_db):
    move_to(hub_db, "0062")
    old = table("telegram_links", column("user_id"), column("code_hash", Text), column("expires_at"))
    soon = func.now() + timedelta(minutes=10)
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        conn.execute(insert(old).values(user_id=ids["user"], code_hash="a" * 64, expires_at=soon))
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0063")
    codes = hub_tables.channel_link_codes
    assert "telegram_links" not in tables(hub_db)
    assert query(hub_db, select(codes.c.code_hash, codes.c.kind)) == [("a" * 64, "telegram")]
    named = select(PG_CONSTRAINT.c.conname).where(
        PG_CONSTRAINT.c.conrelid == cast(literal("channel_link_codes", Text), REGCLASS)
    )
    assert all(name.startswith("channel_link_codes_") for (name,) in query(hub_db, named))
    with live.connect(hub_db) as conn:
        conn.execute(insert(codes).values(user_id=ids["user"], kind="northwind", code_hash="b" * 64, expires_at=soon))
        with pytest.raises(errors.CheckViolation):
            conn.execute(insert(codes).values(user_id=ids["user"], kind="web", code_hash="c" * 64, expires_at=soon))
        with pytest.raises(errors.NotNullViolation):  # no default: each code names its kind
            conn.execute(insert(codes).values(user_id=ids["user"], code_hash="d" * 64, expires_at=soon))
    move_to(hub_db, "0062", down=True)
    assert query(hub_db, select(old.c.code_hash)) == [("a" * 64,)]  # the northwind code went, Telegram's stays
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0063") :]


def test_0064_lets_a_version_in_review_be_withdrawn_and_going_back_makes_it_a_draft_again(hub_db):
    move_to(hub_db, "0063")
    s = hub_tables.srs
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        document = {"project_id": ids["project"], "slug": "demo-shop", "title": "Demo shop", "created_by": ids["user"]}
        doc = one(conn, insert(s.documents).values(**document, label={"level": "internal"}).returning(s.documents.c.id))
        version = {"document_id": doc, "author_id": ids["user"], "content_sha256": "a" * 64, "submitted_at": func.now()}
        conn.execute(insert(s.versions).values(number=1, state="in_review", round=1, **version))
        with pytest.raises(errors.CheckViolation):  # 0046 knows no withdrawn version
            conn.execute(update(s.versions).values(state="withdrawn", decided_at=func.now()))
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0064")
    with live.connect(hub_db) as conn:
        with pytest.raises(errors.CheckViolation):  # a withdrawn version carries the moment it was withdrawn
            conn.execute(update(s.versions).values(state="withdrawn"))
        conn.execute(update(s.versions).values(state="withdrawn", decided_at=func.now()))
    assert query(hub_db, select(s.versions.c.state)) == [("withdrawn",)]
    move_to(hub_db, "0063", down=True)
    assert query(hub_db, select(s.versions.c.state, s.versions.c.decided_at)) == [("draft", None)]
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0064") :]


def test_0065_adds_the_read_of_nightly_and_the_update_now_of_a_worker_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0064")
    workers, nightly = hub_tables.workers, hub_tables.hub_nightly
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        token_id = one(
            conn, _token(ids, kind="worker", token_hash="4" * 64, host="mac").returning(hub_tables.tokens.c.id)
        )
        worker = one(conn, _worker_row(ids, token_id, "mac", 1))
    move_to(hub_db, "0065")
    added = {"update_requested_at", "update_ended_at", "update_outcome", "update_reason"}
    assert added <= columns(hub_db, "workers")
    assert columns(hub_db, "hub_nightly") == {"id", "commit_sha", "read_at"}
    shown = select(*(workers.c[name] for name in sorted(added))).where(workers.c.id == worker)
    assert query(hub_db, shown) == [(None, None, None, None)]
    asked, ended = {"update_requested_at": func.now()}, {"update_ended_at": func.now()}
    bad_requests = (
        ended | {"update_outcome": "current"},  # an end without a request
        asked | ended,  # an end without an outcome
        asked | {"update_outcome": "current"},  # an outcome without an end
        asked | ended | {"update_outcome": "stuck"},
        asked | {"update_reason": "why"},  # a reason without an outcome
    )
    with live.connect(hub_db) as conn:
        for bad in bad_requests:
            with pytest.raises(errors.CheckViolation):
                conn.execute(update(workers).values(**bad).where(workers.c.id == worker))
        conn.execute(update(workers).values(**asked).where(workers.c.id == worker))
        done = ended | {"update_outcome": "failed", "update_reason": "pip exited 1"}
        conn.execute(update(workers).values(**done).where(workers.c.id == worker))
        for bad in ({"id": 2, "commit_sha": "a" * 40}, {"commit_sha": "ca3e9af"}, {"commit_sha": "A" * 40}):
            with pytest.raises(errors.CheckViolation):
                conn.execute(insert(nightly).values(read_at=func.now(), **bad))
        conn.execute(insert(nightly).values(commit_sha="b" * 40, read_at=func.now()))
        with pytest.raises(errors.UniqueViolation):
            conn.execute(insert(nightly).values(commit_sha="c" * 64, read_at=func.now()))
    assert query(hub_db, select(workers.c.update_outcome, workers.c.update_reason)) == [("failed", "pip exited 1")]
    assert query(hub_db, select(nightly.c.id, nightly.c.commit_sha)) == [(1, "b" * 40)]
    move_to(hub_db, "0064", down=True)
    assert not added & columns(hub_db, "workers")
    assert not columns(hub_db, "hub_nightly")
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0065") :]


def test_0066_adds_the_sealed_oauth_columns_to_accounts_all_set_or_all_null_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0065")
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0066")
    accounts = hub_tables.accounts
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        secret = insert(hub_tables.secrets).values(
            owner_id=ids["user"],
            name="claude-o",
            kind="env",
            env_var="CLAUDE_CODE_OAUTH_TOKEN",
            sealed=b"s" * 20,
            nonce=b"n" * 12,
            key_id="0123abcd",
        )
        secret_id = conn.execute(secret.returning(hub_tables.secrets.c.id)).scalar_one()
        conn.execute(insert(accounts).values(secret_id=secret_id))  # no tokens: every account that exists
        conn.execute(
            update(accounts)
            .values(oauth_sealed=b"x", oauth_nonce=b"n", oauth_key_id="k")
            .where(accounts.c.secret_id == secret_id)
        )
        with pytest.raises(errors.CheckViolation):  # a set is whole or absent
            conn.execute(update(accounts).values(oauth_sealed=None).where(accounts.c.secret_id == secret_id))
    move_to(hub_db, "0065", down=True)
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0066") :]


def test_0068_keeps_user_emails_one_member_an_address_whatever_its_case_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0067")
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0068")
    emails, users = hub_tables.user_emails, hub_tables.users
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        other = one(conn, insert(users).values(login="lumen-dev", github_id=2).returning(users.c.id))
        conn.execute(insert(emails).values(user_id=ids["user"], email="Octo@Northwind.example"))
        with pytest.raises(errors.UniqueViolation):  # an address is one member's, whatever its case
            conn.execute(insert(emails).values(user_id=other, email="octo@northwind.EXAMPLE"))
        for wrong in (
            "octo",
            "octo@northwind",
            "oc to@northwind.example",
            "a@b@northwind.example",
            "x" * 250 + "@a.bc",
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(insert(emails).values(user_id=other, email=wrong))
        conn.execute(insert(emails).values(user_id=other, email="dev@lumen.example"))
        conn.execute(delete(users).where(users.c.id == other))  # a member's emails go with the member
    assert query(hub_db, select(emails.c.email)) == [("Octo@Northwind.example",)]
    move_to(hub_db, "0067", down=True)
    assert "user_emails" not in tables(hub_db)
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0068") :]


def test_0069_keeps_srs_findings_with_a_source_each_and_going_back_drops_them_and_the_review_run(hub_db):
    move_to(hub_db, "0068")
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0069")
    s = hub_tables.srs
    f = s.findings
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        document = {"project_id": ids["project"], "slug": "demo-shop", "title": "Demo shop", "created_by": ids["user"]}
        doc = one(conn, insert(s.documents).values(**document, label={"level": "internal"}).returning(s.documents.c.id))
        version = {"document_id": doc, "number": 1, "author_id": ids["user"], "content_sha256": "a" * 64}
        version_id = one(conn, insert(s.versions).values(**version).returning(s.versions.c.id))
        shown = [{"kind": "code", "ref": "UC PAY-1"}]
        found = {"document_id": doc, "version_id": version_id, "kind": "contradiction", "level": "decide"}
        found |= {"summary": "Two limits of retries", "label": {"level": "internal"}}
        conn.execute(insert(f).values(**found, sources=shown))
        for wrong in (
            {"sources": []},  # no source, no conflict
            {"sources": {"kind": "code"}},
            {"sources": shown, "kind": "typo"},
            {"sources": shown, "level": "urgent"},
            {"sources": shown, "summary": ""},
            {"sources": shown, "resolution": "settled"},  # an open finding carries nothing of a settling
            {"sources": shown, "status": "resolved", "resolution": "settled"},  # a settled one carries its moment
            {"sources": shown, "status": "gone", "resolution": "x", "settled_at": func.now()},
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(insert(f).values({**found, **wrong}))
        conn.execute(insert(f).values(**found, sources=shown, status="waived", resolution="x", settled_at=func.now()))
    assert query(hub_db, select(f.c.status, f.c.resolution_sources).order_by(f.c.id)) == [
        ("open", []),
        ("waived", []),
    ]
    assert "review_run_id" in columns(hub_db, "srs_versions")
    move_to(hub_db, "0068", down=True)
    assert "srs_findings" not in tables(hub_db)
    assert "review_run_id" not in columns(hub_db, "srs_versions")
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0069") :]


def test_0070_takes_settle_chats_and_their_srs_rows_and_going_back_makes_them_group_chats(hub_db):
    move_to(hub_db, "0069")
    before = query(hub_db, SNAPSHOT)
    t, s = hub_tables, hub_tables.srs
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        profile = {"project_id": ids["project"], "handle": "ba", "name": "BA", "color": "#336699"}
        profile |= {"permissions": {}, "created_by": ids["user"]}
        profile_id = one(conn, insert(t.agent_profiles).values(profile).returning(t.agent_profiles.c.id))
        settle = {"title": "Conflicts of demo-shop v1", "created_by": ids["user"], "kind": "settle"}
        with pytest.raises(errors.CheckViolation):  # 0050 knows group and direct chats alone
            conn.execute(insert(t.chats).values(**settle, direct_profile_id=profile_id))
    move_to(hub_db, "0070")
    with live.connect(hub_db) as conn:
        pairs = ({"direct_profile_id": None}, {"direct_profile_id": profile_id, "direct_user_id": ids["user"]})
        for wrong in pairs:  # a settle chat names its agent, and no person
            with pytest.raises(errors.CheckViolation):
                conn.execute(insert(t.chats).values(**settle, **wrong))
        chat = one(conn, insert(t.chats).values(**settle, direct_profile_id=profile_id).returning(t.chats.c.id))
        twin = one(conn, insert(t.chats).values(**settle, direct_profile_id=profile_id).returning(t.chats.c.id))
        message = {"chat_id": chat, "seq": 1, "author_user_id": ids["user"], "body": "keep 3", "label": []}
        message_id = one(conn, insert(t.chat_messages).values(message).returning(t.chat_messages.c.id))
        mention = {"message_id": message_id, "ordinal": 0, "name": "ba", "profile_id": profile_id}
        conn.execute(insert(t.chat_mentions).values(**mention, via="settle"))
        document = {"project_id": ids["project"], "slug": "demo-shop", "title": "Demo shop", "created_by": ids["user"]}
        doc = one(conn, insert(s.documents).values(**document, label={"level": "internal"}).returning(s.documents.c.id))
        version = {"document_id": doc, "number": 1, "author_id": ids["user"], "content_sha256": "a" * 64}
        version_id = one(conn, insert(s.versions).values(**version).returning(s.versions.c.id))
        opened = {"version_id": version_id, "chat_id": chat, "root_message_id": message_id}
        conn.execute(insert(s.settle_chats).values(opened))
        with pytest.raises(errors.UniqueViolation):  # one chat a version
            conn.execute(insert(s.settle_chats).values({**opened, "chat_id": twin}))
        found = {"document_id": doc, "version_id": version_id, "kind": "contradiction", "level": "decide"}
        found |= {"summary": "Two limits", "label": {"level": "internal"}, "sources": [{"kind": "code", "ref": "X"}]}
        conn.execute(insert(s.findings).values(**found))
        with pytest.raises(errors.CheckViolation):
            conn.execute(insert(s.findings).values(**found, commit_authors={"a": "b"}))
    assert query(hub_db, select(s.findings.c.commit_authors)) == [([],)]
    move_to(hub_db, "0069", down=True)
    c, m = t.chats, t.chat_mentions
    kinds = query(hub_db, select(c.c.kind, c.c.direct_profile_id).where(c.c.id.in_([chat, twin])))
    assert kinds == [("group", None), ("group", None)]
    assert query(hub_db, select(m.c.via)) == [("thread",)]
    assert "srs_settle_chats" not in tables(hub_db)
    assert "commit_authors" not in columns(hub_db, "srs_findings")
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0070") :]


def test_0071_keeps_one_srs_approval_tasks_row_a_version_and_going_back_drops_it(hub_db):
    move_to(hub_db, "0070")
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0071")
    s = hub_tables.srs
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        document = {"project_id": ids["project"], "slug": "demo-shop", "title": "Demo shop", "created_by": ids["user"]}
        doc = one(conn, insert(s.documents).values(**document, label={"level": "internal"}).returning(s.documents.c.id))
        versions = [
            one(
                conn,
                insert(s.versions)
                .values(document_id=doc, number=number, author_id=ids["user"], content_sha256="a" * 64)
                .returning(s.versions.c.id),
            )
            for number in (1, 2)
        ]
        done = {"version_id": versions[0], "approved_by": ids["user"]}
        conn.execute(insert(s.approval_tasks).values(done))
        with pytest.raises(errors.UniqueViolation):  # one row a version
            conn.execute(insert(s.approval_tasks).values(done))
        with pytest.raises(errors.CheckViolation):  # its outcome is an object
            conn.execute(insert(s.approval_tasks).values(version_id=versions[1], approved_by=ids["user"], outcome=[]))
    a = s.approval_tasks
    assert query(hub_db, select(a.c.previous_version, a.c.split_run_id, a.c.outcome)) == [(None, None, {})]
    move_to(hub_db, "0070", down=True)
    assert "srs_approval_tasks" not in tables(hub_db)
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0071") :]


def test_0072_drops_the_chat_budget_and_going_back_puts_it_back_at_5(hub_db):
    move_to(hub_db, "0071")
    before = query(hub_db, SNAPSHOT)
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        chat_id = one(conn, insert(t.chats).values(title="Ops", created_by=ids["user"]).returning(t.chats.c.id))
        conn.execute(update(CHATS_0032).values(budget_usd=20).where(CHATS_0032.c.id == chat_id))
    move_to(hub_db, "0072")
    assert "budget_usd" not in columns(hub_db, "chats")
    assert query(hub_db, select(t.chats.c.title)) == [("Ops",)]
    move_to(hub_db, "0071", down=True)
    assert query(hub_db, select(CHATS_0032.c.budget_usd)) == [(5.0,)]  # what the creator set is gone
    with live.connect(hub_db) as conn, pytest.raises(errors.CheckViolation):
        conn.execute(update(CHATS_0032).values(budget_usd=0).where(CHATS_0032.c.id == chat_id))
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0072") :]


def test_0073_keeps_canvases_claims_revs_and_evidence_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0073")
    k = hub_tables.canvas
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        owner = {"project_id": ids["project"], "created_by": ids["user"]}
        canvas = one(
            conn,
            insert(k.canvases).values(slug="northwind", name="Northwind", label={}, **owner).returning(k.canvases.c.id),
        )
        claim = {"canvas_id": canvas, "number": 1, "cell": "problem", "text": "Shops run out", "label": {}, **owner}
        claim_id = one(conn, insert(k.claims).values(**claim).returning(k.claims.c.id))
        with pytest.raises(errors.UniqueViolation):  # a claim's number is the project's, never given twice
            conn.execute(insert(k.claims).values(**{**claim, "cell": "solution"}))
        with pytest.raises(errors.CheckViolation):  # nine cells, no tenth
            conn.execute(insert(k.claims).values(**{**claim, "number": 2, "cell": "moon"}))
        rev = {"claim_id": claim_id, "rev": 1, "content": {"cell": "problem"}, "change": "create", "actor": "person"}
        conn.execute(insert(k.revs).values(**rev, user_id=ids["user"]))
        with pytest.raises(errors.CheckViolation):  # a person's rev names the person
            conn.execute(insert(k.revs).values(**{**rev, "rev": 2}))
        piece = {"claim_id": claim_id, "kind": "simulation", "direction": "supports", "summary": "x", "label": {}}
        with pytest.raises(errors.CheckViolation):  # a simulation is synthetic
            conn.execute(insert(k.evidence).values(**piece, actor="hub"))
        conn.execute(insert(k.evidence).values(**piece, actor="hub", synthetic=True))
    move_to(hub_db, "0072", down=True)
    assert not pg.CANVAS_TABLES & tables(hub_db)
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0073") :]


def test_0074_keeps_signal_keys_ingest_keys_and_signals_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0074")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        owner = {"project_id": ids["project"], "created_by": ids["user"]}
        key = one(
            conn,
            insert(t.signal_keys).values(name="kb.search.hits", kind="number", **owner).returning(t.signal_keys.c.id),
        )
        with pytest.raises(errors.CheckViolation):  # a key's name has two to six dotted parts
            conn.execute(insert(t.signal_keys).values(name="hits", kind="number", **owner))
        ingest = {"name": "northwind-kb", "token_hash": "b" * 64, "max_level": "internal", **owner}
        ingest_id = one(conn, insert(t.signal_ingest_keys).values(**ingest).returning(t.signal_ingest_keys.c.id))
        with pytest.raises(errors.UniqueViolation):  # one live key of a name in a project
            conn.execute(insert(t.signal_ingest_keys).values(**{**ingest, "token_hash": "c" * 64}))
        signal = {
            "project_id": ids["project"],
            "key_id": key,
            "value": 0.25,
            "measured_at": func.now(),
            "label": {"level": "internal"},
            "via": "ingest",
            "ingest_key_id": ingest_id,
            "batch_id": UUID(int=1),
            "external_id": "a-1",
        }
        conn.execute(insert(t.signals).values(**signal))
        with pytest.raises(errors.UniqueViolation):  # an ingest key sends an external id once
            conn.execute(insert(t.signals).values(**signal))
        with pytest.raises(errors.CheckViolation):  # a number or a text, never both
            conn.execute(insert(t.signals).values(**{**signal, "external_id": "a-2", "text": "slow"}))
        with pytest.raises(errors.CheckViolation):  # pushed by an ingest key names no member
            conn.execute(insert(t.signals).values(**{**signal, "external_id": "a-3", "user_id": ids["user"]}))
    move_to(hub_db, "0073", down=True)
    assert not pg.SIGNAL_TABLES & tables(hub_db)
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0074") :]


def test_0075_keeps_canvas_proposals_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0075")
    k = hub_tables.canvas
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        owner = {"project_id": ids["project"], "created_by": ids["user"]}
        canvas = one(
            conn,
            insert(k.canvases).values(slug="northwind", name="Northwind", label={}, **owner).returning(k.canvases.c.id),
        )
        claim = {"canvas_id": canvas, "number": 1, "cell": "problem", "text": "Shops run out", "label": {}, **owner}
        claim_id = one(conn, insert(k.claims).values(**claim).returning(k.claims.c.id))
        made = {"canvas_id": canvas, "fields": {"cell": "problem"}, "reason": "brainstorm", "label": {}}
        agent = {"actor": "agent", "user_id": ids["user"]}
        conn.execute(insert(k.proposals).values(kind="create", **made, **agent))
        conn.execute(insert(k.proposals).values(kind="change", claim_id=claim_id, base_rev=1, **made, **agent))
        decided = {"decided_by": ids["user"], "decided_at": func.now()}
        conn.execute(insert(k.proposals).values(kind="create", status="rejected", **made, **agent, **decided))
        for bad in (
            {"kind": "change", **agent},  # a change names its claim and base rev
            {"kind": "create", "base_rev": 1, **agent},  # a new claim has no base rev
            {"kind": "create", "actor": "agent"},  # an agent's proposal names the member it was for
            {"kind": "create", "status": "accepted", **agent, **decided},  # accepted names the claim and the rev
            {"kind": "create", "status": "rejected", **agent},  # rejected names who decided
            {"kind": "create", "status": "done", **agent},
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(insert(k.proposals).values(**{**made, **bad}))
        accepted = {"status": "accepted", "claim_id": claim_id, "rev": 1, **decided}
        conn.execute(insert(k.proposals).values(kind="create", **made, **agent, **accepted))
    move_to(hub_db, "0074", down=True)
    assert not pg.CANVAS_PROPOSAL_TABLES & tables(hub_db)
    assert pg.CANVAS_TABLES <= tables(hub_db)
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0075") :]


def test_0076_keeps_judgings_and_the_basis_of_hub_evidence_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0076")
    k, t = hub_tables.canvas, hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        owner = {"project_id": ids["project"], "created_by": ids["user"]}
        canvas = one(
            conn,
            insert(k.canvases).values(slug="northwind", name="Northwind", label={}, **owner).returning(k.canvases.c.id),
        )
        claim = {"canvas_id": canvas, "number": 1, "cell": "problem", "text": "Shops run out", "label": {}, **owner}
        claim_id = one(conn, insert(k.claims).values(**claim).returning(k.claims.c.id))
        key_id = one(
            conn,
            insert(t.signal_keys)
            .values(name="pantry.feedback", kind="feedback", **owner)
            .returning(t.signal_keys.c.id),
        )
        piece = {"claim_id": claim_id, "kind": "signal", "direction": "supports", "summary": "met", "label": {}}
        checked = {"check": "number", "value": 0.4, "samples": 3, "result": "met"}
        evidence_id = one(
            conn,
            insert(k.evidence)
            .values(**piece, actor="hub", basis=checked, rests_on_run=True)
            .returning(k.evidence.c.id),
        )
        person = {**piece, "kind": "interview", "actor": "person", "user_id": ids["user"]}
        for bad in (
            {**piece, "actor": "hub", "basis": [1, 2]},  # a basis is an object
            {**person, "basis": checked},  # only the hub says what evidence rests on
            {**person, "rests_on_run": True},
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(insert(k.evidence).values(**bad))
        made = {"canvas_id": canvas, "kind": "change", "claim_id": claim_id, "base_rev": 1, "label": {}}
        conn.execute(
            insert(k.proposals).values(
                **made, fields={"status": "validated"}, reason="value check", actor="hub", evidence_id=evidence_id
            )
        )
        item = {
            "canvas_id": canvas,
            "claim_id": claim_id,
            "source": "signal",
            "kind": "feedback",
            "key_id": key_id,
            "text": "We reorder every Monday",
            "label": {"level": "internal"},
            "measured_at": func.now(),
        }
        conn.execute(insert(k.judgings).values(**item))
        judged = {"verdict": "supports", "reason": "a shop owner says so", "judged_at": func.now()}
        conn.execute(insert(k.judgings).values(**item, **judged))
        evidence = {**item, "source": "evidence", "kind": "interview", "key_id": None, "evidence_id": evidence_id}
        conn.execute(insert(k.judgings).values(**evidence, **judged, verdict_evidence_id=evidence_id))
        for bad in (
            {"key_id": None},  # feedback names its key
            {"kind": "interview"},  # a signal judged is feedback
            {**evidence, "kind": "doc"},  # evidence judged is an interview, an experiment or a market figure
            {"verdict": "maybe", "reason": "x", "judged_at": func.now()},
            {"verdict": "supports"},  # a verdict says why and when
            {"reason": "x"},
            {"verdict_evidence_id": evidence_id},  # only a verdict writes evidence
            {"text": ""},
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(insert(k.judgings).values(**{**item, **bad}))
    move_to(hub_db, "0075", down=True)
    assert not pg.CANVAS_JUDGING_TABLES & tables(hub_db)
    assert not {"basis", "rests_on_run"} & columns(hub_db, "canvas_evidence")
    assert "evidence_id" not in columns(hub_db, "canvas_proposals")
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0076") :]


def test_0077_keeps_baselines_and_hub_events_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0077")
    k, t = hub_tables.canvas, hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        owner = {"project_id": ids["project"], "created_by": ids["user"]}
        canvas = one(
            conn,
            insert(k.canvases).values(slug="northwind", name="Northwind", label={}, **owner).returning(k.canvases.c.id),
        )
        claim = {"canvas_id": canvas, "number": 1, "cell": "problem", "text": "Shops run out", "label": {}, **owner}
        claim_id = one(conn, insert(k.claims).values(**claim).returning(k.claims.c.id))
        conn.execute(insert(t.hub_events).values(project_id=ids["project"], kind="canvas.claim", payload={"v": 1}))
        for bad in ({"kind": "Canvas.Claim"}, {"kind": "canvas"}, {"payload": [1]}):
            with pytest.raises(errors.CheckViolation):
                conn.execute(
                    insert(t.hub_events).values({"project_id": ids["project"], "kind": "a.b", "payload": {}} | bad)
                )
        weekly = {"canvas_id": canvas, "number": 1, "kind": "weekly", "week": "2026-W43", "label": {}}
        weekly |= {"due_at": func.now(), "time_zone": "UTC"}
        baseline = one(conn, insert(k.baselines).values(weekly).returning(k.baselines.c.id))
        pushed = {"canvas_id": canvas, "number": 2, "kind": "push", "label": {}, "created_by": ids["user"]}
        conn.execute(insert(k.baselines).values(pushed))
        for bad in (
            {"week": None},  # a weekly one names its week
            {"kind": "push"},  # one opened at once has no week, and names who opened it
            {"week": "2026-43"},
            {"status": "approved"},  # approved holds when and its snapshot
            {"status": "approved", "approved_at": func.now(), "snapshot": {"CL-1": 1}},
            {"status": "expired"},
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(insert(k.baselines).values({**weekly, "number": 9, "week": "2026-W44"} | bad))
        with pytest.raises(errors.UniqueViolation):  # once a week per canvas
            conn.execute(insert(k.baselines).values({**weekly, "number": 9}))
        item = {"baseline_id": baseline, "claim_id": claim_id, "kind": "status", "from_status": "assumption"}
        item |= {
            "at_status": "testing",
            "to_status": "testing",
            "claim_rev": 2,
            "label": {},
            "proposers": [ids["user"]],
        }
        conn.execute(insert(k.baseline_items).values(item))
        decided = {"decision": "accepted", "decided_by": ids["user"], "decided_at": func.now()}
        for bad in (
            {"to_status": None},  # a change of status names the status it asks
            {"kind": "overdue"},  # an overdue card names its deadline and asks no status
            {"kind": "new", "to_status": "testing"},
            {"proposal_id": 1},  # only the value check's names a proposal
            {"decision": "extended", "decided_by": ids["user"], "decided_at": func.now()},  # an overdue card's
            {"decision": "accepted"},  # a member's decision says who and when
            {"kind": "new", "to_status": "assumption", **decided},  # a new claim is the hub's to include
            {**decided, "new_deadline": "2026-12-01"},  # only an extension gives a deadline
            {"reason": "x"},  # a reason goes with a decision
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(insert(k.baseline_items).values({**item, "baseline_id": baseline} | bad))
        rev = {"claim_id": claim_id, "rev": 1, "content": {}, "change": "edit", "actor": "hub"}
        with pytest.raises(errors.CheckViolation):  # only a baseline's decision names one
            conn.execute(insert(k.revs).values({**rev, "baseline_id": baseline}))
        conn.execute(insert(k.revs).values({**rev, "change": "baseline", "baseline_id": baseline}))
        plugin_id = one(conn, insert(t.plugins).values(name="loop", publisher="acme").returning(t.plugins.c.id))
        conn.execute(_plugin_version(plugin_id, ids["user"]))
        granted = {"project_id": ids["project"], "plugin_id": plugin_id, "permissions": ["runs:read", "canvas:read"]}
        conn.execute(insert(t.project_plugins).values(granted | {"enabled_by": ids["user"], "granted_by": ids["user"]}))
        delivery = {"plugin_id": plugin_id, "project_id": ids["project"], "contribution": "loop", "payload": {"v": 1}}
        for kind in ("canvas.claim", "canvas.baseline", "signal.posted", "run.state"):
            conn.execute(insert(t.plugin_deliveries).values({**delivery, "kind": kind}))
        notice = {"user_id": ids["user"], "kind": "notice", "project_id": ids["project"], "title": "Baseline 1"}
        conn.execute(insert(t.notifications).values({**notice, "notice_kind": "canvas_baseline"}))
    move_to(hub_db, "0076", down=True)
    assert not pg.CANVAS_BASELINE_TABLES & tables(hub_db)
    assert "baseline_id" not in columns(hub_db, "canvas_claim_revs")
    assert query(hub_db, select(t.plugin_deliveries.c.kind)) == [("run.state",)]
    assert query(hub_db, select(t.project_plugins.c.permissions)) == [(["runs:read"],)]
    assert query(hub_db, select(t.notifications.c.id)) == []
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0077") :]


def test_0078_keeps_what_a_claim_links_and_going_back_drops_it(hub_db):
    move_to(hub_db, "0078")
    k = hub_tables.canvas
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        owner = {"project_id": ids["project"], "created_by": ids["user"]}
        canvas = one(
            conn,
            insert(k.canvases).values(slug="northwind", name="Northwind", label={}, **owner).returning(k.canvases.c.id),
        )
        claim = {"canvas_id": canvas, "cell": "solution", "text": "One tap reorders", "label": {}, **owner}
        conn.execute(insert(k.claims).values(number=1, **claim))  # links nothing by default
        linked = {"use_cases": ["UC ORD-1"], "signal_keys": ["pantry.stock.outs_per_week"]}
        conn.execute(insert(k.claims).values(number=2, **claim, **linked))
        for bad in ({"use_cases": [f"UC ORD-{n}" for n in range(21)]}, {"signal_keys": [f"a.b{n}" for n in range(21)]}):
            with pytest.raises(errors.CheckViolation):  # at most 20 of each
                conn.execute(insert(k.claims).values(number=3, **claim, **bad))
    held = select(k.claims.c.number, k.claims.c.use_cases, k.claims.c.signal_keys).order_by(k.claims.c.number)
    assert query(hub_db, held) == [(1, [], []), (2, ["UC ORD-1"], ["pantry.stock.outs_per_week"])]
    move_to(hub_db, "0077", down=True)
    assert not {"use_cases", "signal_keys"} & columns(hub_db, "canvas_claims")
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0078") :]


def test_0079_keeps_the_revisions_of_autonomy_policies_and_proposals_of_one_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0078")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    proposal = {"project_id": ids["project"], "change": {"set": {}, "unset": []}, "summary": "s", "base_version": 0}
    proposal["proposed_by"] = ids["user"]
    with live.connect(hub_db) as conn, pytest.raises(errors.CheckViolation):  # 0043 knows no policy proposal
        conn.execute(insert(t.builder_proposals).values({**proposal, "kind": "policy"}))
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0079")
    assert columns(hub_db, "autonomy_policies") == {
        "project_id",
        "revision",
        "body",
        "note",
        "created_by",
        "created_at",
    }
    row = {"project_id": ids["project"], "revision": 1, "body": {"merge": {"hold_until": "merged"}}}
    row["created_by"] = ids["user"]
    notice = {"user_id": ids["user"], "kind": "notice", "notice_kind": "builder_proposal", "title": "Policy"}
    with live.connect(hub_db) as conn:
        conn.execute(insert(t.autonomy_policies).values(row))
        conn.execute(insert(t.autonomy_policies).values({**row, "revision": 2, "body": {}, "note": "all defaults"}))
        for bad in (
            insert(t.autonomy_policies).values(row),  # one row per revision of a project
            insert(t.autonomy_policies).values({**row, "revision": 0}),
            insert(t.autonomy_policies).values({**row, "revision": 3, "body": ["merge"]}),
            insert(t.autonomy_policies).values({**row, "revision": 3, "note": ""}),
            insert(t.autonomy_policies).values({**row, "revision": 3, "note": "x" * 1001}),
            insert(t.builder_proposals).values({**proposal, "kind": "secrets"}),
        ):
            with pytest.raises((errors.CheckViolation, errors.UniqueViolation)):
                conn.execute(bad)
        conn.execute(insert(t.builder_proposals).values({**proposal, "kind": "policy"}))
        conn.execute(insert(t.builder_proposals).values({**proposal, "kind": "agent", "target": {"agent": "r"}}))
        conn.execute(insert(t.notifications).values({**notice, "details": {"proposal": 1, "kind": "policy"}}))
        conn.execute(insert(t.notifications).values({**notice, "details": {"proposal": 2, "kind": "agent"}}))
    p, n = t.builder_proposals, t.notifications
    assert query(hub_db, select(t.autonomy_policies.c.revision).order_by(t.autonomy_policies.c.revision)) == [
        (1,),
        (2,),
    ]
    # Going back drops the policies, and the proposals of a change of one with their notices.
    move_to(hub_db, "0078", down=True)
    assert "autonomy_policies" not in tables(hub_db)
    assert query(hub_db, select(p.c.kind)) == [("agent",)]
    assert query(hub_db, select(n.c.details["kind"].as_string())) == [("agent",)]
    with live.connect(hub_db) as conn:
        conn.execute(delete(n))
        conn.execute(delete(p))
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0079") :]


def test_0080_takes_forge_secrets_with_a_url_prefix_and_an_end_and_going_back_deletes_them(hub_db):
    move_to(hub_db, "0079")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    sealed = {"sealed": b"s" * 17, "nonce": b"n" * 12, "key_id": "0123abcd"}
    prefix = "https://gitlab.example.org/northwind"
    forge = {"owner_id": ids["user"], "name": "gitlab-merge", "kind": "forge", "url_prefix": prefix, **sealed}
    forge["expires_at"] = func.now() + DAY
    git = {"owner_id": ids["user"], "name": "gitlab-push", "kind": "git", "url_prefix": prefix, "username": "oauth2"}
    with live.connect(hub_db) as conn:
        conn.execute(insert(t.secrets).values({**git, **sealed}))
        with pytest.raises(errors.CheckViolation):  # 0011 knows env and git alone
            conn.execute(insert(t.secrets).values(forge))
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0080")
    with live.connect(hub_db) as conn:
        forge_id = one(conn, insert(t.secrets).values(forge).returning(t.secrets.c.id))
        conn.execute(insert(t.secret_bindings).values(secret_id=forge_id, project_id=ids["project"]))
        for bad in (
            {**forge, "name": "no-end", "expires_at": None},  # a forge secret always ends
            {**forge, "name": "no-prefix", "url_prefix": None},
            {**forge, "name": "with-user", "username": "oauth2"},  # GitLab reads the token from a header
            {**forge, "name": "other", "kind": "ssh"},
            {**git, **sealed, "name": "git-no-prefix", "url_prefix": None},
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(insert(t.secrets).values(bad))
    s, b = t.secrets, t.secret_bindings
    assert query(hub_db, select(s.c.name, s.c.kind).order_by(s.c.name)) == [
        ("gitlab-merge", "forge"),
        ("gitlab-push", "git"),
    ]
    # Going back deletes the forge secrets and their bindings, and the checks of 0011 hold again.
    move_to(hub_db, "0079", down=True)
    assert query(hub_db, select(s.c.name, s.c.kind)) == [("gitlab-push", "git")]
    assert query(hub_db, select(func.count()).select_from(b)) == [(0,)]
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0080") :]


def test_0081_takes_the_merge_queue_and_judge_runs_on_no_plan_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0080")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    judge_run = {
        "project_id": ids["project"],
        "kind": "judge",
        "dispatched_by": ids["user"],
        "dispatched_via": "machine",
        "requested_runtime": "claude-code",
        "runtime": "claude-code",
        "mode": "headless",
        "approval": "auto",
        "timeout_s": 600,
        "repos": [{"repo": "lumen", "branch": "plan/report"}],
    }
    with live.connect(hub_db) as conn:
        with pytest.raises(errors.CheckViolation):  # 0067 and 0017: a judge run is on a plan and a schedule
            conn.execute(insert(t.runs).values(judge_run))
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0081")
    q, e = t.merge_queue, t.merge_events
    row = {
        "project_id": ids["project"],
        "repo": "lumen",
        "source": "plan",
        "plan_id": "report",
        "target_branch": "dev",
        "source_branch": "plan/report",
        "iid": 1,
    }
    with live.connect(hub_db) as conn:
        run_id = one(conn, insert(t.runs).values(judge_run).returning(t.runs.c.id))
        row_id = one(conn, insert(q).values(**row, judge_run_id=run_id).returning(q.c.id))
        line = {"project_id": ids["project"], "queue_id": row_id, "kind": "gate", "gate": 1, "passed": False}
        conn.execute(insert(e).values(line))
        for bad in (
            {**row, "iid": 2, "source": "fork"},
            {**row, "iid": 3, "state": "lost"},
            {**row, "iid": 4, "plan_id": None},  # a plan's merge request names its plan
            {**row, "iid": 0, "plan_id": "other"},
            {**row, "iid": 5, "plan_id": "other", "reason": "x" * 2001},
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(insert(q).values(bad))
        for bad in ({**line, "gate": None}, {**line, "gate": 8}, {**line, "kind": "pipeline"}, {**line, "kind": "x"}):
            with pytest.raises(errors.CheckViolation):
                conn.execute(insert(e).values(bad))
        with pytest.raises(errors.UniqueViolation):  # one merge request for each plan and repo
            conn.execute(insert(q).values({**row, "iid": 6}))
        conn.execute(insert(q).values({**row, "iid": 7, "source": "label", "plan_id": None}))
    assert query(hub_db, select(func.count()).select_from(e)) == [(1,)]
    # Going back drops the queue, deletes the judge runs on no plan and puts the checks of 0067 and 0017 back.
    move_to(hub_db, "0080", down=True)
    assert {"merge_queue", "merge_events"}.isdisjoint(tables(hub_db))
    assert query(hub_db, select(func.count()).select_from(t.runs).where(t.runs.c.kind == "judge")) == [(0,)]
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0081") :]


def test_0082_gives_one_merge_queue_turn_to_each_target_branch_and_going_back_drops_it(hub_db):
    move_to(hub_db, "0081")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    q = t.merge_queue
    row = {"project_id": ids["project"], "repo": "lumen", "source": "label", "target_branch": "dev"}
    with live.connect(hub_db) as conn:
        conn.execute(insert(q).values({**row, "iid": 1, "source_branch": "fix/a", "state": "testing"}))
        conn.execute(insert(q).values({**row, "iid": 2, "source_branch": "fix/b", "state": "ready"}))
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0082")
    rows = select(q.c.iid, q.c.state, q.c.rebases).order_by(q.c.iid)
    assert query(hub_db, rows) == [(1, "testing", 0), (2, "ready", 0)]
    second = q.c.iid == 2
    with live.connect(hub_db) as conn:
        for state in ("rebasing", "testing", "merging"):  # !1 has the turn of dev
            with pytest.raises(errors.UniqueViolation):
                conn.execute(update(q).values(state=state).where(second))
        with pytest.raises(errors.CheckViolation):
            conn.execute(update(q).values(rebases=-1).where(second))
        main = {**row, "iid": 3, "source_branch": "fix/c", "target_branch": "main", "state": "merging"}
        conn.execute(insert(q).values(main))  # main has a turn of its own
        conn.execute(update(q).values(state="merged").where(q.c.iid == 1))
        conn.execute(update(q).values(state="rebasing", rebases=1).where(second))  # !1 merged: the turn is !2's
    assert query(hub_db, rows) == [(1, "merged", 0), (2, "rebasing", 1), (3, "merging", 0)]
    # Going back drops the index and the column and keeps the rows.
    move_to(hub_db, "0081", down=True)
    assert query(hub_db, SNAPSHOT) == before
    kept = select(q.c.iid, q.c.state).order_by(q.c.iid)
    assert query(hub_db, kept) == [(1, "merged"), (2, "rebasing"), (3, "merging")]
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0082") :]


def test_0083_follows_a_merge_with_its_checks_reverts_and_samples_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0082")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    q, s = t.merge_queue, t.merge_samples
    row = {"project_id": ids["project"], "repo": "lumen", "source": "label", "target_branch": "dev"}
    with live.connect(hub_db) as conn:
        conn.execute(insert(q).values({**row, "iid": 1, "source_branch": "fix/a", "state": "merged"}))
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0083")
    rows = select(q.c.iid, q.c.state, q.c.uat_result, q.c.follow, q.c.reverts_id)
    assert query(hub_db, rows) == [(1, "merged", None, {}, None)]
    culprit = query(hub_db, select(q.c.id).where(q.c.iid == 1))[0][0]
    revert = {**row, "iid": 2, "source": "revert", "source_branch": "evo-revert/1", "reverts_id": culprit}
    with live.connect(hub_db) as conn:
        conn.execute(insert(q).values(revert))
        conn.execute(update(q).values(state="reverting", uat_result="failed").where(q.c.iid == 1))
        conn.execute(insert(s).values(project_id=ids["project"], queue_id=culprit))
        for bad in (
            update(q).values(uat_result="maybe").where(q.c.iid == 1),
            update(q).values(reverts_id=None).where(q.c.iid == 2),  # a revert names what it undoes
            update(q).values(reverts_id=culprit).where(q.c.iid == 1),  # and no other row does
            update(q).values(follow=[]).where(q.c.iid == 1),
            insert(s).values(project_id=ids["project"], queue_id=culprit),  # one sample a merge request
            update(s).values(result="faulty"),  # an answer has its time
            update(s).values(result="unsure", answered_at=func.now()),
        ):
            with pytest.raises((errors.CheckViolation, errors.UniqueViolation)):
                conn.execute(bad)
        conn.execute(update(s).values(result="faulty", answered_at=func.now(), note="It drops the totals."))
    assert query(hub_db, select(s.c.result, s.c.note)) == [("faulty", "It drops the totals.")]
    # Going back drops the samples, the reverts the hub opened and the columns, and keeps the other rows.
    move_to(hub_db, "0082", down=True)
    assert query(hub_db, SNAPSHOT) == before
    assert query(hub_db, select(q.c.iid, q.c.state)) == [(1, "reverting")]
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0083") :]


def test_0084_keeps_one_breaker_a_repo_and_the_notice_merge_paused_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0083")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    q, b, n = t.merge_queue, t.merge_breakers, t.notifications
    row = {"project_id": ids["project"], "repo": "lumen", "source": "label", "target_branch": "dev"}
    with live.connect(hub_db) as conn:
        conn.execute(insert(q).values({**row, "iid": 1, "source_branch": "fix/a", "state": "ready"}))
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0084")
    breaker = {"project_id": ids["project"], "repo": "lumen"}
    with live.connect(hub_db) as conn:
        conn.execute(insert(b).values(breaker))
        assert one(conn, select(b.c.state)) == "armed"
        tripped = {"state": "tripped", "cause": "uat_check", "reason": "the UAT check of !1 failed", "trips": 1}
        conn.execute(update(b).values(tripped_at=func.now(), **tripped))
        conn.execute(update(q).values(state="paused", reason="the breaker of lumen tripped"))
        notice = {"user_id": ids["user"], "kind": "notice", "notice_kind": "merge_paused", "title": "Stopped"}
        conn.execute(insert(n).values({**notice, "project_id": ids["project"], "details": {"repo": "lumen"}}))
        conn.execute(insert(n).values({**notice, "notice_kind": "release", "title": "Released"}))  # 0067's kind holds
        for bad in (
            insert(b).values(breaker),  # one breaker a repo
            update(b).values(state="open"),
            update(b).values(cause="weather"),
            update(b).values(reason=None),  # a tripped breaker says why
            update(b).values(tripped_at=None),  # and when
            update(b).values(trips=-1),
            insert(n).values({**notice, "notice_kind": "merge_stopped"}),
        ):
            with pytest.raises((errors.CheckViolation, errors.UniqueViolation)):
                conn.execute(bad)
        conn.execute(update(b).values(state="armed", resumed_by=ids["user"], resumed_at=func.now()))
    assert query(hub_db, select(b.c.state, b.c.cause, b.c.trips)) == [("armed", "uat_check", 1)]
    # Going back drops the breakers and the notices, and puts the merge request the breaker paused back in line.
    move_to(hub_db, "0083", down=True)
    assert query(hub_db, SNAPSHOT) == before
    assert query(hub_db, select(q.c.iid, q.c.state, q.c.reason)) == [(1, "ready", None)]
    assert query(hub_db, select(func.count()).select_from(n).where(n.c.notice_kind == "merge_paused")) == [(0,)]
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0084") :]


def test_0085_keeps_tasks_blockers_with_one_side_each_and_a_sprint_activated_at_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0084")
    k = hub_tables.tasks
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        sprint = {"project_id": ids["project"], "created_by": ids["user"], "state": "active"}
        conn.execute(insert(k.sprints).values(name="Sprint 1", **sprint))
        values = {"project_id": ids["project"], "title": "Cancel", "status": "todo", "label": {}}
        values |= {"created_by": ids["user"]}
        first = one(conn, insert(k.items).values(number=1, **values).returning(k.items.c.id))
        second = one(conn, insert(k.items).values(number=2, **values).returning(k.items.c.id))
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0085")
    b = k.blockers
    given = {"task_id": first, "created_by": ids["user"], "reason": "needs the API key"}
    with live.connect(hub_db) as conn:
        assert one(conn, select(k.sprints.c.activated_at)) is None  # active before: left empty
        conn.execute(insert(b).values(**given, waiting_user_id=ids["user"]))
        conn.execute(insert(b).values(**given, waiting_task_id=second))
        conn.execute(insert(b).values(**given, waiting_text="the bank"))  # a task has many open blockers
        assert one(conn, select(func.count()).select_from(b).where(b.c.cleared_at.is_(None))) == 3
        for bad in (
            insert(b).values(**given),  # waits on nothing
            insert(b).values(**given, waiting_user_id=ids["user"], waiting_text="the bank"),  # on two things
            insert(b).values(**given, waiting_task_id=first),  # on itself
            insert(b).values(**{**given, "reason": ""}, waiting_text="x"),
            update(b).values(cleared_at=func.now()),  # cleared by nobody
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(bad)
        conn.execute(update(b).values(cleared_at=func.now(), cleared_by=ids["user"]))
    # Going back drops the table and the column.
    move_to(hub_db, "0084", down=True)
    assert "tasks_blockers" not in tables(hub_db)
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0085") :]


def test_0086_keeps_one_send_a_day_and_the_pm_notices_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0085")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0086")
    s, n = plugin_tables(hub_db, "pm_sends").pm_sends, t.notifications
    sent = {"project_id": ids["project"], "day": date(2026, 10, 21), "kind": "digest"}
    notice = {"user_id": ids["user"], "kind": "notice", "title": "Digest", "project_id": ids["project"]}
    with live.connect(hub_db) as conn:
        conn.execute(insert(s).values(sent))
        assert one(conn, select(s.c.key)) == ""
        conn.execute(insert(s).values({**sent, "kind": "escalation", "key": "blocker:1"}))
        conn.execute(insert(s).values({**sent, "kind": "escalation", "key": "decision:1"}))
        conn.execute(insert(s).values({**sent, "day": date(2026, 10, 22)}))
        for kind in ("pm_daily", "pm_escalation", "merge_paused", "release"):
            conn.execute(insert(n).values({**notice, "notice_kind": kind}))
        for bad in (insert(s).values(sent), insert(s).values({**sent, "kind": "weekly", "key": "x"})):
            with pytest.raises((errors.CheckViolation, errors.UniqueViolation)):
                conn.execute(bad)
        with pytest.raises(errors.CheckViolation):
            conn.execute(insert(n).values({**notice, "notice_kind": "pm_weekly"}))
    # Going back drops the table and the two notices, and keeps the other kinds.
    move_to(hub_db, "0085", down=True)
    assert "pm_sends" not in tables(hub_db)
    assert query(hub_db, select(n.c.notice_kind).order_by(n.c.id)) == [("merge_paused",), ("release",)]
    with live.connect(hub_db) as conn:
        conn.execute(delete(n).where(n.c.notice_kind.in_(("merge_paused", "release"))))
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0086") :]


def test_0088_gives_every_task_an_empty_outcome_and_going_back_drops_the_column(hub_db):
    move_to(hub_db, "0087")
    k = hub_tables.tasks
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        values = {"project_id": ids["project"], "title": "Cancel", "status": "todo", "label": {}}
        conn.execute(insert(k.items).values(number=1, created_by=ids["user"], description_md="## Outcome\nx", **values))
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0088")
    assert "outcome_md" in columns(hub_db, "tasks_items")
    assert query(hub_db, select(k.items.c.outcome_md)) == [("",)]  # the description is not read
    with live.connect(hub_db) as conn:
        conn.execute(update(k.items).values(outcome_md="Done."))
    move_to(hub_db, "0087", down=True)
    assert "outcome_md" not in columns(hub_db, "tasks_items")
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0088") :]


def test_0089_keeps_one_output_a_file_of_a_run_its_links_and_the_switch_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0088")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        run_id = one(conn, _local_run(ids, "s-outputs"))
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0089")
    o, k, d, p, e = t.run_outputs, t.run_output_links, t.decisions, t.projects, t.chat_events
    output = {"run_id": run_id, "project_id": ids["project"], "source": "sent", "name": "shot.png"}
    output |= {"media_type": "image/png", "size": 10, "sha256": "b" * 64}
    asked = {"run_id": run_id, "project_id": ids["project"], "category": "question", "question": "Ship it?"}
    with live.connect(hub_db) as conn:
        assert one(conn, select(p.c.output_tool_images)) is True  # on unless an admin turns it off
        output_id = one(conn, insert(o).values(output).returning(o.c.id))
        options = [{"key": "yes", "label": "Yes"}, {"key": "no", "label": "No"}]
        decision_id = one(conn, insert(d).values({**asked, "options": options}).returning(d.c.id))
        conn.execute(insert(k).values(output_id=output_id, decision_id=decision_id))
        for bad in (
            insert(o).values(output),  # one output a SHA-256 of a run
            insert(o).values({**output, "sha256": "c" * 64, "source": "screen"}),
            insert(o).values({**output, "sha256": "c" * 64, "name": ""}),
            insert(o).values({**output, "sha256": "C" * 64}),
            insert(o).values({**output, "sha256": "c" * 64, "size": 200 * 1024 * 1024 + 1}),
            insert(o).values({**output, "sha256": "c" * 64, "caption": ""}),
            insert(k).values(output_id=output_id),  # a link names a decision or a message
            insert(k).values(output_id=output_id, decision_id=decision_id),
        ):
            with pytest.raises((errors.CheckViolation, errors.UniqueViolation)):
                conn.execute(bad)
        conn.execute(insert(e).values(chat_id=_chat(conn, ids), seq=1, kind="message_changed", body={}))
    # Going back drops the outputs, their links, the switch and the events of 0089's kind.
    move_to(hub_db, "0088", down=True)
    assert query(hub_db, select(func.count()).select_from(e).where(e.c.kind == "message_changed")) == [(0,)]
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0089") :]


def test_0090_takes_the_notice_run_outputs_and_the_notices_members_turned_off_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0089")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0090")
    n, m = t.notifications, t.notification_mutes
    notice = {"user_id": ids["user"], "kind": "notice", "project_id": ids["project"], "title": "Run #1 sent a.png"}
    with live.connect(hub_db) as conn:
        conn.execute(insert(n).values({**notice, "notice_kind": "run_outputs", "details": {"outputs": [1]}}))
        conn.execute(insert(n).values({**notice, "notice_kind": "merge_paused"}))  # 0084's kind holds
        conn.execute(insert(n).values({**notice, "notice_kind": "pm_daily"}))  # and 0086's
        conn.execute(insert(m).values(user_id=ids["user"], notice_kind="run_outputs"))
        for bad in (
            insert(m).values(user_id=ids["user"], notice_kind="run_outputs"),  # one row a member and kind
            insert(m).values(user_id=ids["user"], notice_kind="plan_finished"),  # not a kind a member turns off
            insert(n).values({**notice, "notice_kind": "run_files"}),
        ):
            with pytest.raises((errors.CheckViolation, errors.UniqueViolation)):
                conn.execute(bad)
    # Going back drops the notices of 0090's kind and the table.
    move_to(hub_db, "0089", down=True)
    assert query(hub_db, select(func.count()).select_from(n).where(n.c.notice_kind == "run_outputs")) == [(0,)]
    assert query(hub_db, select(func.count()).select_from(n).where(n.c.notice_kind == "merge_paused")) == [(1,)]
    assert query(hub_db, select(func.count()).select_from(n).where(n.c.notice_kind == "pm_daily")) == [(1,)]
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0090") :]


def _chat(conn, ids) -> int:
    """A group chat the seeded user created; its id."""
    chats = hub_tables.chats
    return one(conn, insert(chats).values(title="Release talk", created_by=ids["user"]).returning(chats.c.id))


def test_0091_keeps_the_revisions_of_goals_and_proposals_of_one_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0090")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    proposal = {"project_id": ids["project"], "change": {"set": {}, "unset": []}, "summary": "s", "base_version": 0}
    proposal["proposed_by"] = ids["user"]
    with live.connect(hub_db) as conn, pytest.raises(errors.CheckViolation):  # 0079 knows no goal proposal
        conn.execute(insert(t.builder_proposals).values({**proposal, "kind": "goal"}))
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0091")
    assert columns(hub_db, "goals") == {"id", "project_id", "number", "alias", "revision", "created_at"}
    assert columns(hub_db, "goal_revisions") == {
        "goal_id",
        "revision",
        "statement",
        "metric",
        "deadline",
        "canvas_id",
        "rank",
        "owner_id",
        "status",
        "close_reason",
        "note",
        "created_by",
        "created_at",
    }
    g, r, p, n = t.goals, t.goal_revisions, t.builder_proposals, t.notifications
    goal = {"project_id": ids["project"], "number": 1, "alias": "night-shift", "revision": 1}
    notice = {"user_id": ids["user"], "kind": "notice", "notice_kind": "builder_proposal", "title": "Goal"}
    with live.connect(hub_db) as conn:
        goal_id = one(conn, insert(g).values(goal).returning(g.c.id))
        row = {"goal_id": goal_id, "revision": 1, "statement": "Run the approved plans at night", "rank": 1}
        row |= {"owner_id": ids["user"], "status": "active", "created_by": ids["user"]}
        conn.execute(insert(r).values(row))
        metric = {"key": "night.plans_done", "aggregate": "sum", "comparator": ">=", "target": 3}
        closed = {"status": "closed", "close_reason": "achieved", "metric": metric, "note": "three a night"}
        conn.execute(insert(r).values({**row, "revision": 2, **closed}))
        for bad in (
            insert(g).values({**goal, "alias": None}),  # one number a project
            insert(g).values({**goal, "number": 2}),  # one alias a project
            insert(g).values({**goal, "number": 0, "alias": None}),
            insert(g).values({**goal, "number": 3, "alias": "Night Shift"}),
            insert(r).values(row),  # one row per revision of a goal
            insert(r).values({**row, "revision": 0}),
            insert(r).values({**row, "revision": 3, "statement": "  "}),
            insert(r).values({**row, "revision": 3, "statement": "x" * 2001}),
            insert(r).values({**row, "revision": 3, "metric": ["night.plans_done"]}),
            insert(r).values({**row, "revision": 3, "rank": 0}),
            insert(r).values({**row, "revision": 3, "status": "done"}),
            insert(r).values({**row, "revision": 3, "status": "closed"}),  # a closed goal names its reason
            insert(r).values({**row, "revision": 3, "close_reason": "achieved"}),  # and only a closed one does
            insert(r).values({**row, "revision": 3, "status": "closed", "close_reason": "weather"}),
            insert(r).values({**row, "revision": 3, "note": ""}),
            insert(p).values({**proposal, "kind": "secrets"}),
        ):
            with pytest.raises((errors.CheckViolation, errors.UniqueViolation)):
                conn.execute(bad)
        conn.execute(insert(p).values({**proposal, "kind": "goal", "target": {"goal": "G-2"}}))
        conn.execute(insert(p).values({**proposal, "kind": "policy"}))
        conn.execute(insert(n).values({**notice, "details": {"proposal": 1, "kind": "goal"}}))
        conn.execute(insert(n).values({**notice, "details": {"proposal": 2, "kind": "policy"}}))
    assert query(hub_db, select(r.c.revision, r.c.status).order_by(r.c.revision)) == [(1, "active"), (2, "closed")]
    # Going back drops the goals, and the proposals of a goal with their notices.
    move_to(hub_db, "0090", down=True)
    assert not {"goals", "goal_revisions"} & tables(hub_db)
    assert query(hub_db, select(p.c.kind)) == [("policy",)]
    assert query(hub_db, select(n.c.details["kind"].as_string())) == [("policy",)]
    with live.connect(hub_db) as conn:
        conn.execute(delete(n))
        conn.execute(delete(p))
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0091") :]


def test_0092_takes_deliveries_and_grants_of_srs_and_tasks_events_and_going_back_deletes_them(hub_db):
    move_to(hub_db, "0091")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        plugin_id = one(conn, insert(t.plugins).values(name="loop", publisher="acme").returning(t.plugins.c.id))
        conn.execute(_plugin_version(plugin_id, ids["user"]))
        granted = {"project_id": ids["project"], "plugin_id": plugin_id, "enabled_by": ids["user"]}
        granted |= {"granted_by": ids["user"], "permissions": ["runs:read"]}
        conn.execute(insert(t.project_plugins).values(granted))
        with pytest.raises(errors.CheckViolation):  # 0091 takes neither permission
            conn.execute(update(t.project_plugins).values(permissions=["runs:read", "tasks:read"]))
    delivery = {"plugin_id": plugin_id, "project_id": ids["project"], "contribution": "loop", "payload": {"v": 1}}
    with live.connect(hub_db) as conn, pytest.raises(errors.CheckViolation):  # nor either kind
        conn.execute(insert(t.plugin_deliveries).values({**delivery, "kind": "tasks.status"}))
    move_to(hub_db, "0092")
    with live.connect(hub_db) as conn:
        conn.execute(update(t.project_plugins).values(permissions=["runs:read", "srs:read", "tasks:read"]))
        for kind in ("srs.version.approved", "tasks.status", "run.state"):
            conn.execute(insert(t.plugin_deliveries).values({**delivery, "kind": kind}))
            conn.execute(insert(t.hub_events).values(project_id=ids["project"], kind=kind, payload={"v": 1}))
        for bad in (
            update(t.project_plugins).values(permissions=["tasks:write"]),
            insert(t.plugin_deliveries).values({**delivery, "kind": "tasks.deleted"}),
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(bad)
    # Going back deletes the deliveries and the hub's events of the two kinds and takes the two permissions back.
    move_to(hub_db, "0091", down=True)
    assert query(hub_db, select(t.plugin_deliveries.c.kind)) == [("run.state",)]
    assert query(hub_db, select(t.hub_events.c.kind)) == [("run.state",)]
    assert query(hub_db, select(t.project_plugins.c.permissions)) == [(["runs:read"],)]
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0092") :]


def test_0093_keeps_the_loop_of_a_project_its_work_items_and_their_ledger_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0092")
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0093")
    assert columns(hub_db, "loop_states") == {
        "project_id",
        "enabled",
        "enabled_at",
        "cursor",
        "breaker",
        "breaker_reason",
        "tripped_at",
        "resumed_by",
        "resumed_at",
        "ticked_at",
        "updated_at",
    }
    s, i, e = hub_tables.loop_states, hub_tables.loop_items, hub_tables.loop_item_events
    item = {"project_id": ids["project"], "kind": "brainstorm", "cycle": "business", "key": "brainstorm:G-1:r1"}
    item |= {"engine": "rules/1", "owner": "@strategist"}
    with live.connect(hub_db) as conn:
        conn.execute(insert(s).values(project_id=ids["project"], enabled=True, enabled_at=func.now(), cursor=7))
        item_id = one(conn, insert(i).values(item).returning(i.c.id))
        conn.execute(insert(e).values(item_id=item_id, kind="created", state="queued"))
        done = {**item, "key": "deliver:p", "kind": "deliver", "cycle": "development", "state": "done"}
        conn.execute(insert(i).values({**done, "finished_at": func.now()}))
        for bad in (
            insert(s).values(project_id=ids["project"]),  # one row a project
            insert(i).values(item),  # one work item a key
            insert(i).values({**item, "key": "x", "kind": "plan"}),
            insert(i).values({**item, "key": "x", "cycle": "development"}),  # a brainstorm is of the business cycle
            insert(i).values({**item, "key": ""}),
            insert(i).values({**item, "key": "k" * 501}),
            insert(i).values({**item, "key": "x", "state": "waiting"}),
            insert(i).values({**item, "key": "x", "reason": "budget"}),  # no budget in loop/1
            insert(i).values({**item, "key": "x", "detail": []}),
            insert(i).values({**item, "key": "x", "attempts": -1}),
            insert(i).values({**item, "key": "x", "state": "done"}),  # a finished one says when
            insert(i).values({**item, "key": "x", "finished_at": func.now()}),  # and only a finished one
            insert(e).values(item_id=item_id, kind="charged", state="queued"),
        ):
            with pytest.raises((errors.CheckViolation, errors.UniqueViolation)):
                conn.execute(bad)
    assert query(hub_db, select(i.c.key).order_by(i.c.id)) == [("brainstorm:G-1:r1",), ("deliver:p",)]
    # Going back drops the three tables.
    move_to(hub_db, "0092", down=True)
    assert not pg.LOOP_TABLES & tables(hub_db)
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0093") :]


def test_downgrade_to_base_removes_everything_and_upgrade_restores_it(hub_db):
    move_to(hub_db, "head")
    pg.migrate_in_steps(hub_db, "base", down=True)
    assert tables(hub_db) == {"alembic_version"}
    functions = select(func.count()).select_from(PG_PROC).where(PG_PROC.c.proname == "hub_reject_update")
    assert query(hub_db, functions) == [(0,)]
    assert pg.migrate_in_steps(hub_db) == ALL
    assert tables(hub_db) == TABLES | {"alembic_version"}


@pytest.fixture
def schema(hub_db):
    """A migrated database and a connection to it as its owner, in autocommit."""
    move_to(hub_db, "head")
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


def test_0100_keeps_the_questions_of_core_plugins_and_question_runs_and_going_back_deletes_them(hub_db):
    move_to(hub_db, "0099")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        plugin_id = one(conn, insert(t.plugins).values(name="northwind", publisher="acme").returning(t.plugins.c.id))
        conn.execute(_plugin_version(plugin_id, ids["user"]))
        axes = ("credential", "tool", "skill", "worker", "project", "model", "runtime", "environment")
        profile_id = one(conn, _profile_row(ids, "helpdesk", {axis: {"mode": "all"} for axis in axes}))
        agent_types = t.project_agent_types
        row = {"project_id": ids["project"], "plugin_id": plugin_id, "name": "helpdesk", "profile_id": profile_id}
        row |= {"run_as": ids["user"], "daily_usd": 5.0, "enabled_by": ids["user"]}
        type_id = one(conn, insert(agent_types).values(row).returning(agent_types.c.id))
        task = one(conn, _task_run(ids, type_id))
        delivery = {"plugin_id": plugin_id, "project_id": ids["project"], "kind": "agent.answer", "contribution": "a"}
        delivery["payload"] = {"question_id": 1}
        with pytest.raises(errors.CheckViolation):  # no run of kind question before 0100
            conn.execute(_task_run(ids, type_id, kind="question", branch=None, pulls=null(), repos=null()))
        with pytest.raises(errors.CheckViolation):  # and no event agent.answer
            conn.execute(insert(t.plugin_deliveries).values(delivery))
    move_to(hub_db, "0100")
    q = t.agent_questions
    asked = {"plugin_id": plugin_id, "project_id": ids["project"], "agent_type_id": type_id}
    asked |= {"asker": "Ana", "cleared": {"level": "internal", "location": None, "below": False}}
    with live.connect(hub_db) as conn:
        question = _task_run(ids, type_id, kind="question", branch=None, pulls=null(), repos=[{"repo": "app"}])
        run_id = one(conn, question)
        other = one(conn, _task_run(ids, type_id, kind="question", branch=None, pulls=null(), repos=null()))
        first = one(conn, insert(q).values({**asked, "run_id": run_id, "ref": "m-1"}).returning(q.c.id))
        conn.execute(insert(t.plugin_deliveries).values(delivery))
        for bad in (
            _task_run(ids, None, kind="question", branch=None, pulls=null()),  # a question run names its agent type
            _task_run(ids, type_id, kind="question", request=None, branch=None, pulls=null()),  # and its question
            insert(q).values({**asked, "run_id": other, "ref": "two\nlines"}),
            insert(q).values({**asked, "run_id": other, "ref": "m-2", "asker": "a" * 201}),
            insert(q).values({**asked, "run_id": other, "ref": "m-2", "cleared": ["internal"]}),
            insert(q).values({**asked, "run_id": other, "ref": "m-2", "outcome": "lost", "answered_at": func.now()}),
            insert(q).values({**asked, "run_id": other, "ref": "m-2", "outcome": "done"}),  # answered_at with it
            insert(q).values({**asked, "run_id": other, "ref": "m-2", "answer": "yes"}),  # an answer only when done
            insert(q).values(
                {
                    **asked,
                    "run_id": other,
                    "ref": "m-2",
                    "outcome": "done",
                    "answered_at": func.now(),
                    "answer": "x" * 8001,
                }
            ),
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(bad)
        with pytest.raises(errors.UniqueViolation):  # a ref once per plugin and project
            conn.execute(insert(q).values({**asked, "run_id": other, "ref": "m-1"}))
        with pytest.raises(errors.UniqueViolation):  # a run answers one question
            conn.execute(insert(q).values({**asked, "run_id": run_id, "ref": "m-3"}))
        done = {"outcome": "done", "answer": "Settings, then Security.", "answered_at": func.now(), "label": {}}
        conn.execute(update(q).where(q.c.id == first).values(done))
    assert query(hub_db, select(q.c.outcome, q.c.answer)) == [("done", "Settings, then Security.")]

    # Going back drops the questions and deletes the question runs and the deliveries of agent.answer; the rest stays.
    move_to(hub_db, "0099", down=True)
    assert "agent_questions" not in tables(hub_db)
    assert query(hub_db, select(t.runs.c.id, t.runs.c.kind)) == [(task, "task")]
    assert query(hub_db, select(func.count()).select_from(t.plugin_deliveries)) == [(0,)]
    with live.connect(hub_db) as conn:
        with pytest.raises(errors.CheckViolation):  # the checks of schema 0099 are back
            conn.execute(insert(t.plugin_deliveries).values(delivery))
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0100") :]


def test_0104_lets_an_agent_profile_own_env_secrets_and_going_back_is_refused_while_one_does(hub_db):
    move_to(hub_db, "0103")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    s = t.secrets
    mine = {"owner_id": ids["user"], "name": "claude-a", "kind": "env", "env_var": "CLAUDE_CODE_OAUTH_TOKEN"}
    mine |= {"sealed": b"s" * 20, "nonce": b"n" * 12, "key_id": "0123abcd"}
    with live.connect(hub_db) as conn:
        conn.execute(insert(s).values(mine))
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0104")
    with live.connect(hub_db) as conn:
        profile = {"project_id": ids["project"], "handle": "auditor", "name": "Auditor", "color": "#475569"}
        profile |= {"permissions": {}, "created_by": ids["user"]}
        pid = conn.execute(insert(t.agent_profiles).values(profile).returning(t.agent_profiles.c.id)).scalar_one()
        agent = {"profile_id": pid, "name": "claude-a", "kind": "env", "env_var": "CLAUDE_CODE_OAUTH_TOKEN"}
        agent |= {"sealed": b"s" * 20, "nonce": b"n" * 12, "key_id": "0123abcd"}
        conn.execute(insert(s).values(agent))  # the member's secret of that name is another row
        assert one(conn, select(func.count()).select_from(s).where(s.c.name == "claude-a")) == 2
        for bad in (
            insert(s).values({**agent, "owner_id": ids["user"]}),  # exactly one owner
            insert(s).values({"name": "orphan", "kind": "env"}),
            insert(s).values({**agent, "kind": "git", "name": "g"}),  # an agent's secret is env
            insert(s).values(agent),  # unique by (profile, name) among the live ones
            insert(s).values({**agent, "profile_id": pid + 1000, "name": "x"}),  # a profile that exists
        ):
            with pytest.raises((errors.CheckViolation, errors.UniqueViolation, errors.ForeignKeyViolation)):
                conn.execute(bad)
        gone = {"deleted_at": func.now(), "sealed": None, "nonce": None, "key_id": None}
        conn.execute(update(s).values(gone).where(s.c.profile_id == pid))
        conn.execute(insert(s).values(agent))  # a deleted one frees the name
    with pytest.raises(RuntimeError, match=r"2 secret row\(s\) belong to agent profiles"):
        move_to(hub_db, "0103", down=True)
    with live.connect(hub_db) as conn:
        conn.execute(delete(s).where(s.c.profile_id.is_not(None)))
    move_to(hub_db, "0103", down=True)
    assert query(hub_db, SNAPSHOT) == before
    assert "profile_id" not in columns(hub_db, "secrets")
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0104") :]


def test_0109_keeps_the_attachments_of_messages_takes_empty_bodies_and_going_back_restores_them(hub_db):
    move_to(hub_db, "0108")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        run_id = one(conn, _author_run(ids))
        chat_id = _chat(conn, ids)
        message = {"chat_id": chat_id, "seq": 1, "author_user_id": ids["user"], "body": "", "label": []}
        with pytest.raises(errors.CheckViolation):  # no message without text before 0109
            conn.execute(insert(t.chat_messages).values(message))
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0109")
    a, k = t.attachments, t.run_inbox_attachments
    with live.connect(hub_db) as conn:
        inbox_id = one(
            conn, insert(t.run_inbox).values(run_id=run_id, sent_by=ids["user"], body="").returning(t.run_inbox.c.id)
        )
        message_id = one(conn, insert(t.chat_messages).values(message).returning(t.chat_messages.c.id))
        upload = {"uploaded_by": ids["user"], "name": "notes.txt", "media_type": "text/plain", "size": 5}
        upload |= {"sha256": "a" * 64}
        to_run = one(conn, insert(a).values({**upload, "run_id": run_id, "inbox_id": inbox_id}).returning(a.c.id))
        conn.execute(insert(a).values({**upload, "chat_id": chat_id, "chat_message_id": message_id}))
        conn.execute(insert(k).values(inbox_id=inbox_id, attachment_id=to_run))
        for bad in (
            insert(a).values(**upload),  # a run or a chat
            insert(a).values({**upload, "run_id": run_id, "chat_id": chat_id}),  # not both
            insert(a).values({**upload, "chat_id": chat_id, "inbox_id": inbox_id}),  # an inbox row holds a run's file
            insert(a).values({**upload, "run_id": run_id, "chat_message_id": message_id}),  # a message a chat's file
            insert(a).values({**upload, "name": "", "run_id": run_id}),
            insert(a).values({**upload, "sha256": "A" * 64, "run_id": run_id}),
            insert(a).values({**upload, "size": 50 * 1024 * 1024 + 1, "run_id": run_id}),
            insert(k).values(inbox_id=inbox_id, attachment_id=to_run),  # tied once
        ):
            with pytest.raises((errors.CheckViolation, errors.UniqueViolation)):
                conn.execute(bad)
    # Going back gives each empty body a placeholder and drops the files.
    move_to(hub_db, "0108", down=True)
    assert query(hub_db, select(t.run_inbox.c.body)) == [("(files)",)]
    assert query(hub_db, select(t.chat_messages.c.body)) == [("(files)",)]
    assert {"attachments", "run_inbox_attachments"}.isdisjoint(tables(hub_db))
    with live.connect(hub_db) as conn:
        conn.execute(delete(t.run_inbox))
        conn.execute(delete(t.chat_messages))
    assert query(hub_db, SNAPSHOT) == before
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0109") :]


def test_0117_keeps_browser_identities_and_going_back_records_their_blobs_for_deletion(hub_db):
    move_to(hub_db, "0116")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        worker = one(conn, _worker_row(ids, ids["token"], "box", 2))
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0117")
    b = t.browser_identities
    mine = {"project_id": ids["project"], "owner_kind": "member", "owner_id": ids["user"], "name": "northwind"}
    mine |= {"domains": ["northwind.example"], "worker_id": worker, "created_by": ids["user"]}
    saved = {"version": 2, "sha256": "a" * 64, "size": 10, "sealed_key": b"k" * 48, "key_nonce": b"n" * 12}
    saved |= {"key_id": "0123abcd", "chromium_version": "141.0.7390.54"}
    saved |= {"previous_sha256": "b" * 64, "previous_size": 9, "previous_sealed_key": b"k" * 48}
    saved |= {"previous_key_nonce": b"n" * 12, "previous_key_id": "0123abcd"}
    with live.connect(hub_db) as conn:
        first = one(conn, insert(b).values(mine).returning(b.c.id))
        assert conn.execute(select(b.c.status, b.c.version, b.c.sha256).where(b.c.id == first)).one() == (
            "active",
            0,
            None,
        )
        conn.execute(update(b).values(saved).where(b.c.id == first))
        project = {**mine, "owner_kind": "project", "owner_id": None}
        conn.execute(insert(b).values(project))  # the project's identity of that name is another one
        for bad in (
            insert(b).values(mine),  # unique by (project, member, name) among the live ones
            insert(b).values(project),  # unique by (project, name) among the project's live ones
            insert(b).values({**mine, "owner_id": None}),  # a member's has its member
            insert(b).values({**project, "owner_id": ids["user"], "name": "p2"}),  # the project's has none
            insert(b).values({**mine, "name": "Northwind"}),  # the name of a secret
            insert(b).values({**mine, "name": "n2", "domains": []}),  # at least one domain
            insert(b).values({**mine, "name": "n3", "status": "broken"}),
            insert(b).values({**mine, "name": "n4", "version": 1}),  # a saved version has its blob
            insert(b).values({**mine, "name": "n5", **saved, "key_id": None}),  # a version is whole
            insert(b).values({**mine, "name": "n6", **saved, "sha256": "A" * 64}),  # a hash in hex
            insert(b).values({**mine, "name": "n7", **saved, "sha256": None, "size": None, "sealed_key": None}),
            insert(b).values({**mine, "name": "n8", "worker_id": worker + 1000}),  # a worker that exists
        ):
            with pytest.raises((errors.CheckViolation, errors.UniqueViolation, errors.ForeignKeyViolation)):
                conn.execute(bad)
        with pytest.raises(errors.CheckViolation):  # a deleted one keeps no key
            conn.execute(update(b).values(deleted_at=func.now()).where(b.c.id == first))
        conn.execute(update(b).values(deleted_at=func.now()).where(b.c.owner_kind == "project"))
        conn.execute(insert(b).values(project))  # a deleted one frees the name
    move_to(hub_db, "0116", down=True)
    assert "browser_identities" not in tables(hub_db)
    deletions = t.blob_deletions
    recorded = select(deletions.c.sha256, deletions.c.size, deletions.c.kind).order_by(deletions.c.sha256)
    assert query(hub_db, recorded) == [("a" * 64, 10, "browser-profile"), ("b" * 64, 9, "browser-profile")]
    with live.connect(hub_db) as conn:
        conn.execute(delete(deletions))
    assert query(hub_db, SNAPSHOT) == before
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0117") :]


def test_0121_lets_a_run_ask_a_login_decision_and_going_back_deletes_those_with_their_inbox_rows(hub_db):
    move_to(hub_db, "0120")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        run_id = one(conn, _author_run(ids))
        with pytest.raises(errors.CheckViolation):  # no login category before 0121
            conn.execute(_decision_row(ids, run_id, "wait-helper").values(category="login"))
    move_to(hub_db, "0121")
    with live.connect(hub_db) as conn:
        login = one(conn, _decision_row(ids, run_id, "wait-helper").values(category="login"))
        scope = one(conn, _decision_row(ids, run_id, "wait-helper"))
        for decision_id in (login, scope):
            conn.execute(
                insert(t.run_inbox).values(run_id=run_id, sent_by=ids["user"], body="A.", decision_id=decision_id)
            )

    move_to(hub_db, "0120", down=True)
    assert query(hub_db, select(t.decisions.c.id, t.decisions.c.category)) == [(scope, "scope")]
    assert query(hub_db, select(t.run_inbox.c.decision_id)) == [(scope,)]
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0121") :]


def test_0124_crm_keeps_one_contact_a_login_and_going_back_drops_the_tables(hub_db):
    move_to(hub_db, "0123")
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0124")
    t = plugin_tables(hub_db, "crm_contacts", "crm_touchpoints", "crm_stage_history")
    contact = {"project_id": ids["project"], "login": "octocat", "profile_url": "https://github.com/octocat"}
    contact |= {"source": "issue"}
    with live.connect(hub_db) as conn:
        contact_id = one(conn, insert(t.crm_contacts).values(contact).returning(t.crm_contacts.c.id))
        assert one(conn, select(t.crm_contacts.c.stage)) == "lead"
        touch = {"contact_id": contact_id, "kind": "issue", "ref": "issue#1", "happened_at": func.now()}
        conn.execute(insert(t.crm_touchpoints).values(touch))
        conn.execute(insert(t.crm_touchpoints).values({**touch, "kind": "note", "ref": "n", "note": "asked about CI"}))
        conn.execute(insert(t.crm_stage_history).values(contact_id=contact_id, stage="lead", reached_at=func.now()))
        for bad in (
            insert(t.crm_contacts).values(contact),  # one contact a login
            insert(t.crm_contacts).values({**contact, "login": "Octo"}),  # lower case alone
            insert(t.crm_contacts).values({**contact, "login": "other", "source": "email"}),
            insert(t.crm_contacts).values({**contact, "login": "other", "stage": "customer"}),
            insert(t.crm_touchpoints).values(touch),  # once a kind and reference
            insert(t.crm_touchpoints).values({**touch, "ref": "issue#2", "note": "a note on an issue"}),
            insert(t.crm_touchpoints).values({**touch, "kind": "note", "ref": "n2"}),  # a note has text
            insert(t.crm_stage_history).values(contact_id=contact_id, stage="lead", reached_at=func.now()),
        ):
            with pytest.raises((errors.CheckViolation, errors.UniqueViolation)):
                conn.execute(bad)
    with live.connect(hub_db) as conn:  # the contact takes its touchpoints and history with it
        conn.execute(delete(t.crm_contacts).where(t.crm_contacts.c.id == contact_id))
        assert one(conn, select(func.count()).select_from(t.crm_touchpoints)) == 0
        assert one(conn, select(func.count()).select_from(t.crm_stage_history)) == 0
    move_to(hub_db, "0123", down=True)
    assert not {name for name in tables(hub_db) if name.startswith("crm_")}
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0124") :]


def test_0125_marketing_keeps_the_registry_rules_in_the_database_and_going_back_drops_the_tables(hub_db):
    move_to(hub_db, "0124")
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0125")
    t = plugin_tables(hub_db, "marketing_channels", "marketing_posts", "marketing_metrics", "marketing_denylist")
    account = {"project_id": ids["project"], "channel": "x", "account": "evo_agents", "account_kind": "project"}
    personal = {**account, "channel": "tiktok", "account": "me", "account_kind": "personal", "owner_id": ids["user"]}
    with live.connect(hub_db) as conn:
        channel_id = one(conn, insert(t.marketing_channels).values(account).returning(t.marketing_channels.c.id))
        assert one(conn, select(t.marketing_channels.c.mode)) == "draft_only"  # the default is the safe one
        conn.execute(insert(t.marketing_channels).values({**personal, "mode": "autonomous"}))  # tiktok may
        post = {"project_id": ids["project"], "channel_id": channel_id, "channel": "x", "account": "a", "body": "hi"}
        post_id = one(conn, insert(t.marketing_posts).values(post).returning(t.marketing_posts.c.id))
        conn.execute(insert(t.marketing_metrics).values(post_id=post_id, span="24h", metric="likes", value=3))
        for bad in (
            insert(t.marketing_channels).values(account),  # one row for each account
            insert(t.marketing_channels).values({**account, "account": "b", "channel": "mastodon"}),
            insert(t.marketing_channels).values({**account, "account": "b", "mode": "robot"}),
            insert(t.marketing_channels).values({**personal, "account": "b", "owner_id": None}),  # an owner
            insert(t.marketing_channels).values({**personal, "account": "b", "channel": "x", "mode": "autonomous"}),
            insert(t.marketing_channels).values({**account, "account": "b", "channel": "facebook_profile"}),
            insert(t.marketing_channels).values(
                {**account, "account": "b", "channel": "hackernews", "mode": "autonomous"}
            ),
            insert(t.marketing_posts).values({**post, "state": "sent"}),
            insert(t.marketing_posts).values({**post, "body": ""}),
            insert(t.marketing_metrics).values(post_id=post_id, span="24h", metric="likes", value=4),
            insert(t.marketing_denylist).values(project_id=ids["project"], kind="name", value="x"),
        ):
            with pytest.raises((errors.CheckViolation, errors.UniqueViolation)):
                conn.execute(bad)
    with live.connect(hub_db) as conn:  # a channel takes its posts and their metrics with it
        conn.execute(delete(t.marketing_channels).where(t.marketing_channels.c.id == channel_id))
        assert one(conn, select(func.count()).select_from(t.marketing_posts)) == 0
        assert one(conn, select(func.count()).select_from(t.marketing_metrics)) == 0
    move_to(hub_db, "0124", down=True)
    assert not {name for name in tables(hub_db) if name.startswith("marketing_")}
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0125") :]


def test_0126_marketing_browser_adds_the_queue_states_and_the_session_and_going_back_makes_drafts(hub_db):
    move_to(hub_db, "0125")
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    t = plugin_tables(hub_db, "marketing_channels", "marketing_posts")
    account = {"project_id": ids["project"], "channel": "viblo", "account": "evo", "account_kind": "project"}
    with live.connect(hub_db) as conn:
        channel_id = one(conn, insert(t.marketing_channels).values(account).returning(t.marketing_channels.c.id))
        post = {"project_id": ids["project"], "channel_id": channel_id, "channel": "viblo", "account": "evo"}
        post = {**post, "body": "hi"}
        with pytest.raises(errors.CheckViolation):
            conn.execute(insert(t.marketing_posts).values({**post, "state": "queued"}))  # not a state yet
    move_to(hub_db, "0126")
    t = plugin_tables(hub_db, "marketing_channels", "marketing_posts")
    with live.connect(hub_db) as conn:
        assert one(conn, select(t.marketing_channels.c.session_state)) == "none"  # the safe default
        for state in ("queued", "posting"):
            conn.execute(insert(t.marketing_posts).values({**post, "state": state, "screenshot": b"png"}))
        with pytest.raises(errors.CheckViolation):
            conn.execute(insert(t.marketing_channels).values({**account, "account": "b", "session_state": "maybe"}))
    move_to(hub_db, "0125", down=True)
    with live.connect(hub_db) as conn:
        found = conn.execute(select(t.marketing_posts.c.state, t.marketing_posts.c.reason)).all()
        assert [row.state for row in found] == ["draft", "draft"]
        assert all("browser adapter" in row.reason for row in found)
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0126") :]


def test_0130_curator_probes_keep_a_task_and_a_verify_and_going_back_drops_the_table(hub_db):
    move_to(hub_db, "0129")
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0130")
    t = hub_tables
    label = {"level": "internal"}
    with live.connect(hub_db) as conn:
        run_id = one(conn, _author_run(ids))
        proposal = {"project_id": ids["project"], "run_id": run_id, "lens": "harness", "kind": "docs"}
        proposal |= {
            "title": "Say where the tests live",
            "summary": "s",
            "paths": [],
            "tier": 1,
            "tier_reasons": ["a docs change"],
        }
        proposal |= {
            "evidence": [{"text": "e"}],
            "evidence_count": 1,
            "draft": {},
            "fingerprint": "f" * 64,
            "label": label,
        }
        proposal_id = one(conn, insert(t.proposals).values(proposal).returning(t.proposals.c.id))
        probe = {"project_id": ids["project"], "proposal_id": proposal_id, "repo": "demo", "task": "Add a test"}
        probe |= {"verify": "pytest -q tests/test_x.py", "source": "proposal", "label": label}
        probe_id = one(conn, insert(t.curator_probes).values(probe).returning(t.curator_probes.c.id))
        assert one(conn, select(t.curator_probes.c.state).where(t.curator_probes.c.id == probe_id)) == "active"
        assert one(conn, select(t.curator_probes.c.base_sha)) is None  # the tip of the default branch
        for bad in (
            {**probe, "state": "paused"},
            {**probe, "source": "someone"},
            {**probe, "verify": ""},
            {**probe, "verify": "x" * 2001},
            {**probe, "task": ""},
            {**probe, "base_sha": "main"},
            {**probe, "proposal_id": None},  # a probe belongs to a proposal or a change
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(insert(t.curator_probes).values(bad))
    with live.connect(hub_db) as conn:  # a proposal takes its probe with it
        conn.execute(delete(t.proposals).where(t.proposals.c.id == proposal_id))
        assert one(conn, select(func.count()).select_from(t.curator_probes)) == 0
    move_to(hub_db, "0129", down=True)
    assert "curator_probes" not in tables(hub_db)
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0130") :]


def test_0132_notices_take_the_weekly_probe_report_and_going_back_deletes_those_notices(hub_db):
    move_to(hub_db, "0131")
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    before = query(hub_db, SNAPSHOT)
    n = hub_tables.notifications
    notice = {"user_id": ids["user"], "kind": "notice", "title": "Probe report", "project_id": ids["project"]}
    with live.connect(hub_db) as conn, pytest.raises(errors.CheckViolation):
        conn.execute(insert(n).values({**notice, "notice_kind": "curator_probe_report"}))
    move_to(hub_db, "0132")
    with live.connect(hub_db) as conn:
        for kind in ("curator_probe_report", "browser_needs_login", "release"):
            conn.execute(insert(n).values({**notice, "notice_kind": kind}))
        with pytest.raises(errors.CheckViolation):
            conn.execute(insert(n).values({**notice, "notice_kind": "curator_probe_weekly"}))
    # Going back deletes the reports and keeps the other kinds.
    move_to(hub_db, "0131", down=True)
    assert query(hub_db, select(n.c.notice_kind).order_by(n.c.id)) == [("browser_needs_login",), ("release",)]
    with live.connect(hub_db) as conn:
        conn.execute(delete(n).where(n.c.notice_kind.in_(("browser_needs_login", "release"))))
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0132") :]


def test_0133_fix_proposals_one_open_per_change_findings_and_replacement_probes_name_theirs(hub_db):
    move_to(hub_db, "0132")
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0133")
    t = hub_tables
    label = {"level": "internal"}
    with live.connect(hub_db) as conn:
        run_id = one(conn, _author_run(ids))
        proposal = {"project_id": ids["project"], "run_id": run_id, "lens": "corrections", "kind": "docs"}
        proposal |= {"title": "Say where the tests live", "paths": [], "tier": 1, "tier_reasons": ["docs"]}
        proposal |= {"evidence": [], "evidence_count": 1, "draft": {}, "fingerprint": "f" * 64, "label": label}
        merged = one(conn, insert(t.proposals).values(proposal).returning(t.proposals.c.id))
        change = {"project_id": ids["project"], "proposal_id": merged, "tier": 1, "state": "open"}
        change_id = one(conn, insert(t.curator_changes).values(change).returning(t.curator_changes.c.id))
        fix = {**proposal, "title": "Turn the rule into a hook", "fix_of": change_id}
        first = one(conn, insert(t.proposals).values(fix).returning(t.proposals.c.id))
        with pytest.raises(errors.UniqueViolation):  # one open fix proposal per change
            conn.execute(insert(t.proposals).values({**fix, "title": "Add a check"}))
    with live.connect(hub_db) as conn:
        answered = {"state": "rejected", "answered_by": ids["user"], "answered_at": func.now()}
        conn.execute(update(t.proposals).values(answered).where(t.proposals.c.id == first))
        conn.execute(insert(t.proposals).values(fix))  # once the first is answered, another may open
        finding = {"project_id": ids["project"], "run_id": run_id, "lens": "corrections", "severity": "medium"}
        finding |= {
            "title": "No hook can check it",
            "evidence": [{"kind": "probe"}],
            "label": label,
            "fix_of": change_id,
        }
        conn.execute(insert(t.findings).values(finding))
        probe = {"project_id": ids["project"], "change_id": change_id, "repo": "demo", "task": "Add a test"}
        probe |= {"verify": "pytest -q tests/test_x.py", "source": "review", "label": label}
        old = one(conn, insert(t.curator_probes).values({**probe, "state": "retired"}).returning(t.curator_probes.c.id))
        conn.execute(insert(t.curator_probes).values({**probe, "replaces": old}))
        assert one(conn, select(func.count()).where(t.curator_probes.c.replaces == old)) == 1
    move_to(hub_db, "0132", down=True)
    assert "fix_of" not in columns(hub_db, "proposals") and "fix_of" not in columns(hub_db, "findings")
    assert "replaces" not in columns(hub_db, "curator_probes")
    with live.connect(hub_db) as conn:
        conn.execute(delete(t.findings))
        conn.execute(delete(t.proposals))
        conn.execute(delete(t.runs).where(t.runs.c.id == run_id))
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0133") :]


def test_0134_keeps_the_revisions_of_problems_the_problem_of_a_goal_and_proposals_of_one_and_going_back_drops_them(
    hub_db,
):
    move_to(hub_db, "0133")
    t = hub_tables
    p, r, g, gr, b, n = t.problems, t.problem_revisions, t.goals, t.goal_revisions, t.builder_proposals, t.notifications
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    proposal = {"project_id": ids["project"], "change": {"set": {}, "unset": []}, "summary": "s", "base_version": 0}
    proposal["proposed_by"] = ids["user"]
    goal_row = {"revision": 1, "statement": "Fewer empty searches", "rank": 1, "owner_id": ids["user"]}
    goal_row |= {"status": "active", "created_by": ids["user"]}
    with live.connect(hub_db) as conn:
        with pytest.raises(errors.CheckViolation):  # 0091 knows no problem proposal
            conn.execute(insert(b).values({**proposal, "kind": "problem"}))
        goal_id = one(conn, insert(g).values(project_id=ids["project"], number=1, revision=1).returning(g.c.id))
        conn.execute(insert(gr).values({**goal_row, "goal_id": goal_id}))  # a goal of 0133: it answers no problem
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0134")
    assert columns(hub_db, "problems") == {"id", "project_id", "number", "revision", "created_at"}
    assert columns(hub_db, "problem_revisions") == {
        "problem_id",
        "revision",
        "statement",
        "who",
        "when",
        "workaround",
        "metric",
        "rank",
        "owner_id",
        "status",
        "close_reason",
        "note",
        "created_by",
        "created_at",
    }
    assert "problem_id" in columns(hub_db, "goal_revisions")
    assert query(hub_db, select(gr.c.problem_id)) == [(None,)]  # the goal of 0133 answers none
    texts = {"statement": "Searches find nothing", "who": "shoppers", "when": "a brand has two names"}
    row = {**texts, "workaround": "they ask support", "revision": 1, "rank": 1, "owner_id": ids["user"]}
    row |= {"status": "open", "created_by": ids["user"]}
    with live.connect(hub_db) as conn:
        problem_id = one(conn, insert(p).values(project_id=ids["project"], number=1, revision=1).returning(p.c.id))
        row["problem_id"] = problem_id
        conn.execute(insert(r).values(row))
        metric = {"key": "search.zero_rate", "aggregate": "p50"}
        dropped = {"status": "dropped", "close_reason": "the search reads synonyms", "metric": metric, "note": "gone"}
        conn.execute(insert(r).values({**row, "revision": 2, **dropped}))
        conn.execute(insert(r).values({**row, "revision": 3, "status": "solved", "close_reason": "synonyms"}))
        for bad in (
            insert(p).values(project_id=ids["project"], number=1, revision=1),  # one number a project
            insert(p).values(project_id=ids["project"], number=0, revision=1),
            insert(r).values(row),  # one row per revision of a problem
            insert(r).values({**row, "revision": 0}),
            insert(r).values({**row, "revision": 4, "statement": "  "}),
            insert(r).values({**row, "revision": 4, "who": ""}),
            insert(r).values({**row, "revision": 4, "when": " "}),
            insert(r).values({**row, "revision": 4, "workaround": "x" * 2001}),
            insert(r).values({**row, "revision": 4, "metric": ["search.zero_rate"]}),
            insert(r).values({**row, "revision": 4, "rank": 0}),
            insert(r).values({**row, "revision": 4, "status": "closed", "close_reason": "x"}),
            insert(r).values({**row, "revision": 4, "status": "dropped"}),  # a closed problem names its reason
            insert(r).values({**row, "revision": 4, "close_reason": "x"}),  # and only a closed one does
            insert(r).values({**row, "revision": 4, "status": "solved", "close_reason": " "}),
            insert(r).values({**row, "revision": 4, "note": ""}),
            insert(gr).values({**goal_row, "goal_id": goal_id, "revision": 2, "problem_id": problem_id + 99}),
        ):
            with pytest.raises((errors.CheckViolation, errors.UniqueViolation, errors.ForeignKeyViolation)):
                conn.execute(bad)
        conn.execute(insert(gr).values({**goal_row, "goal_id": goal_id, "revision": 2, "problem_id": problem_id}))
        with pytest.raises(errors.ForeignKeyViolation):  # a problem a goal names is never deleted under it
            conn.execute(delete(p).where(p.c.id == problem_id))
        conn.execute(insert(b).values({**proposal, "kind": "problem", "target": {"problem": "P-2"}}))
        conn.execute(insert(b).values({**proposal, "kind": "goal"}))
        notice = {"user_id": ids["user"], "kind": "notice", "notice_kind": "builder_proposal", "title": "Problem"}
        conn.execute(insert(n).values({**notice, "details": {"proposal": 1, "kind": "problem"}}))
        conn.execute(insert(n).values({**notice, "details": {"proposal": 2, "kind": "goal"}}))
        # a project's goals and problems go with it in one statement: the goal's key waits for the statement's end
        lumen = one(
            conn,
            insert(t.projects)
            .values(name="lumen", levels=["internal"], locations=["any"], default_label={"level": "internal"})
            .values(created_by=ids["user"])
            .returning(t.projects.c.id),
        )
        lumen_problem = one(conn, insert(p).values(project_id=lumen, number=1, revision=1).returning(p.c.id))
        conn.execute(insert(r).values({**row, "problem_id": lumen_problem}))
        lumen_goal = one(conn, insert(g).values(project_id=lumen, number=1, revision=1).returning(g.c.id))
        conn.execute(insert(gr).values({**goal_row, "goal_id": lumen_goal, "problem_id": lumen_problem}))
        conn.execute(delete(t.projects).where(t.projects.c.id == lumen))
    assert query(hub_db, select(r.c.revision, r.c.status).order_by(r.c.revision)) == [
        (1, "open"),
        (2, "dropped"),
        (3, "solved"),
    ]
    assert query(hub_db, select(gr.c.revision, gr.c.problem_id).order_by(gr.c.revision)) == [
        (1, None),
        (2, problem_id),
    ]
    # Going back drops the problems and the problem of a goal, and the proposals of a problem with their notices.
    move_to(hub_db, "0133", down=True)
    assert not {"problems", "problem_revisions"} & tables(hub_db)
    assert "problem_id" not in columns(hub_db, "goal_revisions")
    assert query(hub_db, select(b.c.kind)) == [("goal",)]
    assert query(hub_db, select(n.c.details["kind"].as_string())) == [("goal",)]
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0134") :]


def test_0135_keeps_the_problems_a_claim_links_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0135")
    k = hub_tables.canvas
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        owner = {"project_id": ids["project"], "created_by": ids["user"]}
        canvas = one(
            conn,
            insert(k.canvases).values(slug="northwind", name="Northwind", label={}, **owner).returning(k.canvases.c.id),
        )
        claim = {"canvas_id": canvas, "cell": "problem", "text": "Shops run out of milk", "label": {}, **owner}
        conn.execute(insert(k.claims).values(number=1, **claim))  # links no problem by default
        conn.execute(insert(k.claims).values(number=2, problems=["P-1", "P-3"], use_cases=["UC ORD-1"], **claim))
        with pytest.raises(errors.CheckViolation):  # at most 20, as of each link of 0078
            conn.execute(insert(k.claims).values(number=3, problems=[f"P-{n}" for n in range(1, 22)], **claim))
    held = select(k.claims.c.number, k.claims.c.problems, k.claims.c.use_cases).order_by(k.claims.c.number)
    assert query(hub_db, held) == [(1, [], []), (2, ["P-1", "P-3"], ["UC ORD-1"])]
    move_to(hub_db, "0134", down=True)
    assert "problems" not in columns(hub_db, "canvas_claims")
    assert {"use_cases", "signal_keys"} <= columns(hub_db, "canvas_claims")
    with live.connect(hub_db) as conn, pytest.raises(errors.CheckViolation):  # the check of 0078 is back
        conn.execute(insert(k.claims).values(number=4, use_cases=[f"UC ORD-{n}" for n in range(21)], **claim))
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0135") :]


def test_0136_keeps_the_problem_of_a_work_item_and_the_kind_discover_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0135")
    t = hub_tables
    i, e = t.loop_items, t.loop_item_events
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        item = {"project_id": ids["project"], "kind": "brainstorm", "cycle": "business", "key": "brainstorm:G-1:r1"}
        item |= {"engine": "rules/1", "owner": "@strategist"}
        conn.execute(insert(i).values(item))  # a work item of rules/1, before any problem
        with pytest.raises(errors.CheckViolation):  # 0123 knows no discover
            conn.execute(insert(i).values({**item, "key": "discover:c:CL-1:b1", "kind": "discover"}))
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0136")
    assert "problem_id" in columns(hub_db, "loop_items")
    assert query(hub_db, select(i.c.key, i.c.problem_id)) == [("brainstorm:G-1:r1", None)]
    texts = {"statement": "Shops run out of milk", "who": "shop owners", "when": "before a delivery"}
    row = {**texts, "workaround": "they buy it retail", "revision": 1, "rank": 1, "owner_id": ids["user"]}
    row |= {"status": "open", "created_by": ids["user"]}
    p = t.problems
    with live.connect(hub_db) as conn:
        problem_id = one(conn, insert(p).values(project_id=ids["project"], number=1, revision=1).returning(p.c.id))
        conn.execute(insert(t.problem_revisions).values({**row, "problem_id": problem_id}))
        found = {**item, "key": "discover:c:CL-1:b1", "kind": "discover", "engine": "rules/2", "problem_id": problem_id}
        found_id = one(conn, insert(i).values(found).returning(i.c.id))
        conn.execute(insert(e).values(item_id=found_id, kind="created", state="queued"))
        built = {**found, "key": "build:claim:c:CL-2:b1", "kind": "build", "cycle": "development", "owner": None}
        conn.execute(insert(i).values(built))
        for bad in (
            insert(i).values({**found, "key": "x", "cycle": "development"}),  # a discover is of the business cycle
            insert(i).values({**found, "key": "y", "problem_id": problem_id + 99}),
        ):
            with pytest.raises((errors.CheckViolation, errors.ForeignKeyViolation)):
                conn.execute(bad)
        # a problem deleted under a work item leaves it, its link cleared
        conn.execute(delete(t.problem_revisions).where(t.problem_revisions.c.problem_id == problem_id))
        conn.execute(delete(p).where(p.c.id == problem_id))
    held = select(i.c.key, i.c.kind, i.c.problem_id).order_by(i.c.id)
    assert query(hub_db, held) == [
        ("brainstorm:G-1:r1", "brainstorm", None),
        ("discover:c:CL-1:b1", "discover", None),
        ("build:claim:c:CL-2:b1", "build", None),
    ]
    # going back deletes the discover with its ledger and drops the column; the rest stays
    move_to(hub_db, "0135", down=True)
    assert "problem_id" not in columns(hub_db, "loop_items")
    assert query(hub_db, select(i.c.key).order_by(i.c.id)) == [("brainstorm:G-1:r1",), ("build:claim:c:CL-2:b1",)]
    assert query(hub_db, select(func.count()).select_from(e)) == [(0,)]
    with live.connect(hub_db) as conn, pytest.raises(errors.CheckViolation):  # the check of 0123 is back
        conn.execute(insert(i).values({**item, "key": "discover:c:CL-3:b1", "kind": "discover"}))
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0136") :]


def test_0137_keeps_the_evidence_of_a_problem_and_going_back_drops_it(hub_db):
    move_to(hub_db, "0136")
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0137")
    t = hub_tables
    e = t.problem_evidence
    assert "problem_evidence" in tables(hub_db)
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        texts = {"statement": "Shops run out of milk", "who": "shop owners", "when": "before a delivery"}
        row = {**texts, "workaround": "they buy it retail", "revision": 1, "rank": 1, "owner_id": ids["user"]}
        row |= {"status": "open", "created_by": ids["user"]}
        p = t.problems
        problem_id = one(conn, insert(p).values(project_id=ids["project"], number=1, revision=1).returning(p.c.id))
        conn.execute(insert(t.problem_revisions).values({**row, "problem_id": problem_id}))
        line = {
            "problem_id": problem_id,
            "number": 1,
            "source_kind": "email",
            "source_uri": "mailto:a@northwind.example",
        }
        line |= {"excerpt": "We ring the depot.", "signal": "workaround", "label": "internal", "added_by": ids["user"]}
        conn.execute(insert(e).values(line))
        for bad in (
            {**line, "number": 2, "label": "personal"},
            {**line, "number": 3, "excerpt": "x" * 301},
            {**line, "number": 4, "signal": "rumour"},
            {**line, "number": 5, "source_kind": "tweet"},
            {**line, "number": 1},
        ):
            with pytest.raises((errors.CheckViolation, errors.UniqueViolation)):
                conn.execute(insert(e).values(bad))
    move_to(hub_db, "0136", down=True)
    assert "problem_evidence" not in tables(hub_db)
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0137") :]


def test_0138_keeps_the_intakes_of_a_project_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0137")
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0138")
    n = hub_tables.intakes
    assert "intakes" in tables(hub_db)
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        row = {"project_id": ids["project"], "number": 1, "requested_by": ids["user"], "sources": []}
        conn.execute(insert(n).values(row))
        done = {"result": {}, "outcome": {}, "put_by": ids["user"], "done_at": func.now(), "status": "done"}
        conn.execute(insert(n).values({**row, "number": 2, **done}))
        for bad in (
            {**row, "number": 3, "status": "stuck"},
            {**row, "number": 0},
            {**row, "number": 4, "sources": {}},
            {**row, "number": 5, "note": "x" * 1001},
            {**row, "number": 6, "status": "done"},  # done without its result
            {**row, "number": 7, **{k: v for k, v in done.items() if k != "status"}},  # a result while open
            {**row, "number": 1},
        ):
            with pytest.raises((errors.CheckViolation, errors.UniqueViolation)):
                conn.execute(insert(n).values(bad))
    move_to(hub_db, "0137", down=True)
    assert "intakes" not in tables(hub_db)
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0138") :]


def test_0141_keeps_the_dismissed_review_items_of_a_project_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0140")
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0141")
    d = hub_tables.kg_review_dismissals
    assert "kg_review_dismissals" in tables(hub_db)
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        row = {
            "project_id": ids["project"],
            "fingerprint": "r:0123456789abcdef",
            "rule": "possible_duplicate",
            "reason": "two names of one thing, on purpose",
            "dismissed_by": ids["user"],
        }
        conn.execute(insert(d).values(row))
        conn.execute(insert(d).values({**row, "fingerprint": "r:fedcba9876543210", "rule": "bulk_noise"}))
        for bad in (
            {**row, "fingerprint": "r:1", "rule": "whatever"},
            {**row, "fingerprint": "r:2", "reason": "   "},
            {**row, "fingerprint": "r:3", "reason": "x" * 1001},
            {**row, "fingerprint": ""},
            row,  # one dismissal per item of a project
        ):
            with pytest.raises((errors.CheckViolation, errors.UniqueViolation)):
                conn.execute(insert(d).values(bad))
    move_to(hub_db, "0140", down=True)
    assert "kg_review_dismissals" not in tables(hub_db)
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0141") :]


def test_0143_keeps_the_opportunities_of_a_project_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0142")
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0143")
    t = hub_tables
    assert pg.OPPORTUNITY_TABLES <= set(tables(hub_db))
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        o = t.opportunities
        made = insert(o).values(project_id=ids["project"], number=1, revision=1).returning(o.c.id)
        opportunity_id = one(conn, made)
        texts = {"statement": "Sell to the depot", "beneficiary": "shops", "leverage": "an open door"}
        row = {**texts, "revision": 1, "rank": 1, "owner_id": ids["user"], "status": "open"}
        row |= {"created_by": ids["user"], "opportunity_id": opportunity_id}
        conn.execute(insert(t.opportunity_revisions).values(row))
        for bad in (
            {**row, "revision": 2, "status": "won"},
            {**row, "revision": 2, "status": "missed"},  # closed without its reason
            {**row, "revision": 2, "close_reason": "late"},  # a reason while open
            {**row, "revision": 2, "statement": ""},
            {**row, "revision": 2, "rank": 0},
            {**row, "revision": 2, "metric": []},
            {**row, "revision": 1},
        ):
            with pytest.raises((errors.CheckViolation, errors.UniqueViolation)):
                conn.execute(insert(t.opportunity_revisions).values(bad))
        with pytest.raises(errors.UniqueViolation):
            conn.execute(insert(o).values(project_id=ids["project"], number=1, revision=1))
    move_to(hub_db, "0142", down=True)
    assert not pg.OPPORTUNITY_TABLES & set(tables(hub_db))
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0143") :]


def test_0142_keeps_the_kg_decisions_of_a_project_with_their_history_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0141")
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0142")
    d, e = hub_tables.kg_decisions, hub_tables.kg_decision_events
    assert {"kg_decisions", "kg_decision_events"} <= tables(hub_db)
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        project = ids["project"]
        node = {"project_id": project, "kind": "deprecate", "state": "active", "src": "app:file:a.py"}
        first = conn.execute(insert(d).values({**node, "number": 1}).returning(d.c.id)).scalar_one()
        conn.execute(insert(d).values({**node, "number": 2, "rel": "calls", "dst": "app:file:b.py"}))
        conn.execute(insert(d).values({**node, "number": 3, "kind": "merge", "dst": "app:file:b.py"}))
        conn.execute(insert(d).values({**node, "number": 4, "kind": "link", "rel": "implements", "dst": "usecase:X"}))
        for bad in (
            {**node, "number": 5, "kind": "rename"},
            {**node, "number": 5, "state": "orphan"},  # an orphan is worked out when read, never stored
            {**node, "number": 5, "rel": "calls"},  # an edge needs both rel and dst
            {**node, "number": 5, "kind": "merge"},  # a merge names two nodes
            {**node, "number": 5, "kind": "merge", "dst": "app:file:a.py"},  # two different ones
            {**node, "number": 5, "kind": "link", "dst": "app:file:b.py"},  # a link names its relation
            {**node, "number": 5, "src": ""},
            {**node, "number": 0},
            {**node, "number": 1},  # one decision per number of a project
        ):
            with pytest.raises((errors.CheckViolation, errors.UniqueViolation)):
                conn.execute(insert(d).values(bad))
        event = {"project_id": project, "rev": 1, "decision_id": first, "action": "decide", "actor_id": ids["user"]}
        conn.execute(insert(e).values({**event, "reason": "replaced by b.py"}))
        for bad in (
            {**event, "rev": 2, "action": "approve", "reason": "x"},
            {**event, "rev": 2, "reason": "   "},
            {**event, "rev": 2, "reason": "x" * 1001},
            {**event, "reason": "again"},  # one event per revision of a project
        ):
            with pytest.raises((errors.CheckViolation, errors.UniqueViolation)):
                conn.execute(insert(e).values(bad))
    move_to(hub_db, "0141", down=True)
    assert not {"kg_decisions", "kg_decision_events"} & tables(hub_db)
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0142") :]


def test_0144_notices_take_the_opportunity_window_and_going_back_deletes_those_notices(hub_db):
    move_to(hub_db, "0143")
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    before = query(hub_db, SNAPSHOT)
    n = hub_tables.notifications
    notice = {"user_id": ids["user"], "kind": "notice", "title": "Window", "project_id": ids["project"]}
    with live.connect(hub_db) as conn, pytest.raises(errors.CheckViolation):
        conn.execute(insert(n).values({**notice, "notice_kind": "opportunity_window"}))
    move_to(hub_db, "0144")
    with live.connect(hub_db) as conn:
        for kind in ("opportunity_window", "curator_probe_report", "release"):
            conn.execute(insert(n).values({**notice, "notice_kind": kind}))
        with pytest.raises(errors.CheckViolation):
            conn.execute(insert(n).values({**notice, "notice_kind": "opportunity_closing"}))
    # Going back deletes the reminders and keeps the other kinds.
    move_to(hub_db, "0143", down=True)
    assert query(hub_db, select(n.c.notice_kind).order_by(n.c.id)) == [("curator_probe_report",), ("release",)]
    with live.connect(hub_db) as conn:
        conn.execute(delete(n).where(n.c.notice_kind.in_(("curator_probe_report", "release"))))
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0144") :]


def test_0145_keeps_the_opportunities_a_claim_links_the_answer_of_a_goal_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0144")
    t = hub_tables
    k, o, g, gr = t.canvas, t.opportunities, t.goals, t.goal_revisions
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        owner = {"project_id": ids["project"], "created_by": ids["user"]}
        canvas = one(
            conn,
            insert(k.canvases).values(slug="northwind", name="Northwind", label={}, **owner).returning(k.canvases.c.id),
        )
        claim = {"canvas_id": canvas, "cell": "problem", "text": "Shops run out of milk", "label": {}, **owner}
        conn.execute(insert(k.claims).values(number=1, problems=["P-1"], **claim))
    assert "opportunity_id" not in columns(hub_db, "goal_revisions")
    assert "opportunity_id" not in columns(hub_db, "loop_items")
    move_to(hub_db, "0145")
    assert "opportunities" in columns(hub_db, "canvas_claims")
    assert "opportunity_id" in columns(hub_db, "goal_revisions")
    assert "opportunity_id" in columns(hub_db, "loop_items")
    assert query(hub_db, select(k.claims.c.opportunities)) == [([],)]  # a claim of 0144 links none
    goal_row = {"revision": 1, "statement": "Win the depot", "rank": 1, "owner_id": ids["user"]}
    goal_row |= {"status": "active", "created_by": ids["user"]}
    with live.connect(hub_db) as conn:
        conn.execute(insert(k.claims).values(number=2, opportunities=["O-1", "O-2"], **claim))
        with pytest.raises(errors.CheckViolation):  # at most 20 of each link
            conn.execute(insert(k.claims).values(number=3, opportunities=[f"O-{n}" for n in range(1, 22)], **claim))
    with live.connect(hub_db) as conn:
        opportunity_id = one(conn, insert(o).values(project_id=ids["project"], number=1, revision=1).returning(o.c.id))
        problem_id = one(
            conn, insert(t.problems).values(project_id=ids["project"], number=1, revision=1).returning(t.problems.c.id)
        )
        goal_id = one(conn, insert(g).values(project_id=ids["project"], number=1, revision=1).returning(g.c.id))
        conn.execute(insert(gr).values({**goal_row, "goal_id": goal_id, "opportunity_id": opportunity_id}))
        with pytest.raises(errors.CheckViolation):  # a goal answers a problem or an opportunity, not both
            conn.execute(
                insert(gr).values(
                    {**goal_row, "goal_id": goal_id, "revision": 2, "opportunity_id": opportunity_id}
                    | {"problem_id": problem_id}
                )
            )
    with live.connect(hub_db) as conn, pytest.raises(errors.ForeignKeyViolation):
        conn.execute(insert(gr).values({**goal_row, "goal_id": goal_id, "revision": 2, "opportunity_id": 9999}))
    with live.connect(hub_db) as conn:
        conn.execute(delete(gr).where(gr.c.opportunity_id.is_not(None)))
        conn.execute(delete(k.claims).where(k.claims.c.number > 1))
    move_to(hub_db, "0144", down=True)
    assert "opportunities" not in columns(hub_db, "canvas_claims")
    assert "opportunity_id" not in columns(hub_db, "goal_revisions")
    assert "opportunity_id" not in columns(hub_db, "loop_items")
    held = select(k.claims.c.number, k.claims.c.problems)
    assert query(hub_db, held) == [(1, ["P-1"])]
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0145") :]


def test_0146_adds_the_browser_install_of_a_worker_and_going_back_drops_it(hub_db):
    move_to(hub_db, "0145")
    workers = hub_tables.workers
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        token_id = one(
            conn, _token(ids, kind="worker", token_hash="5" * 64, host="vps").returning(hub_tables.tokens.c.id)
        )
        worker = one(conn, _worker_row(ids, token_id, "vps", 1))
    move_to(hub_db, "0146")
    added = {
        "browser_install_requested_at",
        "browser_install_ended_at",
        "browser_install_outcome",
        "browser_install_reason",
    }
    assert added <= columns(hub_db, "workers")
    shown = select(*(workers.c[name] for name in sorted(added))).where(workers.c.id == worker)
    assert query(hub_db, shown) == [(None, None, None, None)]
    asked, ended = {"browser_install_requested_at": func.now()}, {"browser_install_ended_at": func.now()}
    bad_requests = (
        ended | {"browser_install_outcome": "installed"},  # an end without a request
        asked | ended,  # an end without an outcome
        asked | {"browser_install_outcome": "installed"},  # an outcome without an end
        asked | ended | {"browser_install_outcome": "current"},
        asked | {"browser_install_reason": "why"},  # a reason without an outcome
    )
    with live.connect(hub_db) as conn:
        for bad in bad_requests:
            with pytest.raises(errors.CheckViolation):
                conn.execute(update(workers).values(**bad).where(workers.c.id == worker))
        conn.execute(update(workers).values(**asked).where(workers.c.id == worker))
        done = ended | {"browser_install_outcome": "failed", "browser_install_reason": "npx is not on PATH"}
        conn.execute(update(workers).values(**done).where(workers.c.id == worker))
    found = select(workers.c.browser_install_outcome, workers.c.browser_install_reason)
    assert query(hub_db, found) == [("failed", "npx is not on PATH")]
    move_to(hub_db, "0145", down=True)
    assert not added & columns(hub_db, "workers")
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0146") :]


def test_0147_turns_canvas_srs_and_tasks_into_features_and_going_back_restores_the_project_plugins(hub_db):
    move_to(hub_db, "0146")
    t = hub_tables
    plugins, pp, settings = t.plugins, t.project_plugins, t.hub_settings
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        user = ids["user"]
        lumen = insert(t.projects).values(
            name="lumen", levels=["internal"], locations=["any"], default_label={"level": "internal"}, created_by=user
        )
        ids["lumen"] = one(conn, lumen.returning(t.projects.c.id))
        plugin = {
            name: one(conn, insert(plugins).values(name=name, publisher="evo-agents").returning(plugins.c.id))
            for name in ("canvas", "srs", "tasks", "northwind")
        }
        grants = {"canvas": ["runs:read", "signals:read"], "srs": ["notifications:send"], "northwind": ["plans:read"]}
        for name, permissions in grants.items():
            values = {"project_id": ids["project"], "plugin_id": plugin[name], "permissions": permissions}
            conn.execute(insert(pp).values(values | {"enabled_by": user, "granted_by": user}))
        conn.execute(
            insert(pp).values(
                project_id=ids["lumen"], plugin_id=plugin["tasks"], permissions=[], enabled_by=user, granted_by=user
            )
        )
        layer = {"kind": "features", "project_id": ids["project"], "changed_by": user}
        conn.execute(insert(settings).values(layer | {"version": 1, "values": {"chats": False}}))
    before = query(
        hub_db, select(*(pp.c[name] for name in sorted(pp.c.keys()))).order_by(pp.c.project_id, pp.c.plugin_id)
    )
    move_to(hub_db, "0147")
    assert {"retired_project_plugins"} <= tables(hub_db) and "builtin" in columns(hub_db, "plugins")
    builtin = select(plugins.c.name).where(plugins.c.builtin).order_by(plugins.c.name)
    assert query(hub_db, builtin) == [("canvas",), ("srs",), ("tasks",)]
    left = select(pp.c.plugin_id, pp.c.permissions)
    assert query(hub_db, left) == [(plugin["northwind"], ["plans:read"])]  # a plugin of the store stays a plugin
    features = (
        select(settings.c.project_id, settings.c.version, settings.c["values"], settings.c.changed_by)
        .where(settings.c.kind == "features")
        .order_by(settings.c.project_id, settings.c.version)
    )
    assert query(hub_db, features) == [
        (ids["project"], 1, {"chats": False}, user),
        (ids["project"], 2, {"chats": False, "canvas": True, "srs": True}, user),  # its values, and the features on
        (ids["lumen"], 1, {"tasks": True}, user),
    ]
    retired = t.retired_project_plugins
    kept = select(retired.c.project_id, retired.c.plugin_id, retired.c.permissions, retired.c.settings_version)
    assert sorted(query(hub_db, kept)) == sorted(
        [
            (ids["project"], plugin["canvas"], ["runs:read", "signals:read"], 2),
            (ids["project"], plugin["srs"], ["notifications:send"], 2),
            (ids["lumen"], plugin["tasks"], [], 1),
        ]
    )
    with live.connect(hub_db) as conn:  # a later version on top of the one 0147 wrote
        conn.execute(insert(settings).values(layer | {"version": 3, "values": {"chats": True, "canvas": True}}))
    move_to(hub_db, "0146", down=True)
    assert "retired_project_plugins" not in tables(hub_db) and "builtin" not in columns(hub_db, "plugins")
    after = query(
        hub_db, select(*(pp.c[name] for name in sorted(pp.c.keys()))).order_by(pp.c.project_id, pp.c.plugin_id)
    )
    assert after == before  # each row back with the permissions it had
    assert query(hub_db, select(settings.c.project_id, settings.c.version, settings.c["values"])) == [
        (ids["project"], 1, {"chats": False}),
        (ids["project"], 3, {"chats": True}),  # 0146 knows no feature canvas
    ]
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0147") :]


def test_0148_adds_worker_pools_and_their_join_tokens_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0147")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        token_id = one(conn, _token(ids, kind="worker", token_hash="7" * 64, host="vps").returning(t.tokens.c.id))
        worker = one(conn, _worker_row(ids, token_id, "vps", 1))
    move_to(hub_db, "0148")
    assert pg.POOL_TABLES <= tables(hub_db)
    assert "pool_id" in columns(hub_db, "workers")
    assert query(hub_db, select(t.workers.c.pool_id).where(t.workers.c.id == worker)) == [(None,)]
    pools, tokens = t.worker_pools, t.worker_join_tokens
    with live.connect(hub_db) as conn:
        pool = one(conn, insert(pools).values(owner_id=ids["user"], name="CI").returning(pools.c.id))
        conn.execute(insert(t.worker_pool_projects).values(pool_id=pool, project_id=ids["project"]))
        made = {"pool_id": pool, "created_by": ids["user"], "expires_at": func.now() + DAY, "max_uses": 2}
        join_token = one(conn, insert(tokens).values(token_hash="8" * 64, **made).returning(tokens.c.id))
        conn.execute(update(t.workers).values(pool_id=pool).where(t.workers.c.id == worker))
    defaults = select(pools.c.slots, pools.c.labels, pools.c.taints, pools.c.accepted_findings, pools.c.reap_after_days)
    assert query(hub_db, defaults) == [(1, [], [], [], 7)]
    assert query(hub_db, select(tokens.c.uses, tokens.c.revoked_at)) == [(0, None)]
    bad_pools = ({"name": "ci"}, {"name": "x", "slots": 17}, {"name": "y", "reap_after_days": 0})
    bad_pools += ({"name": "z", "reap_after_days": 91}, {"name": ""})
    for bad in bad_pools:
        with live.connect(hub_db) as conn, pytest.raises((errors.CheckViolation, errors.UniqueViolation)):
            conn.execute(insert(pools).values(owner_id=ids["user"], **bad))
    bad_tokens = (
        {"token_hash": "8" * 64},  # one hash, one token
        {"token_hash": "9" * 64, "max_uses": 101},
        {"token_hash": "9" * 64, "max_uses": 0},
        {"token_hash": "9" * 64, "uses": 3},  # more uses than it takes
        {"token_hash": "9" * 64, "expires_at": func.now() - DAY},
    )
    for bad in bad_tokens:
        with live.connect(hub_db) as conn, pytest.raises((errors.CheckViolation, errors.UniqueViolation)):
            conn.execute(insert(tokens).values({**made, **bad}))
    with live.connect(hub_db) as conn, pytest.raises(errors.ForeignKeyViolation):  # not deleted under its nodes
        conn.execute(delete(pools).where(pools.c.id == pool))
    assert query(hub_db, select(tokens.c.id)) == [(join_token,)]
    move_to(hub_db, "0147", down=True)
    assert not pg.POOL_TABLES & tables(hub_db)
    assert "pool_id" not in columns(hub_db, "workers")
    assert query(hub_db, select(t.workers.c.name)) == [("vps",)]  # the node stays a worker, without its pool
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0148") :]


def test_0149_adds_worker_taints_and_run_selectors_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0148")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        token_id = one(conn, _token(ids, kind="worker", token_hash="7" * 64, host="vps").returning(t.tokens.c.id))
        worker = one(conn, _worker_row(ids, token_id, "vps", 1))
        run_id = one(conn, _author_run(ids))
    move_to(hub_db, "0149")
    assert "taints" in columns(hub_db, "workers")
    assert {"selector", "tolerations"} <= columns(hub_db, "runs")
    # what was there before places as before: no taint, no selector
    assert query(hub_db, select(t.workers.c.taints).where(t.workers.c.id == worker)) == [([],)]
    assert query(hub_db, select(t.runs.c.selector, t.runs.c.tolerations).where(t.runs.c.id == run_id)) == [([], [])]
    move_to(hub_db, "0148", down=True)
    assert "taints" not in columns(hub_db, "workers")
    assert not {"selector", "tolerations"} & columns(hub_db, "runs")
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0149") :]


def test_0151_keeps_provider_accounts_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0150")
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    move_to(hub_db, "0151")
    s, p = hub_tables.secrets, hub_tables.provider_accounts
    key = {"sealed": b"x" * 32, "nonce": b"n" * 12, "key_id": "0123abcd"}
    made = insert(s).values(owner_id=ids["user"], name="openrouter-main", kind="provider", **key).returning(s.c.id)
    endpoint = {"provider": "openrouter", "base_url": "https://openrouter.ai/api/v1", "api_format": "openai"}
    with live.connect(hub_db) as conn:
        secret_id = one(conn, made)
        conn.execute(insert(p).values(secret_id=secret_id, **endpoint, models=["a/b"]))
    bad_rows = (
        endpoint | {"api_format": "soap"},
        endpoint | {"base_url": "http://openrouter.ai/api/v1"},
        endpoint | {"models": {"a": 1}},
    )
    for number, bad in enumerate(bad_rows):
        other = insert(s).values(owner_id=ids["user"], name=f"bad-{number}", kind="provider", **key)
        with live.connect(hub_db) as conn, pytest.raises(errors.CheckViolation):
            new_id = one(conn, other.returning(s.c.id))
            conn.execute(insert(p).values(secret_id=new_id, **bad))
    assert query(hub_db, select(p.c.provider, p.c.models)) == [("openrouter", ["a/b"])]
    move_to(hub_db, "0150", down=True)
    assert "provider_accounts" not in tables(hub_db)
    assert query(hub_db, select(func.count()).select_from(s).where(s.c.kind == "provider")) == [(0,)]
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0151") :]


def test_0152_takes_gateway_leases_and_runs_and_going_back_refuses_while_one_is_recorded(hub_db):
    move_to(hub_db, "0151")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    move_to(hub_db, "0152")
    run = {
        "project_id": ids["project"],
        "kind": "judge",
        "dispatched_by": ids["user"],
        "dispatched_via": "machine",
        "requested_runtime": "claude-code",
        "runtime": "claude-code",
        "mode": "headless",
        "approval": "auto",
        "timeout_s": 600,
        "repos": [{"repo": "lumen", "branch": "plan/report"}],
        "gateway_provider": "openrouter",
    }
    cl = t.credential_leases
    key = {"provider": "gateway", "target": "openrouter/auto", "external_id": "key-1", "worker_id": None}
    sealed = {"sealed_value": b"x" * 32, "nonce": b"n" * 12, "key_id": "0123abcd", "token_hash": b"h" * 32}
    with live.connect(hub_db) as conn:
        worker_id = one(conn, _worker_row(ids, ids["token"], "mac-mini", 1))
        run_id = one(conn, insert(t.runs).values(run).returning(t.runs.c.id))
        key["worker_id"] = worker_id
        made = insert(cl).values(run_id=run_id, **key, **sealed, expires_at=func.now() + timedelta(hours=1))
        conn.execute(made)
    for bad in (
        {"gateway_provider": "Open Router"},  # runs_gateway_provider_check
    ):
        with live.connect(hub_db) as conn, pytest.raises(errors.CheckViolation):
            conn.execute(insert(t.runs).values(run | bad))
    for bad in (
        sealed | {"token_hash": b"short"},  # a SHA-256
        {"token_hash": b"h" * 32, "provider": "secret"},  # a token is a gateway lease's alone
    ):
        row = {"run_id": run_id, **key, **bad, "expires_at": func.now() + timedelta(hours=1)}
        with live.connect(hub_db) as conn, pytest.raises(errors.CheckViolation):
            conn.execute(insert(cl).values(row))
    with live.connect(hub_db) as conn, pytest.raises(errors.UniqueViolation):
        conn.execute(insert(cl).values(run_id=run_id, **key, **sealed, expires_at=func.now() + timedelta(hours=1)))
    with pytest.raises(Exception, match="a run of a gateway model or a gateway lease is recorded"):
        move_to(hub_db, "0151", down=True)
    with live.connect(hub_db) as conn:
        conn.execute(delete(cl).where(cl.c.run_id == run_id))
        conn.execute(update(t.runs).values(gateway_provider=None).where(t.runs.c.id == run_id))
    move_to(hub_db, "0151", down=True)
    assert "token_hash" not in columns(hub_db, "credential_leases") and "gateway_provider" not in columns(
        hub_db, "runs"
    )
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0152") :]


def test_0153_notices_take_the_kinds_of_core_plugins_and_going_back_deletes_those_notices(hub_db):
    move_to(hub_db, "0152")
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    before = query(hub_db, SNAPSHOT)
    n = hub_tables.notifications
    notice = {"user_id": ids["user"], "kind": "notice", "title": "Overdue", "project_id": ids["project"]}
    with live.connect(hub_db) as conn, pytest.raises(errors.CheckViolation):
        conn.execute(insert(n).values({**notice, "notice_kind": "northwind_ledger_overdue"}))
    move_to(hub_db, "0153")
    with live.connect(hub_db) as conn:
        for kind in ("northwind_ledger_overdue", "lumen_relay_due", "release", "pm_daily"):
            conn.execute(insert(n).values({**notice, "notice_kind": kind}))
        for wrong in ("Northwind_overdue", "overdue", "northwind-ledger_overdue", "_overdue", "northwind_"):
            with pytest.raises(errors.CheckViolation):
                conn.execute(insert(n).values({**notice, "notice_kind": wrong}))
    # Going back deletes the notices of the plugins' kinds and keeps the hub's.
    move_to(hub_db, "0152", down=True)
    kinds = query(hub_db, select(n.c.notice_kind).order_by(n.c.id))
    assert kinds == [("release",), ("pm_daily",)]
    with live.connect(hub_db) as conn, pytest.raises(errors.CheckViolation):
        conn.execute(insert(n).values({**notice, "notice_kind": "northwind_ledger_overdue"}))
    with live.connect(hub_db) as conn:
        conn.execute(delete(n))
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0153") :]


def test_0154_adds_the_market_sources_publishers_and_install_requests_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0153")
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0154")
    assert pg.MARKET_TABLES <= tables(hub_db)
    t = hub_tables
    sources, publishers, requests = t.market_sources, t.plugin_publishers, t.market_install_requests
    signer = "sigstore:lumen-labs/lumen-notes/.github/workflows/ci.yml"
    with live.connect(hub_db) as conn:
        builtin = one(conn, insert(sources).values(repo="northwind/market", builtin=True).returning(sources.c.id))
        added = one(
            conn, insert(sources).values(repo="lumen-labs/market", added_by=ids["user"]).returning(sources.c.id)
        )
        row = conn.execute(select(sources).where(sources.c.id == builtin)).mappings().one()
        assert (row["ref"], row["path"], row["releases"], row["index"]) == ("main", "market.json", {}, None)
        conn.execute(insert(publishers).values(publisher="lumen", signer=signer, added_by=ids["user"]))
        request = {
            "project_id": ids["project"],
            "source_id": added,
            "plugin": "lumen-notes",
            "requested_by": ids["user"],
        }
        conn.execute(insert(requests).values(request))
    bad = (
        (sources, {"repo": "northwind", "added_by": ids["user"]}),  # not OWNER/REPO
        (sources, {"repo": "northwind/other"}),  # neither the default market nor added by someone
        (sources, {"repo": "northwind/second", "builtin": True}),  # a second default market
        (sources, {"repo": "lumen-labs/market", "added_by": ids["user"]}),  # the same index twice
        (publishers, {"publisher": "Lumen", "signer": signer, "added_by": ids["user"]}),
        (publishers, {"publisher": "lumen", "signer": "github:lumen", "added_by": ids["user"]}),
        (publishers, {"publisher": "lumen", "signer": signer, "added_by": ids["user"]}),  # trusted already
        (publishers, {"publisher": "acme", "signer": signer, "added_by": ids["user"], "removed_at": func.now()}),
        (requests, request),  # a second open request
        (requests, {**request, "plugin": "Lumen"}),
        (requests, {**request, "plugin": "other", "state": "installed"}),  # decided by nobody
        (
            requests,
            {**request, "plugin": "other", "state": "merged", "decided_by": ids["user"], "decided_at": func.now()},
        ),
    )
    for target, values in bad:
        with live.connect(hub_db) as conn, pytest.raises((errors.CheckViolation, errors.UniqueViolation)):
            conn.execute(insert(target).values(values))
    with live.connect(hub_db) as conn:
        decided = {"state": "installed", "decided_by": ids["user"], "decided_at": func.now()}
        conn.execute(update(requests).values(decided))
        conn.execute(insert(requests).values(request))  # a new request once the first is decided
        gone = {"removed_by": ids["user"], "removed_at": func.now()}
        conn.execute(update(publishers).values(gone))
        conn.execute(insert(publishers).values(publisher="lumen", signer=signer, added_by=ids["user"]))
        conn.execute(delete(sources).where(sources.c.id == added))  # its requests go with it
        assert query(hub_db, select(func.count()).select_from(requests)) == [(0,)]
    move_to(hub_db, "0153", down=True)
    assert not pg.MARKET_TABLES & tables(hub_db)
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0154") :]


def test_0155_keeps_the_recommended_reason_reversible_undo_and_decision_evidence_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0154")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        run_id = one(conn, _author_run(ids))
        before = one(conn, _decision_row(ids, run_id, "wait-helper"))
    move_to(hub_db, "0155")
    added = {"recommended_reason", "reversible", "undo", "evidence"}
    assert added <= columns(hub_db, "decisions")
    d = t.decisions
    shown = select(d.c.recommended_reason, d.c.reversible, d.c.undo, d.c.evidence).where(d.c.id == before)
    assert query(hub_db, shown) == [(None, None, None, None)], "a decision made before carries none of them"
    recommended = [{"key": "a", "label": "A", "recommended": True}, {"key": "b", "label": "B"}]
    full = {"options": recommended, "recommended_reason": "A is cheaper.", "reversible": True, "undo": "Revert it."}
    full |= {"evidence": ["run:12", "P-3/E-2", "https://example.org/x"]}
    with live.connect(hub_db) as conn:
        kept = one(conn, _decision_row(ids, run_id, "wait-helper").values(**full))
        one(conn, _decision_row(ids, run_id, "wait-helper").values(reversible=False))
        for bad in (
            {"recommended_reason": "A is cheaper."},  # a reason without a recommended option
            {**full, "recommended_reason": ""},
            {**full, "recommended_reason": "x" * 501},
            {**full, "undo": "Revert it.", "reversible": False},  # how to undo what cannot be undone
            {**full, "undo": "Revert it.", "reversible": None},
            {**full, "undo": "x" * 501},
            {**full, "evidence": []},
            {**full, "evidence": [f"run:{n}" for n in range(1, 22)]},
            {**full, "evidence": ["run:1", 2]},
            {**full, "evidence": [""]},
            {**full, "evidence": {"ref": "run:1"}},
            {**full, "evidence": "run:1"},
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(_decision_row(ids, run_id, "wait-helper").values(**bad))
    found = select(d.c.recommended_reason, d.c.reversible, d.c.undo, d.c.evidence).where(d.c.id == kept)
    assert query(hub_db, found) == [("A is cheaper.", True, "Revert it.", full["evidence"])]
    move_to(hub_db, "0154", down=True)
    assert not added & columns(hub_db, "decisions")
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0155") :]


def test_0156_keeps_push_devices_their_deliveries_and_preferences_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0155")
    t = hub_tables
    n, dl = t.notifications, t.notification_deliveries
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        token = one(conn, _token(ids, kind="machine", token_hash="6" * 64, host="phone").returning(t.tokens.c.id))
        stored = insert(n).values(user_id=ids["user"], kind="notice", notice_kind="plan_finished", title="Done")
        notification = one(conn, stored.returning(n.c.id))
        one(conn, insert(dl).values(notification_id=notification).returning(dl.c.id))  # the web's
    move_to(hub_db, "0156")
    added = {"push_devices", "notification_preferences"}
    assert added <= tables(hub_db) and "device_id" in columns(hub_db, "notification_deliveries")
    d, p, c = t.push_devices, t.notification_preferences, t.notification_channels
    endpoint = "https://fcm.googleapis.com/fcm/send/a"
    device = {"user_id": ids["user"], "token_id": token, "kind": "webpush", "params": {"p256dh": "k", "auth": "a"}}
    with live.connect(hub_db) as conn:
        first = one(conn, insert(d).values(**device, address=endpoint).returning(d.c.id))
        second = one(conn, insert(d).values(**device, address=endpoint + "b", label="Phone").returning(d.c.id))
        one(conn, insert(d).values({**device, "kind": "apns", "address": endpoint}).returning(d.c.id))  # another kind
        for device_id in (first, second):
            one(conn, insert(dl).values(notification_id=notification, device_id=device_id).returning(dl.c.id))
        channel = one(conn, insert(c).values(user_id=ids["user"], kind="fake").returning(c.c.id))
        one(conn, insert(dl).values(notification_id=notification, channel_id=channel).returning(dl.c.id))
        conn.execute(insert(p).values(user_id=ids["user"], hide_details=True))
        for bad in (
            {"kind": "mail", "address": "x"},
            {"address": ""},
            {"address": "x" * 2049},
            {"address": "y", "label": ""},
            {"address": "y", "label": "x" * 101},
            {"address": "y", "params": ["p256dh"]},
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(insert(d).values({**device, **bad}))
        with pytest.raises(errors.UniqueViolation):  # an address of a kind is one device
            conn.execute(insert(d).values(**device, address=endpoint))
        with pytest.raises(errors.UniqueViolation):  # one delivery a device
            conn.execute(insert(dl).values(notification_id=notification, device_id=first))
        with pytest.raises(errors.UniqueViolation):  # one delivery on the web
            conn.execute(insert(dl).values(notification_id=notification))
        with pytest.raises(errors.CheckViolation):  # a channel or a device, not both
            conn.execute(insert(dl).values(notification_id=notification, channel_id=channel, device_id=second))
        conn.execute(delete(d).where(d.c.id == first))  # its deliveries go with it
    count = select(func.count()).select_from(dl)
    assert query(hub_db, count) == [(3,)]
    assert query(hub_db, select(p.c.hide_details)) == [(True,)]
    with live.connect(hub_db) as conn:
        conn.execute(delete(t.tokens).where(t.tokens.c.id == token))  # a token's devices go with it
    assert query(hub_db, select(func.count()).select_from(d)) == [(0,)]
    assert query(hub_db, count) == [(2,)]
    move_to(hub_db, "0155", down=True)
    assert not added & tables(hub_db) and "device_id" not in columns(hub_db, "notification_deliveries")
    assert query(hub_db, count) == [(2,)]  # the web's and the channel's stay
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0156") :]


def test_0157_keeps_the_tiers_quiet_hours_time_zone_and_summary_time_of_a_member_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0156")
    t = hub_tables
    p = t.notification_preferences
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        conn.execute(insert(p).values(user_id=ids["user"], hide_details=True))
    move_to(hub_db, "0157")
    added = {"tiers", "quiet_start", "quiet_end", "time_zone", "digest_at"}
    assert added <= columns(hub_db, "notification_preferences")
    shown = select(p.c.hide_details, p.c.tiers, p.c.quiet_start, p.c.quiet_end, p.c.time_zone, p.c.digest_at)
    assert query(hub_db, shown) == [(True, {}, None, None, None, None)], "a member's row of 0156 gets the defaults"
    chosen = {
        "tiers": {"decision": "off", "plan_finished": "interrupt", "run_failed": "brief"},
        "quiet_start": "22:00",
        "quiet_end": "07:00",
        "time_zone": "Asia/Tokyo",
        "digest_at": "08:15",
    }
    with live.connect(hub_db) as conn:
        conn.execute(update(p).values(**chosen).where(p.c.user_id == ids["user"]))
        for bad in (
            {"tiers": {"decision": "loud"}},
            {"tiers": {"decision": 1}},
            {"tiers": {"": "off"}},
            {"tiers": {"Decision": "off"}},
            {"tiers": {"x" * 65: "off"}},
            {"tiers": ["decision"]},
            {"quiet_start": None},  # quiet hours are both ends or neither
            {"quiet_end": "22:00"},  # and end at another time than they start
            {"quiet_start": "24:00"},
            {"quiet_end": "7:00"},
            {"time_zone": ""},
            {"time_zone": "x" * 65},
            {"digest_at": "8:15"},
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(update(p).values(**bad).where(p.c.user_id == ids["user"]))
        conn.execute(update(p).values(quiet_start=None, quiet_end=None, time_zone=None, digest_at=None))
    assert query(hub_db, select(p.c.tiers, p.c.quiet_start)) == [(chosen["tiers"], None)]
    move_to(hub_db, "0156", down=True)
    assert not added & columns(hub_db, "notification_preferences")
    assert query(hub_db, select(p.c.hide_details)) == [(True,)]
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0157") :]


def test_0158_keeps_the_seen_mark_and_summary_day_of_a_member_takes_the_notice_today_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0157")
    t = hub_tables
    n = t.notifications
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    notice = {"user_id": ids["user"], "kind": "notice", "title": "Today on the hub"}
    with live.connect(hub_db) as conn, pytest.raises(errors.CheckViolation):
        conn.execute(insert(n).values({**notice, "notice_kind": "today"}))
    move_to(hub_db, "0158")
    assert "today_marks" in tables(hub_db)
    assert columns(hub_db, "today_marks") == {"user_id", "seen_at", "digest_on", "updated_at"}
    m = t.today_marks
    with live.connect(hub_db) as conn:
        conn.execute(insert(m).values(user_id=ids["user"], seen_at=func.now(), digest_on=date(2026, 10, 25)))
        with pytest.raises(errors.UniqueViolation):  # one row a member
            conn.execute(insert(m).values(user_id=ids["user"]))
        # today beside the hub's other kinds and a core plugin's kind, which 0153 lets through by its shape
        for kind in ("today", "opportunity_window", "release", "northwind_ledger_overdue"):
            conn.execute(insert(n).values({**notice, "notice_kind": kind}))
        for bad in ("summary", "Today"):  # no kind of the hub's and no plugin's name: no "_", or not lower-case
            with pytest.raises(errors.CheckViolation):
                conn.execute(insert(n).values({**notice, "notice_kind": bad}))
    assert query(hub_db, select(m.c.digest_on)) == [(date(2026, 10, 25),)]
    # Going back deletes the summary pushes and the marks, keeps the other notices and the check of 0153.
    move_to(hub_db, "0157", down=True)
    assert "today_marks" not in tables(hub_db)
    kinds = select(n.c.notice_kind).order_by(n.c.id)
    assert query(hub_db, kinds) == [("opportunity_window",), ("release",), ("northwind_ledger_overdue",)]
    with live.connect(hub_db) as conn:
        conn.execute(insert(n).values({**notice, "notice_kind": "northwind_ledger_paid"}))
        with pytest.raises(errors.CheckViolation):
            conn.execute(insert(n).values({**notice, "notice_kind": "today"}))
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0158") :]


def test_0159_adds_the_hub_identity_and_the_tables_of_paid_plugins_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0158")
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0159")
    assert pg.SELLER_TABLES <= tables(hub_db)
    t = hub_tables
    identity, listings, bundles, orders = t.hub_identity, t.market_listings, t.market_bundles, t.market_orders
    payouts, purchases = t.market_payouts, t.market_purchases
    hub = "hub_" + "a" * 32
    signer = "sigstore:northwind/ledger/.github/workflows/ci.yml"
    with live.connect(hub_db) as conn:
        conn.execute(insert(identity).values(hub_id=hub))
        listing = {"plugin": "northwind-ledger", "publisher": "northwind", "signer": signer, "owner_id": ids["user"]}
        listing_id = one(conn, insert(listings).values({**listing, "usd_per_hub_month": 49.5}).returning(listings.c.id))
        assert query(hub_db, select(listings.c.state)) == [("draft",)]
        bundle = {
            "listing_id": listing_id,
            "version": "1.0.0",
            "sha256": "b" * 64,
            "size": 10,
            "signature": {"kind": "sigstore"},
            "signed_by": "x",
            "uploaded_by": ids["user"],
        }
        conn.execute(insert(bundles).values(bundle))
        order = {
            "number": "ORD-2026-0001",
            "listing_id": listing_id,
            "buyer_hub": hub,
            "buyer_name": "Ada",
            "buyer_email": "ada@example.com",
            "months": 2,
            "usd_per_month": 49.5,
            "usd_total": 99,
            "secret_hash": "c" * 64,
        }
        conn.execute(insert(orders).values(order))
        licence = {
            "license_id": "lic_" + "d" * 16,
            "valid_from": date(2026, 10, 25),
            "valid_until": date(2026, 12, 25),
            "key_id": "ed25519:" + "e" * 16,
            "token": "x.y",
        }
        paid = {"state": "paid", "invoice": "INV-1", "closed_by": ids["user"], "closed_at": func.now()}
        conn.execute(update(orders).values(**paid, **licence, issued_at=func.now()))
        payout = {"month": date(2026, 10, 1), "publisher": "northwind", "paid_by": ids["user"]}
        conn.execute(insert(payouts).values({**payout, "gross_usd": 99, "publisher_usd": 79.2, "platform_usd": 19.8}))
        purchase = {
            "seller": "northwind",
            "seller_url": "https://licenses.northwind.test",
            "plugin": "northwind-ledger",
            "publisher": "northwind",
            "signer": signer,
            "number": "ORD-2026-0001",
            "months": 2,
            "usd_total": 99,
            "secret": {"ciphertext": "x"},
            "placed_by": ids["user"],
        }
        conn.execute(insert(purchases).values(purchase))
    bad = (
        (identity, {"hub_id": hub}),  # a second identity
        (identity, {"singleton": False, "hub_id": hub}),
        (listings, {**listing, "plugin": "other", "usd_per_hub_month": 0}),
        (listings, {**listing, "usd_per_hub_month": 5}),  # listed twice
        (listings, {**listing, "plugin": "other", "usd_per_hub_month": 5, "state": "sold"}),
        (bundles, bundle),  # the same version twice
        (bundles, {**bundle, "version": "2.0", "sha256": "b" * 64}),
        (orders, {**order, "number": "ORD-2026-0002", "months": 37}),
        (orders, {**order, "number": "ORD-2026-0002", "buyer_hub": "hub_1"}),
        (orders, {**order, "number": "2026-0002"}),
        (orders, order),  # the same number twice
        (orders, {**order, "number": "ORD-2026-0002", **paid}),  # paid without a licence
        (orders, {**order, "number": "ORD-2026-0002", "state": "cancelled"}),  # closed by nobody
        (orders, {**order, "number": "ORD-2026-0002", "license_id": "lic_" + "f" * 16}),  # a licence in part
        (payouts, {**payout, "month": date(2026, 10, 2), "gross_usd": 1, "publisher_usd": 1, "platform_usd": 0}),
        (payouts, {**payout, "month": date(2026, 9, 1), "gross_usd": 2, "publisher_usd": 1, "platform_usd": 0}),
        (payouts, {**payout, "gross_usd": 1, "publisher_usd": 1, "platform_usd": 0}),  # the month paid out already
        (purchases, purchase),  # the same order twice
        (purchases, {**purchase, "number": "ORD-2026-0002", "state": "paid"}),  # paid without a licence
    )
    for target, values in bad:
        with live.connect(hub_db) as conn, pytest.raises((errors.CheckViolation, errors.UniqueViolation)):
            conn.execute(insert(target).values(values))
    with live.connect(hub_db) as conn, pytest.raises(errors.ForeignKeyViolation):
        conn.execute(delete(listings))  # a listing with orders stays
    move_to(hub_db, "0158", down=True)
    assert not pg.SELLER_TABLES & tables(hub_db)
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0159") :]


def test_0160_takes_proposals_of_a_plugin_its_install_and_its_listing_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0159")
    t = hub_tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    proposal = {"project_id": ids["project"], "change": {"set": {}, "unset": []}, "summary": "s", "base_version": 0}
    proposal["proposed_by"] = ids["user"]
    p, n = t.builder_proposals, t.notifications
    with live.connect(hub_db) as conn, pytest.raises(errors.CheckViolation):  # 0143 knows no plugin proposal
        conn.execute(insert(p).values({**proposal, "kind": "plugin"}))
    before = query(hub_db, SNAPSHOT)
    move_to(hub_db, "0160")
    assert {"opened_run_id", "outcome"} <= columns(hub_db, "builder_proposals")
    decided = {"decided_by": ids["user"], "decided_at": func.now(), "state": "applied", "applied_version": 1}
    notice = {"user_id": ids["user"], "kind": "notice", "notice_kind": "builder_proposal", "title": "Plugin"}
    with live.connect(hub_db) as conn:
        for bad in (
            {"kind": "secrets"},
            {"kind": "plugin", **decided, "outcome": ""},
            {"kind": "plugin", "outcome": "x" * 2001},
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(insert(p).values({**proposal, **bad}))
        conn.execute(insert(p).values({**proposal, "kind": "plugin", **decided, "outcome": "@builder took the task"}))
        conn.execute(insert(p).values({**proposal, "kind": "plugin_install"}))
        conn.execute(insert(p).values({**proposal, "kind": "plugin_listing"}))
        conn.execute(insert(p).values({**proposal, "kind": "policy"}))
        for number, kind in enumerate(("plugin", "plugin_install", "plugin_listing", "policy"), 1):
            conn.execute(insert(n).values({**notice, "details": {"proposal": number, "kind": kind}}))
    assert query(hub_db, select(p.c.kind).order_by(p.c.id)) == [
        ("plugin",),
        ("plugin_install",),
        ("plugin_listing",),
        ("policy",),
    ]
    # Going back drops the proposals of the three kinds with their notices, and the two columns.
    move_to(hub_db, "0159", down=True)
    assert not {"opened_run_id", "outcome"} & columns(hub_db, "builder_proposals")
    assert query(hub_db, select(n.c.details["kind"].as_string())) == [("policy",)]
    assert query(hub_db, select(p.c.kind)) == [("policy",)]
    with live.connect(hub_db) as conn:
        conn.execute(delete(n))
        conn.execute(delete(p))
    assert query(hub_db, SNAPSHOT) == before
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0160") :]

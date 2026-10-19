"""Migrations: a fresh database gets the sixty-five hub tables, a second run changes nothing, processes that start
together apply each revision once, a database at 0001 with rows in it moves to 0002, and a database the code
cannot read is refused. Then the constraints schemas 0001 and 0002 promise. Schemas 0009 and 0010 have their checks in
``tests.hub.test_run_tables``, 0011 in ``tests.hub.test_sealing``, 0012 in ``tests.hub.test_curator``, 0013 in
``tests.hub.test_session_digests``, 0014 in ``tests.hub.test_review_runs``, 0015 in ``tests.hub.test_brief``, 0017 in
``tests.hub.test_curator_changes``, 0018 in ``tests.hub.test_curator_ledger``."""

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
    null,
    select,
    table,
    union_all,
    update,
)
from sqlalchemy.dialects.postgresql import REGCLASS

from evo_agents.hub import migrate as hub_migrate
from evo_agents.hub import tables as hub_tables
from evo_agents.hub.migrate import LOCK_KEY, MigrationError, alembic_config, head_revision, migrate, revisions

# Every test here migrates a database of its own from nothing, not the copy of the migrated template.
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
TABLES |= pg.SRS_TABLES  # migration 0044
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


def test_a_fresh_database_gets_the_eighty_tables(hub_db):
    assert len(TABLES) == 80  # the name of this test counts them: a new table renames it
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
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0026") :]


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
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0029") :]


def _chat_run(ids, chat_id: int, message_id: int | None, **extra):
    """The INSERT of a queued chat run of the seeded user in demo, started by ``message_id`` of ``chat_id``."""
    runs = hub_tables.runs
    row = {"project_id": ids["project"], "dispatched_by": ids["user"], "kind": "chat", "branch": "chat/1"}
    row |= {"requested_runtime": "claude-code", "runtime": "claude-code", "mode": "headless", "approval": "auto"}
    row |= {"timeout_s": 3600, "state": "queued", "chat_id": chat_id, "chat_message_id": message_id, "hop": 1}
    return insert(runs).values(row | extra).returning(runs.c.id)


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
            update(t.chats).values(budget_usd=0).where(t.chats.c.id == chat_id),
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(bad)
    assert query(hub_db, select(t.chats.c.budget_usd)) == [(5.0,)]

    # Going back deletes the chat runs, the one going on in another's session and the retry included; the rest stays.
    move_to(hub_db, "0031", down=True)
    assert query(hub_db, select(t.runs.c.id, t.runs.c.kind).order_by(t.runs.c.id)) == [(author, "author")]
    assert query(hub_db, select(t.chat_messages.c.id)) == [(message_id,)]
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0032") :]


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
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0033") :]


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
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0034") :]


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
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0035") :]


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
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0036") :]


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
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0037") :]


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
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0038") :]


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
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0039") :]


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
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0040") :]


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
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0041") :]


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
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0042") :]


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

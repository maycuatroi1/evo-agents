"""Schemas 0009 and 0010: the workers, pairings, runs, run events and inbox of ``docs/workers.md``, and the plan runs,
decisions and notifications of ``docs/notifications.md``, in Postgres.

The checks step 3 of the worker-fleet plan names: a database with rows in it goes up to 0009, down to 0008 with the
schema 0008 had and without the worker tokens, and up again; a second active run of one step is refused by the
partial unique index; an UPDATE of run_events is refused by the trigger. Two processes migrating at once are
``tests.hub.test_migrate``'s, which apply every revision, 0009 and 0010 included. Around them: the states, event
kinds, runtimes, modes and approvals of the tables are those of ``evo_agents.hub.runs``, the claim and the reaper find
their rows through an index, and the constraints refuse rows the protocol rules out.

The checks step 2 of the plan-runs-and-decisions plan names: a plan run has no step key and a run of one step must
have one; a second active plan run of one plan is refused by the partial unique index; a timeout of 24 hours is taken
only by a plan run; and a database with runs goes up to 0010, down to 0009 with the schema 0009 had, and up again."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from psycopg import errors, sql
from psycopg.types.json import Jsonb

from evo_agents.hub import runs
from evo_agents.hub.migrate import migrate
from tests.hub.test_migrate import SNAPSHOT, move_to, one, query, seed, tables

PLAN = "worker-fleet"
UNCLAIMED = ("queued", "cancelled", "failed")  # the states a run no worker claimed may be in
REPOS = [{"repo": "evo-agents", "branch": "feat/plan-runs"}, {"repo": "evo-agents-harness", "branch": "main"}]
DAY = 24 * 3600


def add_run(conn, ids, state: str = "queued", step: str = "3", kind: str = "step", **columns) -> int:
    """A run of ``step`` in ``state``, with what that state requires: a claimed run has a worker, a runtime and
    leased_at, a held one a lease, a final one finished_at, a failed one an error, a waiting one waiting_since and a
    parked one parked_at. A plan run (``kind`` plan) has repos instead of a step key and a repo. Columns of 0010 are
    named only when the run needs them, so the same helper writes runs at 0009."""
    now = datetime.now(timezone.utc)
    row = {
        "project_id": ids["project"],
        "plan_id": PLAN,
        "step_key": step,
        "plan_revision": 1,
        "dispatched_by": ids["user"],
        "runtime": "any",
        "mode": "headless",
        "approval": "review",
        "timeout_s": 3600,
        "repo": "evo-agents",
        "branch": "feat/worker-fleet",
        "state": state,
    }
    if state not in UNCLAIMED:
        row |= {"worker_id": ids["worker"], "runtime": "claude-code", "leased_at": now}
    if state in runs.HELD_STATES:
        row["lease_expires_at"] = now + timedelta(seconds=runs.LEASE_SECONDS)
    if state in runs.TERMINAL_STATES:
        row["finished_at"] = now
    if state == "failed":
        row["error"] = "verify command 1 exited 1"
    if state == "waiting":
        row["waiting_since"] = now
    if state == "parked":
        row["parked_at"] = now
    if kind == "plan":
        row |= {"kind": "plan", "step_key": None, "repo": None, "branch": None, "repos": Jsonb(REPOS)}
    elif kind != "step":
        row["kind"] = kind
    row |= columns
    # a dispatch asked for the runtime a queued run has, or for any one a worker then picked
    row.setdefault("requested_runtime", "any" if row.get("worker_id") is not None else row["runtime"])
    statement = sql.SQL("INSERT INTO runs ({}) VALUES ({}) RETURNING id").format(
        sql.SQL(", ").join(map(sql.Identifier, row)), sql.SQL(", ").join(sql.Placeholder() * len(row))
    )
    return conn.execute(statement, list(row.values())).fetchone()[0]


def add_worker(conn, ids, name: str, token_hash: str) -> int:
    token = one(
        conn,
        "INSERT INTO tokens (user_id, kind, token_hash, host, expires_at) "
        "VALUES (%s, 'worker', %s, %s, now() + interval '90 days') RETURNING id",
        ids["user"],
        token_hash,
        name,
    )
    worker = one(
        conn,
        "INSERT INTO workers (owner_id, token_id, name, hostname, os, arch, agent_version, slots, labels, runtimes, "
        "checkouts) VALUES (%s, %s, %s, %s, 'darwin', 'arm64', '0.3.0', 2, %s, %s, %s) RETURNING id",
        ids["user"],
        token,
        name,
        f"{name}.local",
        ["mac"],
        Jsonb({"claude-code": {"version": "2.1.289"}, "codex": {"version": "0.153.4"}}),
        Jsonb({"demo/evo-agents": {"path": "~/github/evo-agents"}}),
    )
    conn.execute("INSERT INTO worker_projects (worker_id, project_id) VALUES (%s, %s)", (worker, ids["project"]))
    return worker


def add_plan(conn, ids, plan_id: str) -> None:
    """The plan ``plan_id`` at revision 1 in the project of ``ids``, with two pending steps."""
    body = {"id": plan_id, "steps": [{"id": 1, "status": "pending"}, {"id": 2, "status": "pending"}]}
    digest = "sha256:" + "e" * 64
    conn.execute(
        "INSERT INTO plans (project_id, plan_id, area, label, body, digest, updated_by) "
        "VALUES (%s, %s, 'active', '{}', %s, %s, %s)",
        (ids["project"], plan_id, Jsonb(body), digest, ids["user"]),
    )
    conn.execute(
        "INSERT INTO plan_revisions (project_id, plan_id, revision, area, label, body, digest, actor_id) "
        "VALUES (%s, %s, 1, 'active', '{}', %s, %s, %s)",
        (ids["project"], plan_id, Jsonb(body), digest, ids["user"]),
    )


def seed_runs(conn, ids: dict | None = None) -> dict:
    """``test_migrate.seed`` (a user, a machine token, the project demo, ...) unless ``ids`` names what an earlier one
    wrote, then a plan at revision 1, two workers of the user, a spare worker token, a pairing, a queued run of step
    3 and a done run of step 2 with events and a message."""
    ids = seed(conn) if ids is None else dict(ids)
    body = {"id": PLAN, "steps": [{"id": 2, "status": "done"}, {"id": 3, "status": "pending"}]}
    digest = "sha256:" + "f" * 64
    conn.execute(
        "INSERT INTO plans (project_id, plan_id, area, label, body, digest, updated_by) "
        "VALUES (%s, %s, 'active', '{}', %s, %s, %s)",
        (ids["project"], PLAN, Jsonb(body), digest, ids["user"]),
    )
    conn.execute(
        "INSERT INTO plan_revisions (project_id, plan_id, revision, area, label, body, digest, actor_id) "
        "VALUES (%s, %s, 1, 'active', '{}', %s, %s, %s)",
        (ids["project"], PLAN, Jsonb(body), digest, ids["user"]),
    )
    ids["worker"] = add_worker(conn, ids, "mac-mini", "b" * 64)
    ids["other_worker"] = add_worker(conn, ids, "linux-box", "c" * 64)
    ids["worker_token"] = one(conn, "SELECT token_id FROM workers WHERE id = %s", ids["worker"])
    ids["spare_token"] = one(
        conn,
        "INSERT INTO tokens (user_id, kind, token_hash, host, expires_at) "
        "VALUES (%s, 'worker', %s, 'spare', now() + interval '90 days') RETURNING id",
        ids["user"],
        "d" * 64,
    )
    ids["pairing"] = one(
        conn,
        "INSERT INTO worker_pairings (code_selector, code_hash, owner_id, name, projects, expires_at) "
        "VALUES ('ABCD', %s, %s, 'laptop', %s, now() + interval '10 minutes') RETURNING id",
        "e" * 64,
        ids["user"],
        [ids["project"]],
    )
    ids["run"] = add_run(conn, ids)
    ids["done_run"] = add_run(conn, ids, "done", step="2", event_seq=2, events_acked=1)
    conn.execute(
        "INSERT INTO run_events (run_id, seq, kind, body) VALUES (%s, 1, 'agent_message_chunk', %s), "
        "(%s, 2, 'state', %s)",
        (ids["done_run"], Jsonb({"text": "hello"}), ids["done_run"], Jsonb({"from": "verifying", "to": "done"})),
    )
    conn.execute(
        "INSERT INTO run_inbox (run_id, sent_by, body) VALUES (%s, %s, 'also run ruff')", (ids["done_run"], ids["user"])
    )
    return ids


@pytest.fixture
def db(hub_db):
    """A migrated database with ``seed_runs`` in it, and a superuser connection to it, in autocommit."""
    migrate(hub_db.dsn)
    with pg.admin(hub_db.admin_dsn) as conn:
        yield conn, seed_runs(conn)


def test_0009_goes_up_with_rows_down_to_the_schema_of_0008_and_up_again(hub_db):
    move_to(hub_db, "0008")
    at_0008 = query(hub_db, SNAPSHOT)
    with pg.admin(hub_db.admin_dsn) as conn:
        machine = seed(conn)
    move_to(hub_db, "0009")
    assert tables(hub_db) >= pg.RUN_TABLES
    at_0009 = query(hub_db, SNAPSHOT)
    with pg.admin(hub_db.admin_dsn) as conn:
        ids = seed_runs(conn, machine)
        conn.execute(
            "INSERT INTO audit (actor_id, token_id, action, target) VALUES (%s, %s, 'worker.join', 'mac-mini')",
            (ids["user"], ids["worker_token"]),
        )

    # Back at 0008: the schema is the one 0008 had, which release 0.2.3 accepts; the worker tokens are gone, and the
    # audit rows written with one keep their actor.
    move_to(hub_db, "0008", down=True)
    assert query(hub_db, SNAPSHOT) == at_0008
    assert not tables(hub_db) & pg.RUN_TABLES
    assert query(hub_db, "SELECT id, kind FROM tokens ORDER BY id") == [(machine["token"], "machine")]
    trail = "SELECT action, actor_id, token_id FROM audit ORDER BY id"
    assert query(hub_db, trail) == [("memory.put", ids["user"], machine["token"]), ("worker.join", ids["user"], None)]
    assert query(hub_db, "SELECT plan_id, revision FROM plan_revisions") == [(PLAN, 1)]
    with pg.admin(hub_db.admin_dsn) as conn, pytest.raises(errors.CheckViolation):
        conn.execute(
            "INSERT INTO tokens (user_id, kind, token_hash, host, expires_at) "
            "VALUES (%s, 'worker', repeat('9', 64), 'mac', now() + interval '1 day')",
            (ids["user"],),
        )
    with pg.admin(hub_db.admin_dsn) as conn:  # the audit trail is append-only again
        with pytest.raises(errors.RestrictViolation):
            conn.execute("UPDATE audit SET target = 'edited'")

    move_to(hub_db, "0009")
    assert query(hub_db, "SELECT version_num FROM alembic_version") == [("0009",)]
    assert query(hub_db, SNAPSHOT) == at_0009
    with pg.admin(hub_db.admin_dsn) as conn:  # the plan and its revision stayed; workers and runs come back
        ids["worker"] = add_worker(conn, ids, "mac-mini", "b" * 64)
        add_run(conn, ids, "running")


@pytest.mark.parametrize("state", runs.RUN_STATES)
def test_a_step_has_at_most_one_active_run(db, state):
    conn, ids = db
    first = add_run(conn, ids, state, step="9")
    if state in runs.ACTIVE_STATES:
        with pytest.raises(errors.UniqueViolation, match="runs_active_step_key"):
            add_run(conn, ids, step="9")
    else:  # a final run leaves the step free for the next attempt or a rerun
        add_run(conn, ids, step="9", attempt=2, parent_run_id=first)
    # the index is per step of one plan: another step of the same plan always takes a run
    add_run(conn, ids, step="10")


def test_run_events_are_never_updated(db):
    conn, ids = db
    with pytest.raises(errors.RestrictViolation, match="run_events rows are never updated"):
        conn.execute("UPDATE run_events SET body = '{}' WHERE run_id = %s", (ids["done_run"],))
    with pytest.raises(errors.RestrictViolation):
        conn.execute("UPDATE run_events SET truncated = true")
    # pruning deletes them, and they go with their run
    conn.execute("DELETE FROM run_events WHERE run_id = %s AND seq = 1", (ids["done_run"],))
    assert one(conn, "SELECT count(*) FROM run_events") == 1
    conn.execute("DELETE FROM runs WHERE id = %s", (ids["done_run"],))
    assert one(conn, "SELECT count(*) FROM run_events") == one(conn, "SELECT count(*) FROM run_inbox") == 0


def test_the_tables_take_every_state_kind_runtime_mode_and_approval_of_the_runs_module(db):
    conn, ids = db
    assert query_kinds(conn) == {"step"}
    for number, state in enumerate(runs.RUN_STATES):
        add_run(conn, ids, state, step=f"state-{number}")
        add_plan(conn, ids, f"plan-{number}")
        add_run(conn, ids, state, kind="plan", plan_id=f"plan-{number}")
    assert query_kinds(conn) == set(runs.RUN_KINDS)
    for runtime in ("any", *runs.RUNTIMES):
        for mode in runs.MODES:
            for approval in runs.APPROVALS:
                step = f"{runtime}-{mode}-{approval}"
                add_run(conn, ids, step=step, runtime=runtime, mode=mode, approval=approval)
    for seq, kind in enumerate(runs.EVENT_KINDS, start=1):
        conn.execute(
            "INSERT INTO run_events (run_id, seq, kind, body) VALUES (%s, %s, %s, %s)",
            (ids["run"], seq, kind, Jsonb({"kind": kind})),
        )
    assert one(conn, "SELECT count(DISTINCT kind) FROM run_events WHERE run_id = %s", ids["run"]) == len(
        runs.EVENT_KINDS
    )
    for column, value in (
        ("state", "paused"),
        ("runtime", "gemini"),
        ("mode", "batch"),
        ("approval", "never"),
        ("kind", "chain"),
    ):
        with pytest.raises(errors.CheckViolation):
            add_run(conn, ids, step=f"bad-{column}", **{column: value})


def query_kinds(conn) -> set[str]:
    return {row[0] for row in conn.execute("SELECT DISTINCT kind FROM runs")}


EXPLAIN_CLAIM = """
EXPLAIN SELECT id FROM runs
 WHERE state = 'queued' AND project_id = ANY(%s) AND dispatched_by = %s AND runtime IN ('any', 'claude-code')
   AND (pinned_worker_id IS NULL OR pinned_worker_id = %s)
 ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED
"""
EXPLAIN_REAPER = (
    "EXPLAIN SELECT id FROM runs WHERE state IN ('leased', 'running', 'interactive', 'verifying', 'waiting') "
    "AND lease_expires_at < now() FOR UPDATE SKIP LOCKED"
)
EXPLAIN_PARK = "EXPLAIN SELECT id FROM runs WHERE state = 'waiting' AND waiting_since < now() - interval '1 day'"
EXPLAIN_EXPIRE = "EXPLAIN SELECT id FROM runs WHERE state = 'parked' AND parked_at < now() - interval '7 days'"


def test_the_claim_and_the_reaper_read_through_an_index(db):
    conn, ids = db
    for number in range(300):  # a history of finished runs the claim and the reaper must not walk through
        add_run(conn, ids, "done", step=f"old-{number}")
    conn.execute("ANALYZE runs")
    conn.execute("SET enable_seqscan = off")
    claim = "\n".join(row[0] for row in conn.execute(EXPLAIN_CLAIM, ([ids["project"]], ids["user"], ids["worker"])))
    assert "runs_claim_idx" in claim, claim
    reaper = "\n".join(row[0] for row in conn.execute(EXPLAIN_REAPER))
    assert "runs_lease_idx" in reaper, reaper
    for explain, index in ((EXPLAIN_PARK, "runs_waiting_idx"), (EXPLAIN_EXPIRE, "runs_parked_idx")):
        plan = "\n".join(row[0] for row in conn.execute(explain))
        assert index in plan, plan


@pytest.mark.parametrize(
    "statement, error",
    [
        # tokens: worker is the one new kind
        (
            "INSERT INTO tokens (user_id, kind, token_hash, host, expires_at) "
            "VALUES ({user}, 'robot', repeat('1', 64), 'pc', now() + interval '1 day')",
            errors.CheckViolation,
        ),
        (
            "INSERT INTO tokens (user_id, kind, token_hash, expires_at) "
            "VALUES ({user}, 'worker', repeat('1', 64), now() + interval '1 day')",
            errors.CheckViolation,
        ),
        # workers
        (
            "INSERT INTO workers (owner_id, token_id, name, hostname, os, arch, agent_version) "
            "VALUES ({user}, {spare_token}, 'MAC-MINI', 'spare', 'linux', 'x86_64', '0.3.0')",
            errors.UniqueViolation,
        ),
        (
            "INSERT INTO workers (owner_id, token_id, name, hostname, os, arch, agent_version) "
            "VALUES ({user}, {worker_token}, 'spare', 'spare', 'linux', 'x86_64', '0.3.0')",
            errors.UniqueViolation,
        ),
        (
            "INSERT INTO workers (owner_id, token_id, name, hostname, os, arch, agent_version) "
            "VALUES ({user}, {spare_token}, 'my mac', 'spare', 'linux', 'x86_64', '0.3.0')",
            errors.CheckViolation,
        ),
        (
            "INSERT INTO workers (owner_id, token_id, name, hostname, os, arch, agent_version) "
            "VALUES ({user}, {spare_token}, 'spare', E'spare\\nhost', 'linux', 'x86_64', '0.3.0')",
            errors.CheckViolation,
        ),
        ("UPDATE workers SET labels = ARRAY['gpu', 'two words'] WHERE id = {worker}", errors.CheckViolation),
        ("UPDATE workers SET labels = ARRAY['gpu', ''] WHERE id = {worker}", errors.CheckViolation),
        ("UPDATE workers SET labels = ARRAY['gpu', NULL] WHERE id = {worker}", errors.CheckViolation),
        ("UPDATE workers SET labels = ARRAY[['gpu']] WHERE id = {worker}", errors.CheckViolation),
        ("UPDATE workers SET labels = array_fill('l'::text, ARRAY[17]) WHERE id = {worker}", errors.CheckViolation),
        ("UPDATE workers SET runtimes = '[]' WHERE id = {worker}", errors.CheckViolation),
        ("UPDATE workers SET checkouts = 'null' WHERE id = {worker}", errors.CheckViolation),
        ("DELETE FROM workers WHERE id = {worker}", errors.ForeignKeyViolation),  # its runs keep it
        ("DELETE FROM tokens WHERE id = {worker_token}", errors.ForeignKeyViolation),
        # pairings: an unused code holds its selector, the first four characters of the code
        (
            "INSERT INTO worker_pairings (code_selector, code_hash, owner_id, name, projects, expires_at) "
            "VALUES ('ABCD', repeat('2', 64), {user}, 'again', ARRAY[{project}]::bigint[], "
            "now() + interval '5 minutes')",
            errors.UniqueViolation,
        ),
        (
            "INSERT INTO worker_pairings (code_selector, code_hash, owner_id, name, projects, expires_at) "
            "VALUES ('WXYZ', 'ABCD-EFGH', {user}, 'plain', ARRAY[{project}]::bigint[], now() + interval '5 minutes')",
            errors.CheckViolation,
        ),
        (
            "INSERT INTO worker_pairings (code_selector, code_hash, owner_id, name, projects, expires_at) "
            "VALUES ('ABCDEFGH', repeat('2', 64), {user}, 'long-selector', ARRAY[{project}]::bigint[], "
            "now() + interval '5 minutes')",
            errors.CheckViolation,
        ),
        (
            "INSERT INTO worker_pairings (code_selector, code_hash, owner_id, name, projects, expires_at) "
            "VALUES ('ABCU', repeat('2', 64), {user}, 'not-crockford', ARRAY[{project}]::bigint[], "
            "now() + interval '5 minutes')",
            errors.CheckViolation,
        ),
        (
            "INSERT INTO worker_pairings (code_selector, code_hash, owner_id, name, projects, expires_at) "
            "VALUES ('WXYZ', repeat('2', 64), {user}, 'long', ARRAY[{project}]::bigint[], "
            "now() + interval '11 minutes')",
            errors.CheckViolation,
        ),
        (
            "INSERT INTO worker_pairings (code_selector, code_hash, owner_id, name, projects, expires_at) "
            "VALUES ('WXYZ', repeat('2', 64), {user}, 'none', '{{}}', now() + interval '5 minutes')",
            errors.CheckViolation,
        ),
        (
            "INSERT INTO worker_pairings (code_selector, code_hash, owner_id, name, projects, expires_at) "
            "VALUES ('WXYZ', repeat('2', 64), {user}, 'null', ARRAY[{project}, NULL]::bigint[], "
            "now() + interval '5 minutes')",
            errors.CheckViolation,
        ),
        ("UPDATE worker_pairings SET attempts = 6 WHERE id = {pairing}", errors.CheckViolation),
        ("UPDATE worker_pairings SET used_at = now() WHERE id = {pairing}", errors.CheckViolation),
        ("UPDATE worker_pairings SET slots = 9 WHERE id = {pairing}", errors.CheckViolation),
        # runs
        ("UPDATE runs SET timeout_s = 299 WHERE id = {run}", errors.CheckViolation),
        ("UPDATE runs SET timeout_s = 14401 WHERE id = {run}", errors.CheckViolation),
        ("UPDATE runs SET attempt = 2 WHERE id = {run}", errors.CheckViolation),  # names no run it retries
        ("UPDATE runs SET attempt = 4, parent_run_id = {done_run} WHERE id = {run}", errors.CheckViolation),
        ("UPDATE runs SET max_attempts = 4 WHERE id = {run}", errors.CheckViolation),
        ("UPDATE runs SET parent_run_id = id WHERE id = {run}", errors.CheckViolation),
        (
            "UPDATE runs SET state = 'leased', runtime = 'codex', leased_at = now(), "
            "lease_expires_at = now() + interval '5 minutes' WHERE id = {run}",
            errors.CheckViolation,  # claimed by no worker
        ),
        (
            "UPDATE runs SET state = 'leased', worker_id = {worker}, leased_at = now(), "
            "lease_expires_at = now() + interval '5 minutes' WHERE id = {run}",
            errors.CheckViolation,  # still runtime any
        ),
        (
            "UPDATE runs SET state = 'running', worker_id = {worker}, runtime = 'codex', leased_at = now() "
            "WHERE id = {run}",
            errors.CheckViolation,  # held without a lease
        ),
        ("UPDATE runs SET state = 'done' WHERE id = {run}", errors.CheckViolation),  # never claimed
        ("UPDATE runs SET runtime = 'codex' WHERE id = {run}", errors.CheckViolation),  # queued: as it was asked for
        ("UPDATE runs SET requested_runtime = 'opencode' WHERE id = {done_run}", errors.CheckViolation),
        ("UPDATE runs SET requested_runtime = 'gemini' WHERE id = {run}", errors.CheckViolation),
        ("UPDATE workers SET free_slots = 9 WHERE id = {worker}", errors.CheckViolation),
        ("UPDATE runs SET finished_at = NULL WHERE id = {done_run}", errors.CheckViolation),
        ("UPDATE runs SET finished_at = now() WHERE id = {run}", errors.CheckViolation),
        ("UPDATE runs SET state = 'failed', finished_at = now() WHERE id = {run}", errors.CheckViolation),
        (
            "UPDATE runs SET pinned_worker_id = {other_worker} WHERE id = {done_run}",
            errors.CheckViolation,  # claimed by a worker other than the one it is pinned to
        ),
        ("UPDATE runs SET plan_revision = 2 WHERE id = {run}", errors.ForeignKeyViolation),
        ("UPDATE runs SET commit_sha = 'abc1234' WHERE id = {done_run}", errors.CheckViolation),
        ("UPDATE runs SET verify = '{{}}' WHERE id = {done_run}", errors.CheckViolation),
        ("UPDATE runs SET usage = '[]' WHERE id = {done_run}", errors.CheckViolation),
        ("UPDATE runs SET events_acked = 3 WHERE id = {done_run}", errors.CheckViolation),
        ("UPDATE runs SET step_key = '' WHERE id = {run}", errors.CheckViolation),
        ("UPDATE runs SET log_sha256 = 'sha256:' || repeat('a', 64) WHERE id = {done_run}", errors.CheckViolation),
        ("UPDATE runs SET title = '' WHERE id = {run}", errors.CheckViolation),
        ("UPDATE runs SET title = repeat('t', 201) WHERE id = {run}", errors.CheckViolation),
        ("UPDATE runs SET title = E'two\\nlines' WHERE id = {run}", errors.CheckViolation),
        ("UPDATE runs SET takeover_requested_at = now() WHERE id = {run}", errors.CheckViolation),  # queued
        ("UPDATE runs SET handback_requested_at = now() WHERE id = {done_run}", errors.CheckViolation),
        ("DELETE FROM plans WHERE plan_id = 'worker-fleet'", errors.ForeignKeyViolation),
        # events and inbox
        ("INSERT INTO run_events (run_id, seq, kind, body) VALUES ({run}, 1, 'stdout', '{{}}')", errors.CheckViolation),
        ("INSERT INTO run_events (run_id, seq, kind, body) VALUES ({run}, 0, 'system', '{{}}')", errors.CheckViolation),
        (
            "INSERT INTO run_events (run_id, seq, kind, body) VALUES ({done_run}, 2, 'system', '{{}}')",
            errors.UniqueViolation,
        ),
        ("INSERT INTO run_inbox (run_id, sent_by, body) VALUES ({run}, {user}, '')", errors.CheckViolation),
        (
            "INSERT INTO run_inbox (run_id, sent_by, body) VALUES ({run}, {user}, repeat('é', 4097))",
            errors.CheckViolation,  # 8194 bytes
        ),
        ("UPDATE run_inbox SET delivered_at = created_at - interval '1 second'", errors.CheckViolation),
    ],
)
def test_constraints_refuse_bad_rows(db, statement, error):
    conn, ids = db
    with pytest.raises(error):
        conn.execute(statement.format(**ids))


@pytest.mark.parametrize("slots", [0, 9])
def test_a_worker_has_one_to_eight_slots(db, slots):
    conn, ids = db
    with pytest.raises(errors.CheckViolation):
        conn.execute(
            "INSERT INTO workers (owner_id, token_id, name, hostname, os, arch, agent_version, slots) "
            "VALUES (%s, %s, 'spare', 'spare', 'linux', 'x86_64', '0.3.0', %s)",
            (ids["user"], ids["spare_token"], slots),
        )


def test_constraints_accept_good_rows(db):
    conn, ids = db
    lost = add_run(conn, ids, "running", step="4")
    values = {
        "lost": lost,
        "diffstat": json.dumps({"files": 3, "insertions": 120, "deletions": 4}),
        "verify": json.dumps([{"command": "ruff check .", "exit_code": 0}]),
        "usage": json.dumps({"input_tokens": 1200}),
        "tool_call": json.dumps({"title": "pytest"}),
        "raw": json.dumps("raw line"),
    }
    for statement in (
        # a run through its states as the worker reports them, pinned to the worker that claims it
        "UPDATE runs SET state = 'leased', worker_id = {worker}, pinned_worker_id = {worker}, runtime = 'codex', "
        "leased_at = now(), lease_expires_at = now() + interval '5 minutes' WHERE id = {run}",
        "UPDATE runs SET state = 'running', started_at = now(), session_id = '0199a3c1-0000-7000-8000-00000000000a', "
        "title = 'Log trực tiếp, inbox và kết quả', takeover_requested_at = now() WHERE id = {run}",
        "UPDATE runs SET state = 'interactive', cancel_requested_at = now(), takeover_requested_at = NULL, "
        "handback_requested_at = now() WHERE id = {run}",
        "UPDATE runs SET state = 'verifying', event_seq = 40, events_acked = 37, handback_requested_at = NULL "
        "WHERE id = {run}",
        "UPDATE runs SET state = 'review', lease_expires_at = NULL, commit_sha = repeat('a', 40), "
        "diffstat = '{diffstat}', verify = '{verify}', evidence = 'commit aaaaaaa; ruff 0', usage = '{usage}', "
        "log_sha256 = repeat('b', 64), diff_sha256 = repeat('c', 64) WHERE id = {run}",
        "UPDATE runs SET state = 'done', finished_at = now() WHERE id = {run}",
        # a lost run, the attempt that retries it, and its failure while queued, as the reaper writes them
        "UPDATE runs SET state = 'lost', finished_at = now() WHERE id = {lost}",
        "INSERT INTO runs (project_id, plan_id, step_key, plan_revision, dispatched_by, requested_runtime, runtime, "
        "mode, approval, timeout_s, attempt, parent_run_id, repo) VALUES ({project}, 'worker-fleet', '4', 1, {user}, "
        "'opencode', 'opencode', 'interactive', 'auto', 14400, 3, {lost}, 'evo-agents')",
        "UPDATE runs SET state = 'failed', finished_at = now(), error = 'the lease of the last attempt ran out' "
        "WHERE parent_run_id = {lost}",
        # events of the worker, one of them cut, and a message of 8 KiB the worker has
        "INSERT INTO run_events (run_id, seq, at, kind, body, truncated) VALUES "
        "({run}, 1, now() - interval '1 minute', 'tool_call', '{tool_call}', false), "
        "({run}, 2, now(), 'output', '{raw}', true)",
        "INSERT INTO run_inbox (run_id, sent_by, body) VALUES ({run}, {user}, repeat('é', 4096))",
        "UPDATE run_inbox SET delivered_at = now() WHERE run_id = {run}",
        # a revoked worker's name may be used again, in any case, and labels take letters, digits and ._-
        "UPDATE workers SET revoked_at = now() WHERE id = {worker}",
        "INSERT INTO workers (owner_id, token_id, name, hostname, os, arch, agent_version, slots, labels) "
        "VALUES ({user}, {spare_token}, 'MAC-MINI', 'mac-mini.local', 'darwin', 'arm64', '0.3.0', 8, "
        "ARRAY['GPU', 'mac.studio', 'x_1-2'])",
        "UPDATE workers SET labels = '{{}}', last_heartbeat_at = now(), drained_at = now() WHERE id = {other_worker}",
        # a pairing that made a worker, and another with the same code, locked by five wrong tries
        "UPDATE worker_pairings SET used_at = now(), worker_id = {other_worker} WHERE id = {pairing}",
        "INSERT INTO worker_pairings (code_selector, code_hash, owner_id, name, projects, slots, labels, "
        "allow_web_terminal, attempts, expires_at) VALUES ('ABCD', repeat('e', 64), {user}, 'desk', "
        "ARRAY[{project}]::bigint[], 4, ARRAY['linux'], true, 5, now() + interval '10 minutes')",
    ):
        conn.execute(statement.format(**ids, **values))
    states = "SELECT step_key, attempt, state FROM runs ORDER BY id"
    assert conn.execute(states).fetchall() == [
        ("3", 1, "done"),
        ("2", 1, "done"),
        ("4", 1, "lost"),
        ("4", 3, "failed"),
    ]
    assert one(conn, "SELECT count(*) FROM workers WHERE owner_id = %s AND revoked_at IS NULL", ids["user"]) == 2


# Schema 0010: plan runs, decisions and notifications

OPTIONS = [
    {"key": "a", "label": "Keep SQLite", "recommended": True},
    {"key": "b", "label": "Move to Postgres", "description": "Needs a container in the dev compose."},
]


def add_decision(conn, ids, run: int, **columns) -> int:
    """An open decision of the plan run ``run`` of the plan ``PLAN``, unless ``columns`` say otherwise."""
    row = {
        "run_id": run,
        "project_id": ids["project"],
        "plan_id": PLAN,
        "category": "architecture",
        "question": "Which database should the dashboard read?",
        "context": "## Why\nThe plan leaves it open.",
        "options": Jsonb(OPTIONS),
    }
    row |= columns
    statement = sql.SQL("INSERT INTO decisions ({}) VALUES ({}) RETURNING id").format(
        sql.SQL(", ").join(map(sql.Identifier, row)), sql.SQL(", ").join(sql.Placeholder() * len(row))
    )
    return conn.execute(statement, list(row.values())).fetchone()[0]


@pytest.fixture
def plan_db(db):
    """``db`` and what 0010 adds: the plan rollout with a running plan run of it, its open decision, the owner's
    notification of that decision, a Telegram channel of the owner, and a delivery of the notification to it and one to
    the web."""
    conn, ids = db
    add_plan(conn, ids, "rollout")
    ids["plan_run"] = add_run(conn, ids, "running", kind="plan", plan_id="rollout", timeout_s=DAY)
    ids["decision"] = add_decision(conn, ids, ids["plan_run"], plan_id="rollout", step_key="2")
    ids["notification"] = one(
        conn,
        "INSERT INTO notifications (user_id, kind, project_id, run_id, decision_id, title, link) "
        "VALUES (%s, 'decision', %s, %s, %s, 'Run #4 asks which database the dashboard reads', "
        "'/projects/demo/decisions/1') RETURNING id",
        ids["user"],
        ids["project"],
        ids["plan_run"],
        ids["decision"],
    )
    ids["channel"] = one(
        conn,
        "INSERT INTO notification_channels (user_id, kind, config) VALUES (%s, 'telegram', %s) RETURNING id",
        ids["user"],
        Jsonb({"chat_id": 42}),
    )
    ids["delivery"] = one(
        conn,
        "INSERT INTO notification_deliveries (notification_id, channel_id) VALUES (%s, %s) RETURNING id",
        ids["notification"],
        ids["channel"],
    )
    ids["web_delivery"] = one(
        conn, "INSERT INTO notification_deliveries (notification_id) VALUES (%s) RETURNING id", ids["notification"]
    )
    return conn, ids


def test_a_plan_run_has_no_step_key_and_a_run_of_one_step_needs_one(plan_db):
    conn, ids = plan_db
    row = conn.execute("SELECT kind, step_key, repo, branch, repos FROM runs WHERE id = %s", (ids["plan_run"],))
    assert row.fetchone() == ("plan", None, None, None, REPOS)
    with pytest.raises(errors.CheckViolation, match="runs_kind_step_key_check"):
        add_run(conn, ids, kind="plan", step_key="3")
    with pytest.raises(errors.CheckViolation, match="runs_kind_step_key_check"):
        add_run(conn, ids, step=None)
    with pytest.raises(errors.CheckViolation, match="runs_kind_step_key_check"):
        conn.execute("UPDATE runs SET step_key = NULL WHERE id = %s", (ids["run"],))
    # a run of one step has its repo and no repos, a plan run the other way round
    with pytest.raises(errors.CheckViolation, match="runs_kind_repo_check"):
        add_run(conn, ids, step="5", repo=None)
    with pytest.raises(errors.CheckViolation, match="runs_kind_repos_check"):
        add_run(conn, ids, step="5", repos=Jsonb(REPOS))
    with pytest.raises(errors.CheckViolation, match="runs_kind_repo_check"):
        add_run(conn, ids, kind="plan", repo="evo-agents")
    with pytest.raises(errors.CheckViolation, match="runs_kind_repos_check"):
        add_run(conn, ids, kind="plan", repos=None)
    # a run written as 0.3.0 writes it, without a kind, is a run of one step
    legacy = add_run(conn, ids, step="5")
    assert one(conn, "SELECT kind FROM runs WHERE id = %s", legacy) == "step"


@pytest.mark.parametrize("state", runs.RUN_STATES)
def test_a_plan_has_at_most_one_active_plan_run(db, state):
    conn, ids = db
    first = add_run(conn, ids, state, kind="plan")
    if state in runs.ACTIVE_STATES:  # parked included: answering it queues the run that resumes it
        with pytest.raises(errors.UniqueViolation, match="runs_active_plan_key"):
            add_run(conn, ids, kind="plan")
    else:  # a final plan run leaves the plan free for the next attempt or a new plan run
        add_run(conn, ids, kind="plan", attempt=2, parent_run_id=first)
    # the index is per plan: another plan takes a plan run of its own. A run of one step of the same plan is the
    # api's to refuse, under its lock of the plan, not the index's.
    add_plan(conn, ids, "other")
    add_run(conn, ids, kind="plan", plan_id="other")
    add_run(conn, ids, step="9")


def test_a_timeout_of_a_day_is_for_plan_runs_only(db):
    conn, ids = db
    add_run(conn, ids, kind="plan", timeout_s=DAY)
    add_plan(conn, ids, "short")
    add_run(conn, ids, kind="plan", plan_id="short", timeout_s=300)
    add_run(conn, ids, step="5", timeout_s=14400)
    add_plan(conn, ids, "refused")
    for kind, timeout in (("step", DAY), ("step", 14401), ("step", 299), ("plan", DAY + 1), ("plan", 299)):
        with pytest.raises(errors.CheckViolation, match="runs_timeout_s_check"):
            add_run(conn, ids, step="6", kind=kind, plan_id="refused", timeout_s=timeout)
    with pytest.raises(errors.CheckViolation, match="runs_timeout_s_check"):
        conn.execute("UPDATE runs SET timeout_s = %s WHERE id = %s", (DAY, ids["run"]))


def test_0010_goes_up_with_runs_down_to_the_schema_of_0009_and_up_again(hub_db):
    move_to(hub_db, "0009")
    at_0009 = query(hub_db, SNAPSHOT)
    with pg.admin(hub_db.admin_dsn) as conn:
        ids = seed_runs(conn)
        held = add_run(conn, ids, "running", step="4")
    move_to(hub_db, "0010")
    assert tables(hub_db) >= pg.NOTIFICATION_TABLES
    # the runs of 0009 are runs of one step, with their step key and repo, and no agent time counted yet
    assert query(hub_db, "SELECT id, kind, step_key, repo, repos, run_seconds, model FROM runs ORDER BY id") == [
        (ids["run"], "step", "3", "evo-agents", None, 0, None),
        (ids["done_run"], "step", "2", "evo-agents", None, 0, None),
        (held, "step", "4", "evo-agents", None, 0, None),
    ]
    at_0010 = query(hub_db, SNAPSHOT)

    with pg.admin(hub_db.admin_dsn) as conn:
        add_plan(conn, ids, "rollout")
        lost = add_run(conn, ids, "lost", kind="plan", plan_id="rollout", timeout_s=DAY)
        parked = add_run(conn, ids, "done", kind="plan", plan_id="rollout", attempt=2, parent_run_id=lost)
        conn.execute("UPDATE runs SET parked_at = now(), session_id = 'session-1' WHERE id = %s", (parked,))
        resumed = add_run(conn, ids, "waiting", kind="plan", plan_id="rollout", resume_of_run_id=parked, model="opus")
        decision = add_decision(conn, ids, resumed, plan_id="rollout", step_key="2")
        notification = one(
            conn,
            "INSERT INTO notifications (user_id, kind, project_id, run_id, decision_id, title) "
            "VALUES (%s, 'decision', %s, %s, %s, 'A decision waits') RETURNING id",
            ids["user"],
            ids["project"],
            resumed,
            decision,
        )
        conn.execute("INSERT INTO notification_deliveries (notification_id) VALUES (%s)", (notification,))
        conn.execute(
            "INSERT INTO run_events (run_id, seq, kind, body) VALUES (%s, 1, 'system', %s)", (resumed, Jsonb({"x": 1}))
        )
        conn.execute("INSERT INTO run_inbox (run_id, sent_by, body) VALUES (%s, %s, 'B')", (resumed, ids["user"]))
        # runs of one step that only the protocol of 0010 could leave waiting or parked
        waiting = add_run(conn, ids, "waiting", step="6")
        parked_step = add_run(conn, ids, "parked", step="7")

    # Back at 0009: the schema is the one 0009 had, which release 0.3.0 runs on. The plan runs went, with their events,
    # messages, decisions and notifications; the runs of one step stayed, in states 0.3.0 knows.
    move_to(hub_db, "0009", down=True)
    assert query(hub_db, SNAPSHOT) == at_0009
    assert not tables(hub_db) & pg.NOTIFICATION_TABLES
    assert query(hub_db, "SELECT id, step_key, state, finished_at IS NOT NULL FROM runs ORDER BY id") == [
        (ids["run"], "3", "queued", False),
        (ids["done_run"], "2", "done", True),
        (held, "4", "running", False),
        (waiting, "6", "running", False),
        (parked_step, "7", "cancelled", True),
    ]
    assert query(hub_db, "SELECT DISTINCT run_id FROM run_events") == [(ids["done_run"],)]
    assert query(hub_db, "SELECT run_id FROM run_inbox") == [(ids["done_run"],)]
    assert query(hub_db, "SELECT plan_id FROM plans ORDER BY plan_id") == [("rollout",), (PLAN,)]

    result = migrate(hub_db.dsn)
    assert (result.before, result.applied, result.after) == (("0009",), ("0010",), ("0010",))
    assert query(hub_db, SNAPSHOT) == at_0010
    with pg.admin(hub_db.admin_dsn) as conn:  # the plan stayed, and takes a plan run again
        add_run(conn, ids, "running", kind="plan", plan_id="rollout")


VALUES_0010 = {
    "options_one": json.dumps(OPTIONS[:1]),
    "options_seven": json.dumps([{"key": f"k{n}", "label": f"Option {n}"} for n in range(7)]),
    "options_bad_key": json.dumps([{"key": "two words", "label": "A"}, {"key": "b", "label": "B"}]),
    "options_no_label": json.dumps([{"key": "a"}, {"key": "b", "label": "B"}]),
    "options_two_lines": json.dumps([{"key": "a", "label": "A\nB"}, {"key": "b", "label": "B"}]),
    "options_flag": json.dumps([{"key": "a", "label": "A", "recommended": "yes"}, {"key": "b", "label": "B"}]),
    "options_two_flags": json.dumps([{**option, "recommended": True} for option in OPTIONS]),
    "options_object": json.dumps({"a": "A", "b": "B"}),
    "repos_empty": "[]",
    "repos_object": json.dumps({"repo": "evo-agents"}),
    "repos_no_name": json.dumps([{"branch": "main"}]),
    "repos_blank": json.dumps([{"repo": ""}]),
    "repos_number": json.dumps([{"repo": "evo-agents", "branch": 1}]),
    "repos_many": json.dumps([{"repo": f"repo-{n}"} for n in range(51)]),
}


@pytest.mark.parametrize(
    "statement, error",
    [
        # runs
        ("UPDATE runs SET kind = 'plan' WHERE id = {run}", errors.CheckViolation),  # a step key and no repos
        ("UPDATE runs SET repos = '{repos_empty}' WHERE id = {plan_run}", errors.CheckViolation),
        ("UPDATE runs SET repos = '{repos_object}' WHERE id = {plan_run}", errors.CheckViolation),
        ("UPDATE runs SET repos = '{repos_no_name}' WHERE id = {plan_run}", errors.CheckViolation),
        ("UPDATE runs SET repos = '{repos_blank}' WHERE id = {plan_run}", errors.CheckViolation),
        ("UPDATE runs SET repos = '{repos_number}' WHERE id = {plan_run}", errors.CheckViolation),
        ("UPDATE runs SET repos = '{repos_many}' WHERE id = {plan_run}", errors.CheckViolation),
        ("UPDATE runs SET model = '' WHERE id = {plan_run}", errors.CheckViolation),
        ("UPDATE runs SET model = E'opus\\nfast' WHERE id = {plan_run}", errors.CheckViolation),
        ("UPDATE runs SET run_seconds = -1 WHERE id = {plan_run}", errors.CheckViolation),
        ("UPDATE runs SET state = 'waiting' WHERE id = {plan_run}", errors.CheckViolation),  # since when?
        (
            "UPDATE runs SET state = 'waiting', waiting_since = now(), lease_expires_at = NULL WHERE id = {plan_run}",
            errors.CheckViolation,  # waiting is held: it keeps its lease
        ),
        ("UPDATE runs SET state = 'parked', lease_expires_at = NULL WHERE id = {plan_run}", errors.CheckViolation),
        ("UPDATE runs SET resume_of_run_id = {plan_run} WHERE id = {plan_run}", errors.CheckViolation),
        ("UPDATE runs SET resume_of_run_id = {plan_run} WHERE id = {run}", errors.CheckViolation),  # of one step
        ("UPDATE runs SET resume_of_run_id = 999999 WHERE id = {plan_run}", errors.ForeignKeyViolation),
        # decisions
        ("UPDATE decisions SET category = 'refactor' WHERE id = {decision}", errors.CheckViolation),
        ("UPDATE decisions SET question = '' WHERE id = {decision}", errors.CheckViolation),
        ("UPDATE decisions SET context = repeat('é', 8193) WHERE id = {decision}", errors.CheckViolation),
        ("UPDATE decisions SET step_key = '' WHERE id = {decision}", errors.CheckViolation),
        ("UPDATE decisions SET options = '{options_one}' WHERE id = {decision}", errors.CheckViolation),
        ("UPDATE decisions SET options = '{options_seven}' WHERE id = {decision}", errors.CheckViolation),
        ("UPDATE decisions SET options = '{options_bad_key}' WHERE id = {decision}", errors.CheckViolation),
        ("UPDATE decisions SET options = '{options_no_label}' WHERE id = {decision}", errors.CheckViolation),
        ("UPDATE decisions SET options = '{options_two_lines}' WHERE id = {decision}", errors.CheckViolation),
        ("UPDATE decisions SET options = '{options_flag}' WHERE id = {decision}", errors.CheckViolation),
        ("UPDATE decisions SET options = '{options_two_flags}' WHERE id = {decision}", errors.CheckViolation),
        ("UPDATE decisions SET options = '{options_object}' WHERE id = {decision}", errors.CheckViolation),
        ("UPDATE decisions SET state = 'pending' WHERE id = {decision}", errors.CheckViolation),
        ("UPDATE decisions SET answer_text = 'B, and keep a backup' WHERE id = {decision}", errors.CheckViolation),
        (
            "UPDATE decisions SET state = 'answered', answered_at = now(), answered_by = {user} WHERE id = {decision}",
            errors.CheckViolation,  # answered with nothing
        ),
        (
            "UPDATE decisions SET state = 'answered', answered_at = now(), answered_by = {user}, answer_option = 'c' "
            "WHERE id = {decision}",
            errors.CheckViolation,  # not an option of the decision
        ),
        (
            "UPDATE decisions SET state = 'answered', answer_option = 'b', answered_by = {user} WHERE id = {decision}",
            errors.CheckViolation,  # when?
        ),
        (
            "UPDATE decisions SET state = 'answered', answered_at = asked_at - interval '1 second', "
            "answered_by = {user}, answer_option = 'b' WHERE id = {decision}",
            errors.CheckViolation,
        ),
        (
            "UPDATE decisions SET state = 'answered', answered_at = now(), answered_by = {user}, "
            "answer_text = repeat('x', 4097) WHERE id = {decision}",
            errors.CheckViolation,
        ),
        ("UPDATE decisions SET delivered_at = now() WHERE id = {decision}", errors.CheckViolation),  # no answer yet
        ("UPDATE decisions SET run_id = 999999 WHERE id = {decision}", errors.ForeignKeyViolation),
        # notifications
        ("UPDATE notifications SET kind = 'mail' WHERE id = {notification}", errors.CheckViolation),
        ("UPDATE notifications SET decision_id = NULL WHERE id = {notification}", errors.CheckViolation),
        (
            "UPDATE notifications SET notice_kind = 'push_default_branch' WHERE id = {notification}",
            errors.CheckViolation,
        ),
        (
            "UPDATE notifications SET kind = 'notice', decision_id = NULL, notice_kind = 'deploy' "
            "WHERE id = {notification}",
            errors.CheckViolation,
        ),
        ("UPDATE notifications SET title = E'two\\nlines' WHERE id = {notification}", errors.CheckViolation),
        ("UPDATE notifications SET title = '' WHERE id = {notification}", errors.CheckViolation),
        ("UPDATE notifications SET link = 'https://example.org/x' WHERE id = {notification}", errors.CheckViolation),
        ("UPDATE notifications SET link = '//example.org/x' WHERE id = {notification}", errors.CheckViolation),
        ("UPDATE notifications SET details = '[]' WHERE id = {notification}", errors.CheckViolation),
        ("UPDATE notifications SET body = '' WHERE id = {notification}", errors.CheckViolation),
        (
            "UPDATE notifications SET read_at = created_at - interval '1 second' WHERE id = {notification}",
            errors.CheckViolation,
        ),
        (
            "INSERT INTO notifications (user_id, kind, decision_id, title) "
            "VALUES ({user}, 'decision', {decision}, 'Again')",
            errors.UniqueViolation,  # a decision notifies a member once
        ),
        # channels and deliveries
        ("INSERT INTO notification_channels (user_id, kind) VALUES ({user}, 'web')", errors.CheckViolation),
        ("INSERT INTO notification_channels (user_id, kind) VALUES ({user}, 'Tele gram')", errors.CheckViolation),
        ("INSERT INTO notification_channels (user_id, kind) VALUES ({user}, 'telegram')", errors.UniqueViolation),
        ("UPDATE notification_channels SET config = '[]' WHERE id = {channel}", errors.CheckViolation),
        ("UPDATE notification_deliveries SET state = 'sent' WHERE id = {delivery}", errors.CheckViolation),
        ("UPDATE notification_deliveries SET attempts = 6 WHERE id = {delivery}", errors.CheckViolation),
        ("UPDATE notification_deliveries SET state = 'failed' WHERE id = {delivery}", errors.CheckViolation),
        ("UPDATE notification_deliveries SET state = 'delivered' WHERE id = {delivery}", errors.CheckViolation),
        ("UPDATE notification_deliveries SET last_error = '' WHERE id = {delivery}", errors.CheckViolation),
        (
            "INSERT INTO notification_deliveries (notification_id) VALUES ({notification})",
            errors.UniqueViolation,  # the web once
        ),
        (
            "INSERT INTO notification_deliveries (notification_id, channel_id) VALUES ({notification}, {channel})",
            errors.UniqueViolation,
        ),
    ],
)
def test_constraints_of_0010_refuse_bad_rows(plan_db, statement, error):
    conn, ids = plan_db
    with pytest.raises(error):
        conn.execute(statement.format(**ids, **VALUES_0010))


def test_constraints_of_0010_accept_good_rows(plan_db):
    conn, ids = plan_db
    values = {
        "details": json.dumps({"repo": "evo-agents", "branch": "main", "commits": ["a" * 40]}),
        "repos": json.dumps([{"repo": "evo-agents"}, {"repo": "web", "branch": None}, {"repo": "x", "branch": "y"}]),
    }
    for statement in (
        # the plan run through waiting and parked, as the worker and the reaper move it
        "UPDATE runs SET state = 'waiting', waiting_since = now(), run_seconds = 3600, model = 'claude-opus-4-1', "
        "session_id = '0199a3c1-0000-7000-8000-00000000000b' WHERE id = {plan_run}",
        "UPDATE runs SET state = 'running', waiting_since = NULL WHERE id = {plan_run}",
        "UPDATE runs SET state = 'waiting', waiting_since = now(), repos = '{repos}' WHERE id = {plan_run}",
        "UPDATE runs SET state = 'parked', parked_at = now(), lease_expires_at = NULL, waiting_since = NULL "
        "WHERE id = {plan_run}",
        # the owner answers with an option and text of their own: the parked run is done, and the run that resumes it
        # is queued on the same worker
        "UPDATE decisions SET state = 'answered', answered_at = now(), answered_by = {user}, answer_option = 'b', "
        "answer_text = 'And keep the SQLite file as a backup.' WHERE id = {decision}",
        "UPDATE runs SET state = 'done', finished_at = now() WHERE id = {plan_run}",
        "INSERT INTO runs (project_id, plan_id, kind, plan_revision, dispatched_by, pinned_worker_id, "
        "requested_runtime, runtime, mode, approval, timeout_s, repos, resume_of_run_id, run_seconds, session_id) "
        "SELECT project_id, plan_id, kind, plan_revision, dispatched_by, worker_id, runtime, runtime, mode, approval, "
        "timeout_s, repos, id, run_seconds, session_id FROM runs WHERE id = {plan_run}",
        "UPDATE decisions SET delivered_at = now() WHERE id = {decision}",
        "UPDATE notifications SET read_at = now() WHERE id = {notification}",
        # the deliveries: the web at once, Telegram after a failed try
        "UPDATE notification_deliveries SET state = 'delivered', delivered_at = now() WHERE id = {web_delivery}",
        "UPDATE notification_deliveries SET attempts = 1, next_at = now() + interval '2 minutes', "
        "last_error = 'telegram answered 429' WHERE id = {delivery}",
        "UPDATE notification_deliveries SET state = 'failed', attempts = 5 WHERE id = {delivery}",
        # a notice of a push to a default branch, read later, on the web only
        "INSERT INTO notifications (user_id, kind, notice_kind, project_id, run_id, title, body, details, link) "
        "VALUES ({user}, 'notice', 'push_default_branch', {project}, {plan_run}, 'Pushed main of evo-agents', "
        "'2 commits', '{details}', '/')",
        "INSERT INTO notification_channels (user_id, kind, enabled) VALUES ({user}, 'mail', false)",
        # a decision without a step or context, open, and one the reaper let expire
        "INSERT INTO decisions (run_id, project_id, plan_id, category, question, options) "
        'SELECT id, project_id, plan_id, \'deploy\', \'Deploy to staging now?\', \'[{{"key": "yes", "label": '
        '"Deploy"}}, {{"key": "no", "label": "Wait"}}]\' FROM runs WHERE resume_of_run_id = {plan_run}',
        "UPDATE decisions SET state = 'expired' WHERE step_key IS NULL",
    ):
        conn.execute(statement.format(**ids, **values))
    rows = (
        "SELECT kind, state, resume_of_run_id, session_id IS NOT NULL FROM runs WHERE plan_id = 'rollout' ORDER BY id"
    )
    assert conn.execute(rows).fetchall() == [("plan", "done", None, True), ("plan", "queued", ids["plan_run"], True)]
    assert one(conn, "SELECT count(*) FROM notifications WHERE user_id = %s AND read_at IS NULL", ids["user"]) == 1
    # a run, its decisions and their notifications go together
    conn.execute("DELETE FROM runs WHERE resume_of_run_id = %s", (ids["plan_run"],))
    conn.execute("DELETE FROM runs WHERE id = %s", (ids["plan_run"],))
    for table in ("decisions", "notifications", "notification_deliveries"):
        assert one(conn, f"SELECT count(*) FROM {table}") == 0, table

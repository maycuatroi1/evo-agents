"""Schema 0009: the workers, pairings, runs, run events and inbox of ``docs/workers.md``, in Postgres.

The checks step 3 of the worker-fleet plan names: a database with rows in it goes up to 0009, down to 0008 with the
schema 0008 had and without the worker tokens, and up again; a second active run of one step is refused by the
partial unique index; an UPDATE of run_events is refused by the trigger. Two processes migrating at once are
``tests.hub.test_migrate``'s, which apply every revision, 0009 included. Around them: the states, event kinds,
runtimes, modes and approvals of the tables are those of ``evo_agents.hub.runs``, the claim and the reaper find
their rows through an index, and the constraints refuse rows the protocol rules out."""

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


def add_run(conn, ids, state: str = "queued", step: str = "3", **columns) -> int:
    """A run of ``step`` in ``state``, with what that state requires: a claimed run has a worker, a runtime and
    leased_at, a held one a lease, a final one finished_at, a failed one an error."""
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
    row |= columns
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

    result = migrate(hub_db.dsn)
    assert (result.before, result.applied, result.after) == (("0008",), ("0009",), ("0009",))
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
    for number, state in enumerate(runs.RUN_STATES):
        add_run(conn, ids, state, step=f"state-{number}")
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
    ):
        with pytest.raises(errors.CheckViolation):
            add_run(conn, ids, step=f"bad-{column}", **{column: value})


EXPLAIN_CLAIM = """
EXPLAIN SELECT id FROM runs
 WHERE state = 'queued' AND project_id = ANY(%s) AND dispatched_by = %s AND runtime IN ('any', 'claude-code')
   AND (pinned_worker_id IS NULL OR pinned_worker_id = %s)
 ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED
"""
EXPLAIN_REAPER = (
    "EXPLAIN SELECT id FROM runs WHERE state IN ('leased', 'running', 'interactive', 'verifying') "
    "AND lease_expires_at < now() FOR UPDATE SKIP LOCKED"
)


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
        "UPDATE runs SET state = 'running', started_at = now(), session_id = '0199a3c1-0000-7000-8000-00000000000a' "
        "WHERE id = {run}",
        "UPDATE runs SET state = 'interactive', cancel_requested_at = now() WHERE id = {run}",
        "UPDATE runs SET state = 'verifying', event_seq = 40, events_acked = 37 WHERE id = {run}",
        "UPDATE runs SET state = 'review', lease_expires_at = NULL, commit_sha = repeat('a', 40), "
        "diffstat = '{diffstat}', verify = '{verify}', evidence = 'commit aaaaaaa; ruff 0', usage = '{usage}', "
        "log_sha256 = repeat('b', 64), diff_sha256 = repeat('c', 64) WHERE id = {run}",
        "UPDATE runs SET state = 'done', finished_at = now() WHERE id = {run}",
        # a lost run, the attempt that retries it, and its failure while queued, as the reaper writes them
        "UPDATE runs SET state = 'lost', finished_at = now() WHERE id = {lost}",
        "INSERT INTO runs (project_id, plan_id, step_key, plan_revision, dispatched_by, runtime, mode, approval, "
        "timeout_s, attempt, parent_run_id, repo) VALUES ({project}, 'worker-fleet', '4', 1, {user}, 'opencode', "
        "'interactive', 'auto', 14400, 3, {lost}, 'evo-agents')",
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

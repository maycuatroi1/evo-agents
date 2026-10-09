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

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from psycopg import errors
from sqlalchemy import JSON, BigInteger, any_, delete, distinct, func, insert, literal, null, or_, select, update
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import ClauseElement, Executable

from evo_agents.hub import runs, tables
from evo_agents.hub.migrate import migrate
from tests.hub import live
from tests.hub.test_migrate import ALEMBIC_VERSION, SNAPSHOT, move_to, one, query, seed
from tests.hub.test_migrate import tables as table_names

PLAN = "worker-fleet"
UNCLAIMED = ("queued", "cancelled", "failed")  # the states a run no worker claimed may be in
REPOS = [{"repo": "evo-agents", "branch": "feat/plan-runs"}, {"repo": "evo-agents-harness", "branch": "main"}]
DAY = 24 * 3600


def later(**delta):
    """``now()`` plus ``delta``, as the database reads its clock when the statement runs."""
    return func.now() + timedelta(**delta)


def new_id(conn, table, **values) -> int:
    """Insert one row of ``table`` and return its id."""
    return one(conn, insert(table).values(values).returning(table.c.id))


def count(table, *where):
    """The statement that counts the rows of ``table``, or those of them that match ``where``."""
    return select(func.count()).select_from(table).where(*where)


def add_run(conn, ids, state: str = "queued", step: str = "3", kind: str = "step", **columns) -> int:
    """A run of ``step`` in ``state``, with what that state requires: a claimed run has a worker, a runtime and
    leased_at, a held one a lease, a final one finished_at, a failed one an error, a waiting one waiting_since and a
    parked one parked_at. A plan run (``kind`` plan) has repos instead of a step key and a repo. Columns of 0010 are
    named only when the run needs them, so the same helper writes runs at 0009."""
    now = datetime.now(UTC)
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
        row |= {"kind": "plan", "step_key": None, "repo": None, "branch": None, "repos": REPOS}
    elif kind == "review":  # on no plan, queued by a schedule (0014)
        row |= {"kind": "review", "plan_id": None, "plan_revision": None, "step_key": None, "repo": None}
        row |= {"branch": None, "repos": REPOS, "dispatched_via": "schedule", "schedule_night": now.date()}
    elif kind == "judge":  # the Judge of a change of the Curator's plan, queued by a schedule (0017)
        row |= {"kind": "judge", "step_key": None, "repo": None, "branch": None, "repos": REPOS}
        row |= {"dispatched_via": "schedule", "schedule_night": now.date()}
    elif kind != "step":
        row["kind"] = kind
    row |= columns
    # a dispatch asked for the runtime a queued run has, or for any one a worker then picked
    row.setdefault("requested_runtime", "any" if row.get("worker_id") is not None else row["runtime"])
    # None is SQL NULL, in the JSONB column repos too, where SQLAlchemy would write the JSON null
    return new_id(conn, tables.runs, **{name: null() if value is None else value for name, value in row.items()})


def add_worker(conn, ids, name: str, token_hash: str) -> int:
    token = new_id(
        conn,
        tables.tokens,
        user_id=ids["user"],
        kind="worker",
        token_hash=token_hash,
        host=name,
        expires_at=later(days=90),
    )
    worker = new_id(
        conn,
        tables.workers,
        owner_id=ids["user"],
        token_id=token,
        name=name,
        hostname=f"{name}.local",
        os="darwin",
        arch="arm64",
        agent_version="0.3.0",
        slots=2,
        labels=["mac"],
        runtimes={"claude-code": {"version": "2.1.289"}, "codex": {"version": "0.153.4"}},
        checkouts={"demo/evo-agents": {"path": "~/github/evo-agents"}},
    )
    conn.execute(insert(tables.worker_projects).values(worker_id=worker, project_id=ids["project"]))
    return worker


def add_plan(conn, ids, plan_id: str, steps: list | None = None, digest: str = "sha256:" + "e" * 64) -> None:
    """The plan ``plan_id`` at revision 1 in the project of ``ids``, with ``steps``: two pending ones unless named."""
    steps = steps or [{"id": 1, "status": "pending"}, {"id": 2, "status": "pending"}]
    row = {"project_id": ids["project"], "plan_id": plan_id, "area": "active", "label": {}, "digest": digest}
    body = {"id": plan_id, "steps": steps}
    conn.execute(insert(tables.plans).values(**row, body=body, updated_by=ids["user"]))
    conn.execute(insert(tables.plan_revisions).values(**row, revision=1, body=body, actor_id=ids["user"]))


def seed_runs(conn, ids: dict | None = None) -> dict:
    """``test_migrate.seed`` (a user, a machine token, the project demo, ...) unless ``ids`` names what an earlier one
    wrote, then a plan at revision 1, two workers of the user, a spare worker token, a pairing, a queued run of step
    3 and a done run of step 2 with events and a message."""
    ids = seed(conn) if ids is None else dict(ids)
    steps = [{"id": 2, "status": "done"}, {"id": 3, "status": "pending"}]
    add_plan(conn, ids, PLAN, steps, digest="sha256:" + "f" * 64)
    ids["worker"] = add_worker(conn, ids, "mac-mini", "b" * 64)
    ids["other_worker"] = add_worker(conn, ids, "linux-box", "c" * 64)
    ids["worker_token"] = one(conn, select(tables.workers.c.token_id).where(tables.workers.c.id == ids["worker"]))
    ids["spare_token"] = new_id(
        conn,
        tables.tokens,
        user_id=ids["user"],
        kind="worker",
        token_hash="d" * 64,
        host="spare",
        expires_at=later(days=90),
    )
    ids["pairing"] = new_id(
        conn,
        tables.worker_pairings,
        code_selector="ABCD",
        code_hash="e" * 64,
        owner_id=ids["user"],
        name="laptop",
        projects=[ids["project"]],
        expires_at=later(minutes=10),
    )
    ids["run"] = add_run(conn, ids)
    ids["done_run"] = add_run(conn, ids, "done", step="2", event_seq=2, events_acked=1)
    conn.execute(
        insert(tables.run_events).values(
            [
                {"run_id": ids["done_run"], "seq": 1, "kind": "agent_message_chunk", "body": {"text": "hello"}},
                {"run_id": ids["done_run"], "seq": 2, "kind": "state", "body": {"from": "verifying", "to": "done"}},
            ]
        )
    )
    conn.execute(insert(tables.run_inbox).values(run_id=ids["done_run"], sent_by=ids["user"], body="also run ruff"))
    return ids


@pytest.fixture
def db(hub_db):
    """A migrated database with ``seed_runs`` in it, and a connection to it as its owner, in autocommit."""
    migrate(hub_db.dsn)
    with live.connect(hub_db) as conn:
        yield conn, seed_runs(conn)


@pytest.mark.empty_db
def test_0009_goes_up_with_rows_down_to_the_schema_of_0008_and_up_again(hub_db):
    move_to(hub_db, "0008")
    at_0008 = query(hub_db, SNAPSHOT)
    with live.connect(hub_db) as conn:
        machine = seed(conn)
    move_to(hub_db, "0009")
    assert table_names(hub_db) >= pg.RUN_TABLES
    at_0009 = query(hub_db, SNAPSHOT)
    with live.connect(hub_db) as conn:
        ids = seed_runs(conn, machine)
        conn.execute(
            insert(tables.audit).values(
                actor_id=ids["user"], token_id=ids["worker_token"], action="worker.join", target="mac-mini"
            )
        )

    # Back at 0008: the schema is the one 0008 had, which release 0.2.3 accepts; the worker tokens are gone, and the
    # audit rows written with one keep their actor.
    move_to(hub_db, "0008", down=True)
    assert query(hub_db, SNAPSHOT) == at_0008
    assert not table_names(hub_db) & pg.RUN_TABLES
    tokens, audit, revisions = tables.tokens, tables.audit, tables.plan_revisions
    assert query(hub_db, select(tokens.c.id, tokens.c.kind).order_by(tokens.c.id)) == [(machine["token"], "machine")]
    trail = select(audit.c.action, audit.c.actor_id, audit.c.token_id).order_by(audit.c.id)
    assert query(hub_db, trail) == [("memory.put", ids["user"], machine["token"]), ("worker.join", ids["user"], None)]
    assert query(hub_db, select(revisions.c.plan_id, revisions.c.revision)) == [(PLAN, 1)]
    with live.connect(hub_db) as conn, pytest.raises(errors.CheckViolation):
        conn.execute(
            insert(tokens).values(
                user_id=ids["user"], kind="worker", token_hash="9" * 64, host="mac", expires_at=later(days=1)
            )
        )
    with live.connect(hub_db) as conn:  # the audit trail is append-only again
        with pytest.raises(errors.RestrictViolation):
            conn.execute(update(audit).values(target="edited"))

    move_to(hub_db, "0009")
    assert query(hub_db, select(ALEMBIC_VERSION.c.version_num)) == [("0009",)]
    assert query(hub_db, SNAPSHOT) == at_0009
    with live.connect(hub_db) as conn:  # the plan and its revision stayed; workers and runs come back
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
    events = tables.run_events
    with pytest.raises(errors.RestrictViolation, match="run_events rows are never updated"):
        conn.execute(update(events).values(body={}).where(events.c.run_id == ids["done_run"]))
    with pytest.raises(errors.RestrictViolation):
        conn.execute(update(events).values(truncated=True))
    # pruning deletes them, and they go with their run
    conn.execute(delete(events).where(events.c.run_id == ids["done_run"], events.c.seq == 1))
    assert one(conn, count(events)) == 1
    conn.execute(delete(tables.runs).where(tables.runs.c.id == ids["done_run"]))
    assert one(conn, count(events)) == one(conn, count(tables.run_inbox)) == 0


def test_the_tables_take_every_state_kind_runtime_mode_and_approval_of_the_runs_module(db):
    conn, ids = db
    assert query_kinds(conn) == {"step"}
    for number, state in enumerate(runs.RUN_STATES):
        add_run(conn, ids, state, step=f"state-{number}")
        add_plan(conn, ids, f"plan-{number}")
        add_run(conn, ids, state, kind="plan", plan_id=f"plan-{number}")
    schedule = new_id(
        conn,
        tables.schedules,
        project_id=ids["project"],
        kind="night_shift",
        owner_id=ids["user"],
        worker_id=ids["worker"],
    )
    for state in ("queued", *runs.TERMINAL_STATES):  # a project has one review run active at a time
        add_run(conn, ids, state, kind="review", schedule_id=schedule)
    for state in ("queued", *runs.TERMINAL_STATES):  # and one judge run (0017)
        add_run(conn, ids, state, kind="judge", schedule_id=schedule)
    assert query_kinds(conn) == set(runs.RUN_KINDS)
    for runtime in ("any", *runs.RUNTIMES):
        for mode in runs.MODES:
            for approval in runs.APPROVALS:
                step = f"{runtime}-{mode}-{approval}"
                add_run(conn, ids, step=step, runtime=runtime, mode=mode, approval=approval)
    events = tables.run_events
    for seq, kind in enumerate(runs.EVENT_KINDS, start=1):
        conn.execute(insert(events).values(run_id=ids["run"], seq=seq, kind=kind, body={"kind": kind}))
    kinds = select(func.count(distinct(events.c.kind))).where(events.c.run_id == ids["run"])
    assert one(conn, kinds) == len(runs.EVENT_KINDS)
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
    return set(conn.execute(select(tables.runs.c.kind).distinct()).scalars())


class Explain(Executable, ClauseElement):
    """The plan Postgres picks for a Core statement, a line of it per row. SQLAlchemy has no construct for EXPLAIN:
    this one puts the keyword before the statement compiled as usual."""

    inherit_cache = False

    def __init__(self, statement):
        self.statement = statement


@compiles(Explain)
def _explain(element, compiler, **kw):
    return "EXPLAIN " + compiler.process(element.statement, **kw)


class Analyze(Executable, ClauseElement):
    """Statistics of a table's rows for the planner. SQLAlchemy has no construct for ANALYZE: this one puts the
    keyword before the quoted name of the table."""

    inherit_cache = False

    def __init__(self, table):
        self.table = table


@compiles(Analyze)
def _analyze(element, compiler, **kw):
    return "ANALYZE " + compiler.preparer.format_table(element.table)


def explained(conn, statement) -> str:
    return "\n".join(conn.execute(Explain(statement)).scalars())


def test_the_claim_and_the_reaper_read_through_an_index(db):
    conn, ids = db
    for number in range(300):  # a history of finished runs the claim and the reaper must not walk through
        add_run(conn, ids, "done", step=f"old-{number}")
    conn.execute(Analyze(tables.runs))
    conn.execute(select(func.set_config("enable_seqscan", "off", False)))  # for the session, as SET does
    run = tables.runs
    claim = (
        select(run.c.id)
        .where(
            run.c.state == "queued",
            run.c.project_id == any_(literal([ids["project"]], ARRAY(BigInteger))),
            run.c.dispatched_by == ids["user"],
            run.c.runtime.in_(["any", "claude-code"]),
            or_(run.c.pinned_worker_id.is_(None), run.c.pinned_worker_id == ids["worker"]),
        )
        .order_by(run.c.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    plan = explained(conn, claim)
    assert "runs_claim_idx" in plan, plan
    held = ("leased", "running", "interactive", "verifying", "waiting")
    reaper = select(run.c.id).where(run.c.state.in_(held), run.c.lease_expires_at < func.now())
    plan = explained(conn, reaper.with_for_update(skip_locked=True))
    assert "runs_lease_idx" in plan, plan
    park = select(run.c.id).where(run.c.state == "waiting", run.c.waiting_since < func.now() - timedelta(days=1))
    expire = select(run.c.id).where(run.c.state == "parked", run.c.parked_at < func.now() - timedelta(days=7))
    for statement, index in ((park, "runs_waiting_idx"), (expire, "runs_parked_idx")):
        plan = explained(conn, statement)
        assert index in plan, plan


# The rows of the constraint tests are written before the fixture has run: a Ref stands for the id of a row it
# wrote, and each case is a function of the fixture's ids that returns the statement.


@dataclass(frozen=True)
class Ref:
    """The id ``ids[key]`` of a row the fixture wrote."""

    key: str


USER = Ref("user")
PROJECT = Ref("project")
WORKER = Ref("worker")
OTHER_WORKER = Ref("other_worker")
WORKER_TOKEN = Ref("worker_token")
SPARE_TOKEN = Ref("spare_token")
PAIRING = Ref("pairing")
RUN = Ref("run")
DONE_RUN = Ref("done_run")
PLAN_RUN = Ref("plan_run")
DECISION = Ref("decision")
NOTIFICATION = Ref("notification")
CHANNEL = Ref("channel")
DELIVERY = Ref("delivery")


def resolved(value, ids):
    """``value`` with each Ref in it, alone or in a list, replaced by the id it stands for."""
    if isinstance(value, Ref):
        return ids[value.key]
    if isinstance(value, list):
        return [resolved(item, ids) for item in value]
    return value


def insert_of(table, **values):
    """The INSERT of a row of ``table`` with ``values``."""
    return lambda ids: insert(table).values({name: resolved(value, ids) for name, value in values.items()})


def update_of(table, row: Ref, **values):
    """The UPDATE that sets ``values`` on the row ``row`` of ``table``."""
    return lambda ids: (
        update(table)
        .values({name: resolved(value, ids) for name, value in values.items()})
        .where(table.c.id == ids[row.key])
    )


def delete_of(table, row: Ref):
    """The DELETE of the row ``row`` of ``table``."""
    return lambda ids: delete(table).where(table.c.id == ids[row.key])


def spare_worker(**values):
    """The INSERT of a worker on the spare token, with ``values`` instead of its defaults."""
    row = {"owner_id": USER, "token_id": SPARE_TOKEN, "name": "spare", "hostname": "spare", "os": "linux"}
    return insert_of(tables.workers, **{**row, "arch": "x86_64", "agent_version": "0.3.0", **values})


def pairing(selector: str, name: str, **values):
    """The INSERT of a pairing of the user for the project, its code hashed, for five minutes unless ``values`` say
    otherwise."""
    row = {"code_selector": selector, "code_hash": "2" * 64, "owner_id": USER, "name": name, "projects": [PROJECT]}
    return insert_of(tables.worker_pairings, **{**row, "expires_at": later(minutes=5), **values})


@pytest.mark.parametrize(
    "statement, error",
    [
        # tokens: worker is the one new kind
        pytest.param(
            insert_of(
                tables.tokens, user_id=USER, kind="robot", token_hash="1" * 64, host="pc", expires_at=later(days=1)
            ),
            errors.CheckViolation,
            id="token-kind-robot",
        ),
        pytest.param(
            insert_of(tables.tokens, user_id=USER, kind="worker", token_hash="1" * 64, expires_at=later(days=1)),
            errors.CheckViolation,
            id="worker-token-without-host",
        ),
        # workers
        pytest.param(spare_worker(name="MAC-MINI"), errors.UniqueViolation, id="worker-name-taken-in-other-case"),
        pytest.param(spare_worker(token_id=WORKER_TOKEN), errors.UniqueViolation, id="worker-token-taken"),
        pytest.param(spare_worker(name="my mac"), errors.CheckViolation, id="worker-name-with-space"),
        pytest.param(spare_worker(hostname="spare\nhost"), errors.CheckViolation, id="worker-hostname-two-lines"),
        pytest.param(
            update_of(tables.workers, WORKER, labels=["gpu", "two words"]), errors.CheckViolation, id="label-two-words"
        ),
        pytest.param(update_of(tables.workers, WORKER, labels=["gpu", ""]), errors.CheckViolation, id="label-empty"),
        pytest.param(update_of(tables.workers, WORKER, labels=["gpu", None]), errors.CheckViolation, id="label-null"),
        pytest.param(update_of(tables.workers, WORKER, labels=[["gpu"]]), errors.CheckViolation, id="labels-2-dims"),
        pytest.param(update_of(tables.workers, WORKER, labels=["l"] * 17), errors.CheckViolation, id="labels-17"),
        pytest.param(update_of(tables.workers, WORKER, runtimes=[]), errors.CheckViolation, id="runtimes-array"),
        pytest.param(
            update_of(tables.workers, WORKER, checkouts=JSON.NULL), errors.CheckViolation, id="checkouts-json-null"
        ),
        pytest.param(delete_of(tables.workers, WORKER), errors.ForeignKeyViolation, id="worker-with-runs-deleted"),
        pytest.param(delete_of(tables.tokens, WORKER_TOKEN), errors.ForeignKeyViolation, id="worker-token-deleted"),
        # pairings: an unused code holds its selector, the first four characters of the code
        pytest.param(pairing("ABCD", "again"), errors.UniqueViolation, id="pairing-selector-taken"),
        pytest.param(pairing("WXYZ", "plain", code_hash="ABCD-EFGH"), errors.CheckViolation, id="pairing-code-plain"),
        pytest.param(pairing("ABCDEFGH", "long-selector"), errors.CheckViolation, id="pairing-selector-long"),
        pytest.param(pairing("ABCU", "not-crockford"), errors.CheckViolation, id="pairing-selector-not-crockford"),
        pytest.param(
            pairing("WXYZ", "long", expires_at=later(minutes=11)), errors.CheckViolation, id="pairing-for-11-minutes"
        ),
        pytest.param(pairing("WXYZ", "none", projects=[]), errors.CheckViolation, id="pairing-without-projects"),
        pytest.param(
            pairing("WXYZ", "null", projects=[PROJECT, None]), errors.CheckViolation, id="pairing-project-null"
        ),
        pytest.param(update_of(tables.worker_pairings, PAIRING, attempts=6), errors.CheckViolation, id="attempts-6"),
        pytest.param(
            update_of(tables.worker_pairings, PAIRING, used_at=func.now()),
            errors.CheckViolation,
            id="pairing-used-without-worker",
        ),
        pytest.param(update_of(tables.worker_pairings, PAIRING, slots=9), errors.CheckViolation, id="pairing-slots-9"),
        # runs
        pytest.param(update_of(tables.runs, RUN, timeout_s=299), errors.CheckViolation, id="timeout-299"),
        pytest.param(update_of(tables.runs, RUN, timeout_s=14401), errors.CheckViolation, id="timeout-14401"),
        pytest.param(
            update_of(tables.runs, RUN, attempt=2), errors.CheckViolation, id="attempt-2-retrying-no-run"
        ),  # names no run it retries
        pytest.param(
            update_of(tables.runs, RUN, attempt=4, parent_run_id=DONE_RUN), errors.CheckViolation, id="attempt-4"
        ),
        pytest.param(update_of(tables.runs, RUN, max_attempts=4), errors.CheckViolation, id="max-attempts-4"),
        pytest.param(
            update_of(tables.runs, RUN, parent_run_id=tables.runs.c.id), errors.CheckViolation, id="parent-itself"
        ),
        pytest.param(
            update_of(
                tables.runs,
                RUN,
                state="leased",
                runtime="codex",
                leased_at=func.now(),
                lease_expires_at=later(minutes=5),
            ),
            errors.CheckViolation,  # claimed by no worker
            id="leased-without-worker",
        ),
        pytest.param(
            update_of(
                tables.runs,
                RUN,
                state="leased",
                worker_id=WORKER,
                leased_at=func.now(),
                lease_expires_at=later(minutes=5),
            ),
            errors.CheckViolation,  # still runtime any
            id="leased-runtime-any",
        ),
        pytest.param(
            update_of(tables.runs, RUN, state="running", worker_id=WORKER, runtime="codex", leased_at=func.now()),
            errors.CheckViolation,  # held without a lease
            id="running-without-lease",
        ),
        pytest.param(
            update_of(tables.runs, RUN, state="done"), errors.CheckViolation, id="done-never-claimed"
        ),  # never claimed
        pytest.param(
            update_of(tables.runs, RUN, runtime="codex"), errors.CheckViolation, id="queued-runtime-changed"
        ),  # queued: as it was asked for
        pytest.param(
            update_of(tables.runs, DONE_RUN, requested_runtime="opencode"),
            errors.CheckViolation,
            id="claimed-requested-runtime-changed",
        ),
        pytest.param(
            update_of(tables.runs, RUN, requested_runtime="gemini"), errors.CheckViolation, id="requested-gemini"
        ),
        pytest.param(update_of(tables.workers, WORKER, free_slots=9), errors.CheckViolation, id="free-slots-9"),
        pytest.param(
            update_of(tables.runs, DONE_RUN, finished_at=None), errors.CheckViolation, id="done-without-finished-at"
        ),
        pytest.param(update_of(tables.runs, RUN, finished_at=func.now()), errors.CheckViolation, id="queued-finished"),
        pytest.param(
            update_of(tables.runs, RUN, state="failed", finished_at=func.now()),
            errors.CheckViolation,
            id="failed-without-error",
        ),
        pytest.param(
            update_of(tables.runs, DONE_RUN, pinned_worker_id=OTHER_WORKER),
            errors.CheckViolation,  # claimed by a worker other than the one it is pinned to
            id="pinned-to-other-worker",
        ),
        pytest.param(
            update_of(tables.runs, RUN, plan_revision=2), errors.ForeignKeyViolation, id="plan-revision-missing"
        ),
        pytest.param(
            update_of(tables.runs, DONE_RUN, commit_sha="abc1234"), errors.CheckViolation, id="commit-sha-short"
        ),
        pytest.param(update_of(tables.runs, DONE_RUN, verify={}), errors.CheckViolation, id="verify-object"),
        pytest.param(update_of(tables.runs, DONE_RUN, usage=[]), errors.CheckViolation, id="usage-array"),
        pytest.param(
            update_of(tables.runs, DONE_RUN, events_acked=3), errors.CheckViolation, id="events-acked-past-seq"
        ),
        pytest.param(update_of(tables.runs, RUN, step_key=""), errors.CheckViolation, id="step-key-empty"),
        pytest.param(
            update_of(tables.runs, DONE_RUN, log_sha256="sha256:" + "a" * 64),
            errors.CheckViolation,
            id="log-sha256-prefixed",
        ),
        pytest.param(update_of(tables.runs, RUN, title=""), errors.CheckViolation, id="title-empty"),
        pytest.param(update_of(tables.runs, RUN, title="t" * 201), errors.CheckViolation, id="title-201"),
        pytest.param(update_of(tables.runs, RUN, title="two\nlines"), errors.CheckViolation, id="title-two-lines"),
        pytest.param(
            update_of(tables.runs, RUN, takeover_requested_at=func.now()), errors.CheckViolation, id="takeover-queued"
        ),  # queued
        pytest.param(
            update_of(tables.runs, DONE_RUN, handback_requested_at=func.now()),
            errors.CheckViolation,
            id="handback-done",
        ),
        pytest.param(
            lambda ids: delete(tables.plans).where(tables.plans.c.plan_id == PLAN),
            errors.ForeignKeyViolation,
            id="plan-with-runs-deleted",
        ),
        # events and inbox
        pytest.param(
            insert_of(tables.run_events, run_id=RUN, seq=1, kind="stdout", body={}),
            errors.CheckViolation,
            id="event-kind-stdout",
        ),
        pytest.param(
            insert_of(tables.run_events, run_id=RUN, seq=0, kind="system", body={}),
            errors.CheckViolation,
            id="event-seq-0",
        ),
        pytest.param(
            insert_of(tables.run_events, run_id=DONE_RUN, seq=2, kind="system", body={}),
            errors.UniqueViolation,
            id="event-seq-taken",
        ),
        pytest.param(
            insert_of(tables.run_inbox, run_id=RUN, sent_by=USER, body=""), errors.CheckViolation, id="message-empty"
        ),
        pytest.param(
            insert_of(tables.run_inbox, run_id=RUN, sent_by=USER, body="é" * 4097),
            errors.CheckViolation,  # 8194 bytes
            id="message-8194-bytes",
        ),
        pytest.param(
            lambda ids: update(tables.run_inbox).values(
                delivered_at=tables.run_inbox.c.created_at - timedelta(seconds=1)
            ),
            errors.CheckViolation,
            id="message-delivered-before-sent",
        ),
    ],
)
def test_constraints_refuse_bad_rows(db, statement, error):
    conn, ids = db
    refused = statement(ids)
    with pytest.raises(error):
        conn.execute(refused)


@pytest.mark.parametrize("slots", [0, 9])
def test_a_worker_has_one_to_eight_slots(db, slots):
    conn, ids = db
    refused = spare_worker(slots=slots)(ids)
    with pytest.raises(errors.CheckViolation):
        conn.execute(refused)


def test_constraints_accept_good_rows(db):
    conn, ids = db
    lost = add_run(conn, ids, "running", step="4")
    run, events, inbox, workers, pairings = (
        tables.runs,
        tables.run_events,
        tables.run_inbox,
        tables.workers,
        tables.worker_pairings,
    )
    the_run = run.c.id == ids["run"]
    for statement in (
        # a run through its states as the worker reports them, pinned to the worker that claims it
        update(run)
        .values(
            state="leased",
            worker_id=ids["worker"],
            pinned_worker_id=ids["worker"],
            runtime="codex",
            leased_at=func.now(),
            lease_expires_at=later(minutes=5),
        )
        .where(the_run),
        update(run)
        .values(
            state="running",
            started_at=func.now(),
            session_id="0199a3c1-0000-7000-8000-00000000000a",
            title="Log trực tiếp, inbox và kết quả",
            takeover_requested_at=func.now(),
        )
        .where(the_run),
        update(run)
        .values(
            state="interactive",
            cancel_requested_at=func.now(),
            takeover_requested_at=None,
            handback_requested_at=func.now(),
        )
        .where(the_run),
        update(run).values(state="verifying", event_seq=40, events_acked=37, handback_requested_at=None).where(the_run),
        update(run)
        .values(
            state="review",
            lease_expires_at=None,
            commit_sha="a" * 40,
            diffstat={"files": 3, "insertions": 120, "deletions": 4},
            verify=[{"command": "ruff check .", "exit_code": 0}],
            evidence="commit aaaaaaa; ruff 0",
            usage={"input_tokens": 1200},
            log_sha256="b" * 64,
            diff_sha256="c" * 64,
        )
        .where(the_run),
        update(run).values(state="done", finished_at=func.now()).where(the_run),
        # a lost run, the attempt that retries it, and its failure while queued, as the reaper writes them
        update(run).values(state="lost", finished_at=func.now()).where(run.c.id == lost),
        insert(run).values(
            project_id=ids["project"],
            plan_id="worker-fleet",
            step_key="4",
            plan_revision=1,
            dispatched_by=ids["user"],
            requested_runtime="opencode",
            runtime="opencode",
            mode="interactive",
            approval="auto",
            timeout_s=14400,
            attempt=3,
            parent_run_id=lost,
            repo="evo-agents",
        ),
        update(run)
        .values(state="failed", finished_at=func.now(), error="the lease of the last attempt ran out")
        .where(run.c.parent_run_id == lost),
        # events of the worker, one of them cut, and a message of 8 KiB the worker has
        insert(events).values(
            [
                {
                    "run_id": ids["run"],
                    "seq": 1,
                    "at": func.now() - timedelta(minutes=1),
                    "kind": "tool_call",
                    "body": {"title": "pytest"},
                    "truncated": False,
                },
                {
                    "run_id": ids["run"],
                    "seq": 2,
                    "at": func.now(),
                    "kind": "output",
                    "body": "raw line",
                    "truncated": True,
                },
            ]
        ),
        insert(inbox).values(run_id=ids["run"], sent_by=ids["user"], body="é" * 4096),
        update(inbox).values(delivered_at=func.now()).where(inbox.c.run_id == ids["run"]),
        # a revoked worker's name may be used again, in any case, and labels take letters, digits and ._-
        update(workers).values(revoked_at=func.now()).where(workers.c.id == ids["worker"]),
        insert(workers).values(
            owner_id=ids["user"],
            token_id=ids["spare_token"],
            name="MAC-MINI",
            hostname="mac-mini.local",
            os="darwin",
            arch="arm64",
            agent_version="0.3.0",
            slots=8,
            labels=["GPU", "mac.studio", "x_1-2"],
        ),
        update(workers)
        .values(labels=[], last_heartbeat_at=func.now(), drained_at=func.now())
        .where(workers.c.id == ids["other_worker"]),
        # a pairing that made a worker, and another with the same code, locked by five wrong tries
        update(pairings)
        .values(used_at=func.now(), worker_id=ids["other_worker"])
        .where(pairings.c.id == ids["pairing"]),
        insert(pairings).values(
            code_selector="ABCD",
            code_hash="e" * 64,
            owner_id=ids["user"],
            name="desk",
            projects=[ids["project"]],
            slots=4,
            labels=["linux"],
            allow_web_terminal=True,
            attempts=5,
            expires_at=later(minutes=10),
        ),
    ):
        conn.execute(statement)
    states = select(run.c.step_key, run.c.attempt, run.c.state).order_by(run.c.id)
    assert conn.execute(states).all() == [
        ("3", 1, "done"),
        ("2", 1, "done"),
        ("4", 1, "lost"),
        ("4", 3, "failed"),
    ]
    assert one(conn, count(workers, workers.c.owner_id == ids["user"], workers.c.revoked_at.is_(None))) == 2


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
        "options": OPTIONS,
    }
    row |= columns
    return new_id(conn, tables.decisions, **row)


@pytest.fixture
def plan_db(db):
    """``db`` and what 0010 adds: the plan rollout with a running plan run of it, its open decision, the owner's
    notification of that decision, a Telegram channel of the owner, and a delivery of the notification to it and one to
    the web."""
    conn, ids = db
    add_plan(conn, ids, "rollout")
    ids["plan_run"] = add_run(conn, ids, "running", kind="plan", plan_id="rollout", timeout_s=DAY)
    ids["decision"] = add_decision(conn, ids, ids["plan_run"], plan_id="rollout", step_key="2")
    ids["notification"] = new_id(
        conn,
        tables.notifications,
        user_id=ids["user"],
        kind="decision",
        project_id=ids["project"],
        run_id=ids["plan_run"],
        decision_id=ids["decision"],
        title="Run #4 asks which database the dashboard reads",
        link="/projects/demo/decisions/1",
    )
    ids["channel"] = new_id(
        conn, tables.notification_channels, user_id=ids["user"], kind="telegram", config={"chat_id": 42}
    )
    deliveries = tables.notification_deliveries
    ids["delivery"] = new_id(conn, deliveries, notification_id=ids["notification"], channel_id=ids["channel"])
    ids["web_delivery"] = new_id(conn, deliveries, notification_id=ids["notification"])
    return conn, ids


def test_a_plan_run_has_no_step_key_and_a_run_of_one_step_needs_one(plan_db):
    conn, ids = plan_db
    run = tables.runs
    row = conn.execute(
        select(run.c.kind, run.c.step_key, run.c.repo, run.c.branch, run.c.repos).where(run.c.id == ids["plan_run"])
    )
    assert row.one() == ("plan", None, None, None, REPOS)
    with pytest.raises(errors.CheckViolation, match="runs_kind_step_key_check"):
        add_run(conn, ids, kind="plan", step_key="3")
    with pytest.raises(errors.CheckViolation, match="runs_kind_step_key_check"):
        add_run(conn, ids, step=None)
    with pytest.raises(errors.CheckViolation, match="runs_kind_step_key_check"):
        conn.execute(update(run).values(step_key=None).where(run.c.id == ids["run"]))
    # a run of one step has its repo and no repos, a plan run the other way round
    with pytest.raises(errors.CheckViolation, match="runs_kind_repo_check"):
        add_run(conn, ids, step="5", repo=None)
    with pytest.raises(errors.CheckViolation, match="runs_kind_repos_check"):
        add_run(conn, ids, step="5", repos=REPOS)
    with pytest.raises(errors.CheckViolation, match="runs_kind_repo_check"):
        add_run(conn, ids, kind="plan", repo="evo-agents")
    with pytest.raises(errors.CheckViolation, match="runs_kind_repos_check"):
        add_run(conn, ids, kind="plan", repos=None)
    # a run written as 0.3.0 writes it, without a kind, is a run of one step
    unmarked = add_run(conn, ids, step="5")
    assert one(conn, select(run.c.kind).where(run.c.id == unmarked)) == "step"


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
        conn.execute(update(tables.runs).values(timeout_s=DAY).where(tables.runs.c.id == ids["run"]))


@pytest.mark.empty_db
def test_0010_goes_up_with_runs_down_to_the_schema_of_0009_and_up_again(hub_db):
    move_to(hub_db, "0009")
    at_0009 = query(hub_db, SNAPSHOT)
    with live.connect(hub_db) as conn:
        ids = seed_runs(conn)
        held = add_run(conn, ids, "running", step="4")
    move_to(hub_db, "0010")
    assert table_names(hub_db) >= pg.NOTIFICATION_TABLES
    # the runs of 0009 are runs of one step, with their step key and repo, and no agent time counted yet
    run = tables.runs
    listed = select(run.c.id, run.c.kind, run.c.step_key, run.c.repo, run.c.repos, run.c.run_seconds, run.c.model)
    assert query(hub_db, listed.order_by(run.c.id)) == [
        (ids["run"], "step", "3", "evo-agents", None, 0, None),
        (ids["done_run"], "step", "2", "evo-agents", None, 0, None),
        (held, "step", "4", "evo-agents", None, 0, None),
    ]
    at_0010 = query(hub_db, SNAPSHOT)

    with live.connect(hub_db) as conn:
        add_plan(conn, ids, "rollout")
        lost = add_run(conn, ids, "lost", kind="plan", plan_id="rollout", timeout_s=DAY)
        parked = add_run(conn, ids, "done", kind="plan", plan_id="rollout", attempt=2, parent_run_id=lost)
        conn.execute(update(run).values(parked_at=func.now(), session_id="session-1").where(run.c.id == parked))
        resumed = add_run(conn, ids, "waiting", kind="plan", plan_id="rollout", resume_of_run_id=parked, model="opus")
        decision = add_decision(conn, ids, resumed, plan_id="rollout", step_key="2")
        notification = new_id(
            conn,
            tables.notifications,
            user_id=ids["user"],
            kind="decision",
            project_id=ids["project"],
            run_id=resumed,
            decision_id=decision,
            title="A decision waits",
        )
        conn.execute(insert(tables.notification_deliveries).values(notification_id=notification))
        conn.execute(insert(tables.run_events).values(run_id=resumed, seq=1, kind="system", body={"x": 1}))
        conn.execute(insert(tables.run_inbox).values(run_id=resumed, sent_by=ids["user"], body="B"))
        # runs of one step that only the protocol of 0010 could leave waiting or parked
        waiting = add_run(conn, ids, "waiting", step="6")
        parked_step = add_run(conn, ids, "parked", step="7")

    # Back at 0009: the schema is the one 0009 had, which release 0.3.0 runs on. The plan runs went, with their events,
    # messages, decisions and notifications; the runs of one step stayed, in states 0.3.0 knows.
    move_to(hub_db, "0009", down=True)
    assert query(hub_db, SNAPSHOT) == at_0009
    assert not table_names(hub_db) & pg.NOTIFICATION_TABLES
    finished = select(run.c.id, run.c.step_key, run.c.state, run.c.finished_at.is_not(None)).order_by(run.c.id)
    assert query(hub_db, finished) == [
        (ids["run"], "3", "queued", False),
        (ids["done_run"], "2", "done", True),
        (held, "4", "running", False),
        (waiting, "6", "running", False),
        (parked_step, "7", "cancelled", True),
    ]
    assert query(hub_db, select(tables.run_events.c.run_id).distinct()) == [(ids["done_run"],)]
    assert query(hub_db, select(tables.run_inbox.c.run_id)) == [(ids["done_run"],)]
    plans = tables.plans
    assert query(hub_db, select(plans.c.plan_id).order_by(plans.c.plan_id)) == [("rollout",), (PLAN,)]

    move_to(hub_db, "0010")  # not to the head, which later revisions move on
    assert query(hub_db, SNAPSHOT) == at_0010
    with live.connect(hub_db) as conn:  # the plan stayed, and takes a plan run again
        add_run(conn, ids, "running", kind="plan", plan_id="rollout")


def options_of(options):
    """The UPDATE of the open decision's options to ``options``."""
    return update_of(tables.decisions, DECISION, options=options)


@pytest.mark.parametrize(
    "statement, error",
    [
        # runs
        pytest.param(
            update_of(tables.runs, RUN, kind="plan"), errors.CheckViolation, id="kind-plan-with-step-key"
        ),  # a step key and no repos
        pytest.param(update_of(tables.runs, PLAN_RUN, repos=[]), errors.CheckViolation, id="repos-empty"),
        pytest.param(
            update_of(tables.runs, PLAN_RUN, repos={"repo": "evo-agents"}), errors.CheckViolation, id="repos-object"
        ),
        pytest.param(
            update_of(tables.runs, PLAN_RUN, repos=[{"branch": "main"}]), errors.CheckViolation, id="repo-without-name"
        ),
        pytest.param(update_of(tables.runs, PLAN_RUN, repos=[{"repo": ""}]), errors.CheckViolation, id="repo-blank"),
        pytest.param(
            update_of(tables.runs, PLAN_RUN, repos=[{"repo": "evo-agents", "branch": 1}]),
            errors.CheckViolation,
            id="repo-branch-number",
        ),
        pytest.param(
            update_of(tables.runs, PLAN_RUN, repos=[{"repo": f"repo-{n}"} for n in range(51)]),
            errors.CheckViolation,
            id="repos-51",
        ),
        pytest.param(update_of(tables.runs, PLAN_RUN, model=""), errors.CheckViolation, id="model-empty"),
        pytest.param(update_of(tables.runs, PLAN_RUN, model="opus\nfast"), errors.CheckViolation, id="model-two-lines"),
        pytest.param(
            update_of(tables.runs, PLAN_RUN, run_seconds=-1), errors.CheckViolation, id="run-seconds-negative"
        ),
        pytest.param(
            update_of(tables.runs, PLAN_RUN, state="waiting"), errors.CheckViolation, id="waiting-since-when"
        ),  # since when?
        pytest.param(
            update_of(tables.runs, PLAN_RUN, state="waiting", waiting_since=func.now(), lease_expires_at=None),
            errors.CheckViolation,  # waiting is held: it keeps its lease
            id="waiting-without-lease",
        ),
        pytest.param(
            update_of(tables.runs, PLAN_RUN, state="parked", lease_expires_at=None),
            errors.CheckViolation,
            id="parked-since-when",
        ),
        pytest.param(
            update_of(tables.runs, PLAN_RUN, resume_of_run_id=PLAN_RUN), errors.CheckViolation, id="resumes-itself"
        ),
        pytest.param(
            update_of(tables.runs, RUN, resume_of_run_id=PLAN_RUN), errors.CheckViolation, id="step-run-resumes"
        ),  # of one step
        pytest.param(
            update_of(tables.runs, PLAN_RUN, resume_of_run_id=999999),
            errors.ForeignKeyViolation,
            id="resumes-missing-run",
        ),
        # decisions
        pytest.param(
            update_of(tables.decisions, DECISION, category="refactor"), errors.CheckViolation, id="category-refactor"
        ),
        pytest.param(update_of(tables.decisions, DECISION, question=""), errors.CheckViolation, id="question-empty"),
        pytest.param(
            update_of(tables.decisions, DECISION, context="é" * 8193), errors.CheckViolation, id="context-too-long"
        ),
        pytest.param(
            update_of(tables.decisions, DECISION, step_key=""), errors.CheckViolation, id="decision-step-key-empty"
        ),
        pytest.param(options_of(OPTIONS[:1]), errors.CheckViolation, id="one-option"),
        pytest.param(
            options_of([{"key": f"k{n}", "label": f"Option {n}"} for n in range(7)]),
            errors.CheckViolation,
            id="seven-options",
        ),
        pytest.param(
            options_of([{"key": "two words", "label": "A"}, {"key": "b", "label": "B"}]),
            errors.CheckViolation,
            id="option-key-two-words",
        ),
        pytest.param(
            options_of([{"key": "a"}, {"key": "b", "label": "B"}]),
            errors.CheckViolation,
            id="option-without-label",
        ),
        pytest.param(
            options_of([{"key": "a", "label": "A\nB"}, {"key": "b", "label": "B"}]),
            errors.CheckViolation,
            id="option-label-two-lines",
        ),
        pytest.param(
            options_of([{"key": "a", "label": "A", "recommended": "yes"}, {"key": "b", "label": "B"}]),
            errors.CheckViolation,
            id="option-recommended-string",
        ),
        pytest.param(
            options_of([{**option, "recommended": True} for option in OPTIONS]),
            errors.CheckViolation,
            id="two-recommended",
        ),
        pytest.param(options_of({"a": "A", "b": "B"}), errors.CheckViolation, id="options-object"),
        pytest.param(update_of(tables.decisions, DECISION, state="pending"), errors.CheckViolation, id="state-pending"),
        pytest.param(
            update_of(tables.decisions, DECISION, answer_text="B, and keep a backup"),
            errors.CheckViolation,
            id="answer-while-open",
        ),
        pytest.param(
            update_of(tables.decisions, DECISION, state="answered", answered_at=func.now(), answered_by=USER),
            errors.CheckViolation,  # answered with nothing
            id="answered-with-nothing",
        ),
        pytest.param(
            update_of(
                tables.decisions,
                DECISION,
                state="answered",
                answered_at=func.now(),
                answered_by=USER,
                answer_option="c",
            ),
            errors.CheckViolation,  # not an option of the decision
            id="answer-not-an-option",
        ),
        pytest.param(
            update_of(tables.decisions, DECISION, state="answered", answer_option="b", answered_by=USER),
            errors.CheckViolation,  # when?
            id="answered-when",
        ),
        pytest.param(
            update_of(
                tables.decisions,
                DECISION,
                state="answered",
                answered_at=tables.decisions.c.asked_at - timedelta(seconds=1),
                answered_by=USER,
                answer_option="b",
            ),
            errors.CheckViolation,
            id="answered-before-asked",
        ),
        pytest.param(
            update_of(
                tables.decisions,
                DECISION,
                state="answered",
                answered_at=func.now(),
                answered_by=USER,
                answer_text="x" * 4097,
            ),
            errors.CheckViolation,
            id="answer-too-long",
        ),
        pytest.param(
            update_of(tables.decisions, DECISION, delivered_at=func.now()),
            errors.CheckViolation,  # no answer yet
            id="delivered-unanswered",
        ),
        pytest.param(
            update_of(tables.decisions, DECISION, run_id=999999), errors.ForeignKeyViolation, id="decision-run-missing"
        ),
        # notifications
        pytest.param(
            update_of(tables.notifications, NOTIFICATION, kind="mail"), errors.CheckViolation, id="notification-mail"
        ),
        pytest.param(
            update_of(tables.notifications, NOTIFICATION, decision_id=None),
            errors.CheckViolation,
            id="decision-notification-without-decision",
        ),
        pytest.param(
            update_of(tables.notifications, NOTIFICATION, notice_kind="push_default_branch"),
            errors.CheckViolation,
            id="decision-notification-notice-kind",
        ),
        pytest.param(
            update_of(tables.notifications, NOTIFICATION, kind="notice", decision_id=None, notice_kind="deploy"),
            errors.CheckViolation,
            id="notice-kind-deploy",
        ),
        pytest.param(
            update_of(tables.notifications, NOTIFICATION, title="two\nlines"),
            errors.CheckViolation,
            id="notification-title-two-lines",
        ),
        pytest.param(
            update_of(tables.notifications, NOTIFICATION, title=""),
            errors.CheckViolation,
            id="notification-title-empty",
        ),
        pytest.param(
            update_of(tables.notifications, NOTIFICATION, link="https://example.org/x"),
            errors.CheckViolation,
            id="link-absolute",
        ),
        pytest.param(
            update_of(tables.notifications, NOTIFICATION, link="//example.org/x"),
            errors.CheckViolation,
            id="link-protocol-relative",
        ),
        pytest.param(
            update_of(tables.notifications, NOTIFICATION, details=[]), errors.CheckViolation, id="details-array"
        ),
        pytest.param(
            update_of(tables.notifications, NOTIFICATION, body=""), errors.CheckViolation, id="notification-body-empty"
        ),
        pytest.param(
            update_of(
                tables.notifications, NOTIFICATION, read_at=tables.notifications.c.created_at - timedelta(seconds=1)
            ),
            errors.CheckViolation,
            id="read-before-created",
        ),
        pytest.param(
            insert_of(tables.notifications, user_id=USER, kind="decision", decision_id=DECISION, title="Again"),
            errors.UniqueViolation,  # a decision notifies a member once
            id="decision-notified-twice",
        ),
        # channels and deliveries
        pytest.param(
            insert_of(tables.notification_channels, user_id=USER, kind="web"), errors.CheckViolation, id="channel-web"
        ),
        pytest.param(
            insert_of(tables.notification_channels, user_id=USER, kind="Tele gram"),
            errors.CheckViolation,
            id="channel-kind-with-space",
        ),
        pytest.param(
            insert_of(tables.notification_channels, user_id=USER, kind="telegram"),
            errors.UniqueViolation,
            id="channel-twice",
        ),
        pytest.param(
            update_of(tables.notification_channels, CHANNEL, config=[]), errors.CheckViolation, id="config-array"
        ),
        pytest.param(
            update_of(tables.notification_deliveries, DELIVERY, state="sent"), errors.CheckViolation, id="state-sent"
        ),
        pytest.param(
            update_of(tables.notification_deliveries, DELIVERY, attempts=6), errors.CheckViolation, id="attempts-6"
        ),
        pytest.param(
            update_of(tables.notification_deliveries, DELIVERY, state="failed"),
            errors.CheckViolation,
            id="failed-before-five-attempts",
        ),
        pytest.param(
            update_of(tables.notification_deliveries, DELIVERY, state="delivered"),
            errors.CheckViolation,
            id="delivered-when",
        ),
        pytest.param(
            update_of(tables.notification_deliveries, DELIVERY, last_error=""),
            errors.CheckViolation,
            id="last-error-empty",
        ),
        pytest.param(
            insert_of(tables.notification_deliveries, notification_id=NOTIFICATION),
            errors.UniqueViolation,  # the web once
            id="web-delivery-twice",
        ),
        pytest.param(
            insert_of(tables.notification_deliveries, notification_id=NOTIFICATION, channel_id=CHANNEL),
            errors.UniqueViolation,
            id="channel-delivery-twice",
        ),
    ],
)
def test_constraints_of_0010_refuse_bad_rows(plan_db, statement, error):
    conn, ids = plan_db
    refused = statement(ids)
    with pytest.raises(error):
        conn.execute(refused)


def test_constraints_of_0010_accept_good_rows(plan_db):
    conn, ids = plan_db
    run, decisions, notifications = tables.runs, tables.decisions, tables.notifications
    channels, deliveries = tables.notification_channels, tables.notification_deliveries
    the_run = run.c.id == ids["plan_run"]
    the_decision = decisions.c.id == ids["decision"]
    repos = [{"repo": "evo-agents"}, {"repo": "web", "branch": None}, {"repo": "x", "branch": "y"}]
    resumed = select(
        run.c.project_id,
        run.c.plan_id,
        run.c.kind,
        run.c.plan_revision,
        run.c.dispatched_by,
        run.c.worker_id,
        run.c.runtime,
        run.c.runtime,
        run.c.mode,
        run.c.approval,
        run.c.timeout_s,
        run.c.repos,
        run.c.id,
        run.c.run_seconds,
        run.c.session_id,
    ).where(the_run)
    options = [{"key": "yes", "label": "Deploy"}, {"key": "no", "label": "Wait"}]
    asked = select(
        run.c.id,
        run.c.project_id,
        run.c.plan_id,
        literal("deploy"),
        literal("Deploy to staging now?"),
        literal(options, JSONB),
    ).where(run.c.resume_of_run_id == ids["plan_run"])
    for statement in (
        # the plan run through waiting and parked, as the worker and the reaper move it
        update(run)
        .values(
            state="waiting",
            waiting_since=func.now(),
            run_seconds=3600,
            model="claude-opus-4-1",
            session_id="0199a3c1-0000-7000-8000-00000000000b",
        )
        .where(the_run),
        update(run).values(state="running", waiting_since=None).where(the_run),
        update(run).values(state="waiting", waiting_since=func.now(), repos=repos).where(the_run),
        update(run)
        .values(state="parked", parked_at=func.now(), lease_expires_at=None, waiting_since=None)
        .where(the_run),
        # the owner answers with an option and text of their own: the parked run is done, and the run that resumes it
        # is queued on the same worker
        update(decisions)
        .values(
            state="answered",
            answered_at=func.now(),
            answered_by=ids["user"],
            answer_option="b",
            answer_text="And keep the SQLite file as a backup.",
        )
        .where(the_decision),
        update(run).values(state="done", finished_at=func.now()).where(the_run),
        insert(run).from_select(
            [
                "project_id",
                "plan_id",
                "kind",
                "plan_revision",
                "dispatched_by",
                "pinned_worker_id",
                "requested_runtime",
                "runtime",
                "mode",
                "approval",
                "timeout_s",
                "repos",
                "resume_of_run_id",
                "run_seconds",
                "session_id",
            ],
            resumed,
        ),
        update(decisions).values(delivered_at=func.now()).where(the_decision),
        update(notifications).values(read_at=func.now()).where(notifications.c.id == ids["notification"]),
        # the deliveries: the web at once, Telegram after a failed try
        update(deliveries)
        .values(state="delivered", delivered_at=func.now())
        .where(deliveries.c.id == ids["web_delivery"]),
        update(deliveries)
        .values(attempts=1, next_at=later(minutes=2), last_error="telegram answered 429")
        .where(deliveries.c.id == ids["delivery"]),
        update(deliveries).values(state="failed", attempts=5).where(deliveries.c.id == ids["delivery"]),
        # a notice of a push to a default branch, read later, on the web only
        insert(notifications).values(
            user_id=ids["user"],
            kind="notice",
            notice_kind="push_default_branch",
            project_id=ids["project"],
            run_id=ids["plan_run"],
            title="Pushed main of evo-agents",
            body="2 commits",
            details={"repo": "evo-agents", "branch": "main", "commits": ["a" * 40]},
            link="/",
        ),
        insert(channels).values(user_id=ids["user"], kind="mail", enabled=False),
        # a decision without a step or context, open, and one the reaper let expire
        insert(decisions).from_select(["run_id", "project_id", "plan_id", "category", "question", "options"], asked),
        update(decisions).values(state="expired").where(decisions.c.step_key.is_(None)),
    ):
        conn.execute(statement)
    rows = (
        select(run.c.kind, run.c.state, run.c.resume_of_run_id, run.c.session_id.is_not(None))
        .where(run.c.plan_id == "rollout")
        .order_by(run.c.id)
    )
    assert conn.execute(rows).all() == [("plan", "done", None, True), ("plan", "queued", ids["plan_run"], True)]
    unread = count(notifications, notifications.c.user_id == ids["user"], notifications.c.read_at.is_(None))
    assert one(conn, unread) == 1
    # a run, its decisions and their notifications go together
    conn.execute(delete(run).where(run.c.resume_of_run_id == ids["plan_run"]))
    conn.execute(delete(run).where(the_run))
    for gone in (decisions, notifications, deliveries):
        assert one(conn, count(gone)) == 0, gone.name

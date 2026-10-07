"""GET /v1/me/overview, the web's Home: what waits for the caller, what runs and what ended lately.

The checks step 7 of the hub-ui-kit plan names: a reader of project A sees nothing of project B, a plan labelled
customer is hidden from a grant that reaches internal, the counts match the lists, done_by_day follows UTC days, a
member without a grant gets empty lists, and a machine token has the rights of its owner. The database's time zone is
seven hours east of UTC throughout, so a day boundary taken in the session's zone would show."""

from datetime import datetime, time, timedelta, timezone

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.overview import DAYS, MAX_ACTIVE, MAX_DECISIONS, MAX_RECENT, RUNNING_STATES
from evo_agents.hub.server.security import SESSION_COOKIE, WEB
from evo_agents.isotime import parse_iso
from tests.hub.live import ADMIN, bearer, sql
from tests.hub.test_plan_runs import plan_body
from tests.hub.test_plans import registration
from tests.hub.test_runs import PROTOCOL, add_worker

A, B = "evo-agents", "beta"
ROLLOUT, DOCS, VAULT, BETA_PLAN = "rollout", "docs", "vault", "beta-plan"
OWNER, OTHER, READER, INSIDER, BEA, STRANGER = "owner", "someone-else", "reader", "insider", "bea", "stranger"
GRANTS = {
    A: {OWNER: ("writer", "customer"), OTHER: ("writer", "internal"), READER: ("reader", "internal")}
    | {INSIDER: ("reader", "customer")},
    B: {BEA: ("writer", "internal"), OWNER: ("reader", "internal")},
}
SESSION_ZONE = "Asia/Ho_Chi_Minh"  # UTC+7 all year
OPTIONS = [{"key": "yes", "label": "Go on"}, {"key": "no", "label": "Stop here"}]
WAIT = timedelta(hours=24)  # EVO_HUB_DECISION_WAIT_SECONDS by default


@pytest.fixture
def client(hub_db, tmp_path, github):
    sql(hub_db, f"ALTER DATABASE \"{hub_db.name}\" SET timezone TO '{SESSION_ZONE}'")
    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github)), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def members(client, hub_db) -> dict:
    """Projects A (hub sink clearing customer) and B with their grants, and a machine token of each member, a
    stranger and a hub admin without a grant. Plans rollout and docs of A carry the default label (internal), vault
    of A is labelled customer, and beta-plan is B's."""
    logins = [ADMIN, OWNER, OTHER, READER, INSIDER, BEA, STRANGER]
    headers = {login: bearer(live.insert_token(hub_db, login)) for login in logins}
    hub_sink = [{"id": "hub", "kind": "hub", "clearance": {"level": "customer"}}]
    beta = {
        **registration(),
        "repos": [{"name": "beta-app", "path": "beta-app"}],
        "harness": {"name": B, "workspace": "~/ws", "path": "beta-harness"},
    }
    for project, body in ((A, registration(hub_sink)), (B, beta)):
        response = client.put(f"/v1/projects/{project}", json=body, headers=headers[ADMIN])
        assert response.status_code == 200, response.text
        for login, (role, level) in GRANTS[project].items():
            grant = {"role": role, "max_level": level}
            path = f"/v1/admin/projects/{project}/grants/{login}"
            assert client.put(path, json=grant, headers=headers[ADMIN]).status_code == 200
    for project, plan_id, label, pusher in (
        (A, ROLLOUT, None, OWNER),
        (A, DOCS, None, OWNER),
        (A, VAULT, {"level": "customer"}, OWNER),
        (B, BETA_PLAN, None, BEA),
    ):
        body = {"body": plan_body(plan_id), **({"label": label} if label else {})}
        response = client.put(f"/v1/projects/{project}/plans/{plan_id}", json=body, headers=headers[pusher])
        assert response.status_code == 200, response.text
    return headers


def db_now(db) -> datetime:
    return sql(db, "SELECT now()")[0][0]


def utc_today_start(db) -> datetime:
    today = sql(db, "SELECT (now() AT TIME ZONE 'UTC')::date")[0][0]
    return datetime.combine(today, time.min, tzinfo=timezone.utc)


INSERT_RUN = """
INSERT INTO runs (project_id, plan_id, kind, step_key, title, plan_revision, dispatched_by, worker_id,
                  requested_runtime, runtime, mode, approval, timeout_s, state, repo, repos, queued_at, leased_at,
                  lease_expires_at, started_at, finished_at, waiting_since, parked_at, error)
SELECT p.id, pl.plan_id, %(kind)s, %(step)s, %(title)s, pl.revision, u.id, %(worker)s, 'claude-code', 'claude-code',
       'headless', 'review', 3600, %(state)s, %(repo)s, %(repos)s, %(queued_at)s, %(leased_at)s, %(lease)s,
       %(started_at)s, %(finished_at)s, %(waiting_since)s, %(parked_at)s, %(error)s
  FROM projects p JOIN plans pl ON pl.project_id = p.id JOIN users u ON lower(u.login) = lower(%(owner)s)
 WHERE p.name = %(project)s AND pl.plan_id = %(plan)s
RETURNING id
"""
HELD = ("leased", "running", "interactive", "verifying", "waiting")
ENDED = ("done", "failed", "lost", "cancelled")


def add_run(db, worker_id, project, plan_id, state, owner, *, step=None, finished_at=None, since=None) -> int:
    """A run written straight into the database: of step ``step``, or a plan run without one. ``since`` is when a
    waiting run started waiting, or when a parked run parked."""
    now = db_now(db)
    started = None if state == "queued" else (finished_at or now) - timedelta(minutes=50)
    params = {
        "project": project,
        "plan": plan_id,
        "kind": "plan" if step is None else "step",
        "step": step,
        "title": None if step is None else f"Step {step}",
        "owner": owner,
        "worker": None if state == "queued" else worker_id,
        "state": state,
        "repo": None if step is None else "evo-agents",
        "repos": Jsonb([{"repo": "evo-agents", "branch": "main"}]) if step is None else None,
        "queued_at": (finished_at or now) - timedelta(hours=1),
        "leased_at": started,
        "lease": now + timedelta(minutes=5) if state in HELD else None,
        "started_at": started,
        "finished_at": finished_at if state in ENDED else None,
        "waiting_since": since if state == "waiting" else None,
        "parked_at": since if state == "parked" else None,
        "error": "verify failed: pytest exited 1" if state in ("failed", "lost") else None,
    }
    return sql(db, INSERT_RUN, params)[0][0]


def add_decision(db, run_id: int, asked_at: datetime, *, answered_by: str | None = None) -> int:
    """A decision of run ``run_id``, open, or answered by ``answered_by``."""
    answered = answered_by is not None
    return sql(
        db,
        "INSERT INTO decisions (run_id, project_id, plan_id, step_key, category, question, options, asked_at, state, "
        "answer_option, answered_by, answered_at) "
        "SELECT r.id, r.project_id, r.plan_id, '2', 'scope', %s, %s, %s, %s, %s, "
        "(SELECT id FROM users WHERE login = %s), %s FROM runs r WHERE r.id = %s RETURNING id",
        (
            f"Go on with run {run_id}?",
            Jsonb(OPTIONS),
            asked_at,
            "answered" if answered else "open",
            "yes" if answered else None,
            answered_by,
            asked_at + timedelta(minutes=1) if answered else None,
            run_id,
        ),
    )[0][0]


@pytest.fixture
def world(client, members, hub_db) -> dict:
    """Runs and decisions over the four plans, keyed by a name; ``world["worker"]`` is owner's worker."""
    worker = add_worker(client, members[OWNER], "mac-mini")
    wid = worker["id"]
    now = db_now(hub_db)
    today = utc_today_start(hub_db)
    found = {"worker": worker}

    def run(name, *args, **kwargs):
        found[name] = add_run(hub_db, wid, *args, **kwargs)

    # A/rollout, internal
    run("waiting", A, ROLLOUT, "waiting", OWNER, since=now - timedelta(hours=2))
    run("running", A, ROLLOUT, "running", OWNER, step="2")
    run("queued", A, ROLLOUT, "queued", OTHER, step="3")
    run("verifying", A, ROLLOUT, "verifying", OTHER, step="4")
    run("review", A, ROLLOUT, "review", OWNER, step="5")
    run("done_today", A, ROLLOUT, "done", OWNER, step="1", finished_at=now)
    run("failed_yesterday", A, ROLLOUT, "failed", OWNER, step="1", finished_at=today - timedelta(hours=1))
    run("lost_3d", A, ROLLOUT, "lost", OWNER, step="2", finished_at=today - timedelta(days=3) + timedelta(hours=1))
    run("cancelled_today", A, ROLLOUT, "cancelled", OWNER, step="3", finished_at=now)
    run("done_8d", A, ROLLOUT, "done", OWNER, step="1", finished_at=today - timedelta(days=8))
    # A/docs, internal: another member's plan run, parked
    run("parked", A, DOCS, "parked", OTHER, since=now - timedelta(hours=2))
    # A/vault, customer
    run("vault_running", A, VAULT, "running", OWNER, step="1")
    run("vault_done", A, VAULT, "done", OWNER, step="2", finished_at=now)
    run("vault_waiting", A, VAULT, "waiting", OWNER, since=now - timedelta(minutes=30))
    # B/beta-plan
    run("beta_running", B, BETA_PLAN, "running", BEA, step="1")
    run("beta_done", B, BETA_PLAN, "done", BEA, step="2", finished_at=now)
    run("beta_waiting", B, BETA_PLAN, "waiting", BEA, since=now - timedelta(hours=3))

    found["d_mine"] = add_decision(hub_db, found["waiting"], now - timedelta(hours=2))
    found["d_answered"] = add_decision(hub_db, found["waiting"], now - timedelta(hours=5), answered_by=OWNER)
    found["d_other"] = add_decision(hub_db, found["parked"], now - timedelta(hours=26))
    found["d_vault"] = add_decision(hub_db, found["vault_waiting"], now - timedelta(minutes=30))
    found["d_beta"] = add_decision(hub_db, found["beta_waiting"], now - timedelta(hours=3))
    return found


def overview(client, headers) -> dict:
    response = client.get("/v1/me/overview", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def run_ids(items: list[dict]) -> set[int]:
    return {item["id"] for item in items}


def everything(shown: dict) -> tuple[set[int], set[int]]:
    """The runs and the decisions an overview names, wherever it names them."""
    return run_ids(shown["active_runs"]) | run_ids(shown["recent_runs"]), run_ids(shown["open_decisions"])


def by_id(items: list[dict]) -> dict[int, dict]:
    return {item["id"]: item for item in items}


# Who sees what


def test_a_reader_of_one_project_sees_nothing_of_another(client, members, world):
    shown = overview(client, members[READER])
    assert [project["name"] for project in shown["projects"]] == [A]
    assert {item["project"] for item in shown["active_runs"] + shown["recent_runs"]} == {A}
    assert {item["project"] for item in shown["open_decisions"]} == {A}
    beta = {world[name] for name in ("beta_running", "beta_done", "beta_waiting")}
    runs, decisions = everything(shown)
    assert not runs & beta and world["d_beta"] not in decisions
    # a member with grants on both sees both, each with its own role
    mine = overview(client, members[OWNER])
    assert [(p["name"], p["role"], p["max_level"]) for p in mine["projects"]] == [
        (B, "reader", "internal"),
        (A, "writer", "customer"),
    ]
    assert beta <= everything(mine)[0] and world["d_beta"] in everything(mine)[1]
    # and B's own member sees B alone
    theirs = overview(client, members[BEA])
    assert [project["name"] for project in theirs["projects"]] == [B]
    assert everything(theirs) == (beta, {world["d_beta"]})
    assert theirs["counts"]["waiting_on_you"] == 1


def test_a_plan_labelled_customer_is_hidden_from_a_grant_that_reaches_internal(client, members, world):
    vault_runs = {world[name] for name in ("vault_running", "vault_done", "vault_waiting")}
    hidden = overview(client, members[READER])
    runs, decisions = everything(hidden)
    assert not runs & vault_runs and world["d_vault"] not in decisions
    assert hidden["counts"] == {
        "waiting_on_you": 0,
        "running": 2,  # running and verifying of rollout; vault's is not counted either
        "queued": 1,
        "done_7d": 1,
        "failed_7d": 1,
        "lost_7d": 1,
    }
    (project,) = hidden["projects"]
    assert project == {
        "name": A,
        "role": "reader",
        "max_level": "internal",
        "repos": 5,
        "active_plans": 2,
        "open_decisions": 2,
    }
    # a reader whose grant reaches customer sees vault, and its decision is not theirs to answer
    shown = overview(client, members[INSIDER])
    runs, decisions = everything(shown)
    assert vault_runs <= runs and world["d_vault"] in decisions
    assert (shown["counts"]["running"], shown["counts"]["done_7d"], shown["counts"]["waiting_on_you"]) == (3, 2, 0)
    assert shown["projects"][0]["active_plans"] == 3 and shown["projects"][0]["open_decisions"] == 3
    assert not any(item["yours"] for item in shown["open_decisions"])


def reregister(client, members, sinks) -> None:
    """Project A registered again by the hub admin, with ``sinks`` instead of its hub sink clearing customer."""
    response = client.put(f"/v1/projects/{A}", json=registration(sinks), headers=members[ADMIN])
    assert response.status_code == 200, response.text
    assert response.json()["changed"]


def test_a_hub_sink_below_the_grant_limits_what_is_shown(client, members, world):
    reregister(client, members, [{"id": "hub", "kind": "hub", "clearance": {"level": "internal"}}])
    vault_runs = {world[name] for name in ("vault_running", "vault_done", "vault_waiting")}
    reader = overview(client, members[READER])
    for login in (INSIDER, OWNER):  # their grants reach customer, the hub sink internal: the sink limits
        shown = overview(client, members[login])
        runs, decisions = everything(shown)
        assert not runs & vault_runs and world["d_vault"] not in decisions, login
        (project,) = [item for item in shown["projects"] if item["name"] == A]
        assert (project["max_level"], project["active_plans"], project["open_decisions"]) == ("customer", 2, 2)
    insider = overview(client, members[INSIDER])
    assert insider["counts"] == reader["counts"]
    assert everything(insider) == everything(reader)
    # B's own sink is untouched: the owner still sees B's runs, and its own decision of rollout but not of vault
    mine = overview(client, members[OWNER])
    assert world["beta_running"] in everything(mine)[0]
    assert (mine["counts"]["waiting_on_you"], mine["counts"]["running"], mine["counts"]["done_7d"]) == (1, 3, 2)


def test_a_project_without_a_hub_sink_shows_nothing(client, members, world):
    reregister(
        client, members, [{"id": "claude-code@anthropic", "kind": "agent-session", "clearance": {"level": "customer"}}]
    )
    for login in (READER, INSIDER, OTHER):
        shown = overview(client, members[login])
        assert shown["counts"] == dict.fromkeys(shown["counts"], 0), login
        assert [item["done"] for item in shown["done_by_day"]] == [0] * DAYS
        assert (shown["active_runs"], shown["recent_runs"], shown["open_decisions"]) == ([], [], [])
        (project,) = shown["projects"]  # the project is still the member's, with nothing of it shown
        assert (project["name"], project["active_plans"], project["open_decisions"]) == (A, 0, 0)
    # the owner, who holds a grant on B too, sees B alone
    mine = overview(client, members[OWNER])
    beta = {world[name] for name in ("beta_running", "beta_done", "beta_waiting")}
    assert everything(mine) == (beta, {world["d_beta"]})
    assert [(p["name"], p["active_plans"], p["open_decisions"]) for p in mine["projects"]] == [(B, 1, 1), (A, 0, 0)]


def test_a_member_without_a_grant_gets_empty_lists(client, members, world):
    for login in (STRANGER, ADMIN):  # a hub admin without a grant reads nothing of a project
        shown = overview(client, members[login])
        assert shown["counts"] == dict.fromkeys(shown["counts"], 0), login
        assert [item["done"] for item in shown["done_by_day"]] == [0] * DAYS
        assert (shown["active_runs"], shown["recent_runs"], shown["open_decisions"], shown["projects"]) == (
            [],
            [],
            [],
            [],
        )


def test_a_machine_token_has_the_rights_of_its_owner(client, members, world, hub_db):
    web = {"Cookie": f"{SESSION_COOKIE}={live.insert_token(hub_db, OWNER, kind=WEB)}"}
    assert overview(client, web) == overview(client, members[OWNER])
    # a worker's token, though it is the owner's, works only on the worker's routes
    as_worker = client.get("/v1/me/overview", headers={**world["worker"]["headers"], **PROTOCOL})
    assert as_worker.status_code == 403, as_worker.text
    assert client.get("/v1/me/overview").status_code == 401


# What the overview says


def test_the_counts_match_the_lists(client, members, world, hub_db):
    shown = overview(client, members[OWNER])
    counts, active, recent, decisions = (
        shown[key] for key in ("counts", "active_runs", "recent_runs", "open_decisions")
    )
    states = [item["state"] for item in active]
    assert counts["running"] == sum(state in RUNNING_STATES for state in states) == 4
    assert counts["queued"] == states.count("queued") == 1
    assert counts["waiting_on_you"] == sum(item["yours"] for item in decisions) == 2
    assert counts["done_7d"] == sum(day["done"] for day in shown["done_by_day"]) == 3
    assert (counts["failed_7d"], counts["lost_7d"]) == (1, 1)
    # active: the agents at work, newest first, then waiting, parked and queued; a run in review is not in flight
    at_work = sorted((world[name] for name in ("running", "verifying", "vault_running", "beta_running")), reverse=True)
    waiting = sorted((world[name] for name in ("waiting", "vault_waiting", "beta_waiting")), reverse=True)
    assert [item["id"] for item in active] == [*at_work, *waiting, world["parked"], world["queued"]]
    assert world["review"] not in run_ids(active) | run_ids(recent)
    # a plan run shows its plan's steps done of all; a run of one step its step
    plan_run = by_id(active)[world["waiting"]]
    assert (plan_run["kind"], plan_run["step_key"], plan_run["steps_done"], plan_run["steps_total"]) == (
        "plan",
        None,
        2,
        5,
    )
    assert (plan_run["plan_title"], plan_run["worker"], plan_run["runtime"]) == (
        "Run the fleet",
        "mac-mini",
        "claude-code",
    )
    step_run = by_id(active)[world["running"]]
    assert (step_run["step_key"], step_run["title"], step_run["steps_done"], step_run["steps_total"]) == (
        "2",
        "Step 2",
        None,
        None,
    )
    assert by_id(active)[world["queued"]]["worker"] is None and by_id(active)[world["queued"]]["started_at"] is None
    # recent: every end state, the last to end first
    ended = ("done_today", "cancelled_today", "vault_done", "beta_done", "failed_yesterday", "lost_3d", "done_8d")
    assert run_ids(recent) == {world[name] for name in ended}
    order = [(parse_iso(item["finished_at"]), item["id"]) for item in recent]
    assert order == sorted(order, reverse=True)
    assert by_id(recent)[world["failed_yesterday"]]["error"] == "verify failed: pytest exited 1"
    # open decisions: the caller's own first, the oldest first; answered ones are left out
    assert [item["id"] for item in decisions] == [world[n] for n in ("d_mine", "d_vault", "d_other", "d_beta")]
    assert world["d_answered"] not in run_ids(decisions)
    mine = by_id(decisions)[world["d_mine"]]
    assert (mine["run_id"], mine["run_state"], mine["plan_id"], mine["step_key"], mine["owner"], mine["yours"]) == (
        world["waiting"],
        "waiting",
        ROLLOUT,
        "2",
        OWNER,
        True,
    )
    waiting_since, parked_at = sql(
        hub_db,
        "SELECT (SELECT waiting_since FROM runs WHERE id = %s), (SELECT parked_at FROM runs WHERE id = %s)",
        (world["waiting"], world["parked"]),
    )[0]
    assert parse_iso(mine["parks_at"]) == waiting_since + WAIT
    other = by_id(decisions)[world["d_other"]]
    assert (other["owner"], other["yours"], parse_iso(other["parks_at"])) == (OTHER, False, parked_at)
    # projects: the open decisions of each add up to the list's
    assert [(p["name"], p["repos"], p["active_plans"], p["open_decisions"]) for p in shown["projects"]] == [
        (B, 1, 1, 1),
        (A, 5, 3, 3),
    ]
    assert sum(p["open_decisions"] for p in shown["projects"]) == len(decisions)


def test_done_by_day_follows_utc_days(client, members, hub_db):
    assert sql(hub_db, "SELECT current_setting('TimeZone')") == [(SESSION_ZONE,)]
    wid = add_worker(client, members[OWNER], "mac-mini")["id"]
    today = utc_today_start(hub_db)
    first = today - timedelta(days=DAYS - 1)
    for state, finished_at in (
        ("done", today),  # the first instant of today
        ("done", today - timedelta(microseconds=1)),  # the last of yesterday, already today at UTC+7
        ("done", today - timedelta(microseconds=1)),
        ("done", first),  # the first instant of the oldest day counted
        ("done", first - timedelta(microseconds=1)),  # a day too old
        ("failed", first),
        ("lost", first - timedelta(microseconds=1)),
        ("cancelled", today),  # ended, but none of the counts
    ):
        add_run(hub_db, wid, A, ROLLOUT, state, OWNER, step="1", finished_at=finished_at)
    shown = overview(client, members[OWNER])
    days = shown["done_by_day"]
    assert [item["day"] for item in days] == [(first + timedelta(days=n)).date().isoformat() for n in range(DAYS)]
    assert [item["done"] for item in days] == [1, 0, 0, 0, 0, 2, 1]
    assert {key: shown["counts"][key] for key in ("done_7d", "failed_7d", "lost_7d")} == {
        "done_7d": 4,
        "failed_7d": 1,
        "lost_7d": 0,
    }
    assert len(shown["recent_runs"]) == 8


def test_the_lists_stop_at_their_limits_and_the_counts_do_not(client, members, hub_db):
    wid = add_worker(client, members[OWNER], "mac-mini")["id"]
    now = db_now(hub_db)
    for key in range(1, MAX_ACTIVE + 6):
        add_run(hub_db, wid, A, ROLLOUT, "queued", OWNER, step=str(key))
    for _ in range(MAX_RECENT + 2):
        add_run(hub_db, wid, A, ROLLOUT, "done", OWNER, step="1", finished_at=now)
    for plan_id in (ROLLOUT, DOCS, VAULT):
        run_id = add_run(hub_db, wid, A, plan_id, "waiting", OWNER, since=now)
        for _ in range(MAX_DECISIONS):
            add_decision(hub_db, run_id, now)
    shown = overview(client, members[OWNER])
    assert (len(shown["active_runs"]), shown["counts"]["queued"]) == (MAX_ACTIVE, MAX_ACTIVE + 5)
    assert len(shown["recent_runs"]) == MAX_RECENT and shown["counts"]["done_7d"] == MAX_RECENT + 2
    assert len(shown["open_decisions"]) == MAX_DECISIONS
    (project,) = [item for item in shown["projects"] if item["name"] == A]
    assert shown["counts"]["waiting_on_you"] == 3 * MAX_DECISIONS == project["open_decisions"]

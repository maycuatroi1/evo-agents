"""GET /v1/projects/{p}/runs/stats: the runs that ended on each UTC day, for the web's charts.

The checks step 16 of the hub-ui-kit plan names: day boundaries in UTC, days without a run, usage missing or of
another shape, plans hidden by their label (through the grant, a hub sink below the grant, a project without a hub
sink, or the sink the caller names), 403 for a hub admin without a grant, and days out of range answered 422. The
database's time zone is seven hours east of UTC throughout, so a day taken in the session's zone would show."""

from datetime import UTC, datetime, time, timedelta

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import Date, cast, func, insert, null, select

from evo_agents.hub import tables
from evo_agents.hub.runs import HELD_STATES, TERMINAL_STATES
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.plans import SINK_HEADER
from evo_agents.hub.server.runs import MAX_STATS_DAYS, MIN_STATS_DAYS, STATS_DAYS
from tests.hub.live import ADMIN, bearer, sql
from tests.hub.test_plan_runs import plan_body
from tests.hub.test_plans import registration
from tests.hub.test_runs import PROTOCOL, add_worker

A, B = "evo-agents", "beta"
ROLLOUT, VAULT, BETA_PLAN = "rollout", "vault", "beta-plan"
OWNER, READER, INSIDER, BEA, STRANGER = "owner", "reader", "insider", "bea", "stranger"
GRANTS = {
    A: {OWNER: ("writer", "customer"), READER: ("reader", "internal"), INSIDER: ("reader", "customer")},
    B: {BEA: ("writer", "internal")},
}
AGENT_SINK = "claude-code@anthropic"
SINKS = [
    {"id": "hub", "kind": "hub", "clearance": {"level": "customer"}},
    {"id": AGENT_SINK, "kind": "agent-session", "clearance": {"level": "internal"}},
]
SESSION_ZONE = "Asia/Ho_Chi_Minh"  # UTC+7 all year
STATS = f"/v1/projects/{A}/runs/stats"
TOKENS = ("input_tokens", "cache_read_tokens", "output_tokens", "reasoning_tokens")
NOTHING = dict.fromkeys(TERMINAL_STATES, 0) | {"p50_seconds": None, "p90_seconds": None, "runs_with_usage": 0}
NOTHING |= dict.fromkeys(TOKENS, 0)

# Each runtime's usage as its worker reports it with the run's end (the fixtures of web/src/test/fixtures/trace, real
# runs of the three adapters, and the cases of usage-model.test.ts), with the tokens the run page's usage card reads
# from it: input, cache read, output and reasoning, or None for usage it cannot read.
CLAUDE_CODE = {
    "input_tokens": 12,
    "cache_creation_input_tokens": 24069,
    "cache_read_input_tokens": 138764,
    "output_tokens": 361,
    "total_cost_usd": 0.22757280000000002,
}
CODEX = {
    "cache_write_input_tokens": 0,
    "cached_input_tokens": 102016,
    "input_tokens": 125097,
    "output_tokens": 131,
    "reasoning_output_tokens": 0,
    "total_tokens": 125228,
}
OPENCODE = {"total": 378932, "input": 94944, "output": 103, "reasoning": 109, "cache_write": 0, "cache_read": 283776}
USAGES = {
    "claude-code": (CLAUDE_CODE, (12, 138764, 361, 0)),
    "codex": (CODEX, (125097 - 102016, 102016, 131, 0)),
    "opencode": (OPENCODE | {"cost": 0.0}, (94944, 283776, 103, 109)),
    # thinking and reasoning are parts of the output of Claude Code and Codex; Codex's cached input is part of its input
    "claude thinking": (
        {"input_tokens": 5, "output_tokens": 300, "output_tokens_details": {"thinking_tokens": 120}},
        (5, 0, 180, 120),
    ),
    "codex camelCase": (
        {"inputTokens": 10, "cachedInputTokens": 4, "outputTokens": 50, "reasoningOutputTokens": 20},
        (6, 4, 30, 20),
    ),
    "codex more cached than input": (
        {"input_tokens": 10, "cached_input_tokens": 50, "output_tokens": 3, "reasoning_output_tokens": 9},
        (0, 10, 0, 3),
    ),
    "opencode nested cache": ({"input": 10, "output": 5, "cache": {"read": 7, "write": 1}}, (10, 7, 5, 0)),
    # a known shape whose numbers are negative, true or text: those count 0
    "odd numbers": ({"input_tokens": -5, "output_tokens": True, "cache_read_input_tokens": 4}, (0, 4, 0, 0)),
    "no known shape": ({"speed": "standard", "tokens": 500}, None),
    "numbers as text": ({"input_tokens": "12", "output_tokens": "3"}, None),
    "nested only": ({"usage": {"input_tokens": 12}}, None),
    "no usage": (None, None),
}


@pytest.fixture
def client(hub_db, tmp_path, github, monkeypatch):
    # Every session of the test starts in SESSION_ZONE: libpq sets its time zone from PGTZ, for the hub's pool and the
    # test's own connections alike.
    monkeypatch.setenv("PGTZ", SESSION_ZONE)
    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github)), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def members(client, hub_db) -> dict:
    """Project A (a hub sink clearing customer, an agent session's clearing internal) and B with their grants, and a
    machine token of each member, a stranger and a hub admin without a grant. Plan rollout of A carries the default
    label (internal), vault of A is labelled customer, and beta-plan is B's."""
    logins = [ADMIN, OWNER, READER, INSIDER, BEA, STRANGER]
    headers = {login: bearer(live.insert_token(hub_db, login)) for login in logins}
    beta = {
        **registration(),
        "repos": [{"name": "beta-app", "path": "beta-app"}],
        "harness": {"name": B, "workspace": "~/ws", "path": "beta-harness"},
    }
    for project, body in ((A, registration(SINKS)), (B, beta)):
        response = client.put(f"/v1/projects/{project}", json=body, headers=headers[ADMIN])
        assert response.status_code == 200, response.text
        for login, (role, level) in GRANTS[project].items():
            path = f"/v1/admin/projects/{project}/grants/{login}"
            assert client.put(path, json={"role": role, "max_level": level}, headers=headers[ADMIN]).status_code == 200
    for project, plan_id, label, pusher in (
        (A, ROLLOUT, None, OWNER),
        (A, VAULT, {"level": "customer"}, OWNER),
        (B, BETA_PLAN, None, BEA),
    ):
        body = {"body": plan_body(plan_id), **({"label": label} if label else {})}
        response = client.put(f"/v1/projects/{project}/plans/{plan_id}", json=body, headers=headers[pusher])
        assert response.status_code == 200, response.text
    return headers


@pytest.fixture
def worker(client, members) -> dict:
    return add_worker(client, members[OWNER], "mac-mini")


def db_now(db) -> datetime:
    return sql(db, select(func.now()))[0][0]


def utc_today_start(db) -> datetime:
    today = sql(db, select(cast(func.timezone("UTC", func.now()), Date)))[0][0]
    return datetime.combine(today, time.min, tzinfo=UTC)


def add_run(
    db,
    worker: dict,
    state: str,
    finished_at: datetime | None = None,
    *,
    ran: timedelta | None = timedelta(minutes=50),
    leased_only: bool = False,
    usage: dict | None = None,
    project: str = A,
    plan: str = ROLLOUT,
    owner: str = OWNER,
    step: str = "1",
) -> int:
    """A run of step ``step`` written straight into the database. It ran for ``ran`` until ``finished_at`` (now for a
    run that has not ended); ``ran`` None is a run no worker took, ``leased_only`` one leased but never started."""
    now = db_now(db)
    end = finished_at or now
    start = None if ran is None else end - ran
    r, p, pl, u = tables.runs, tables.projects, tables.plans, tables.users
    stmt = insert(r).values(
        project_id=select(p.c.id).where(p.c.name == project).scalar_subquery(),
        plan_id=plan,
        kind="step",
        step_key=step,
        title=f"Step {step}",
        plan_revision=select(pl.c.revision)
        .join_from(pl, p, p.c.id == pl.c.project_id)
        .where(p.c.name == project, pl.c.plan_id == plan)
        .scalar_subquery(),
        dispatched_by=select(u.c.id).where(func.lower(u.c.login) == func.lower(owner)).scalar_subquery(),
        worker_id=None if start is None else worker["id"],
        requested_runtime="claude-code",
        runtime="claude-code",
        mode="headless",
        approval="review",
        timeout_s=3600,
        state=state,
        repo="evo-agents",
        queued_at=(start or end) - timedelta(minutes=5),
        leased_at=start,
        lease_expires_at=now + timedelta(minutes=5) if state in HELD_STATES else None,
        started_at=None if leased_only else start,
        finished_at=finished_at if state in TERMINAL_STATES else None,
        usage=null() if usage is None else usage,  # SQL NULL, not a JSON null
        error="verify failed: pytest exited 1" if state in ("failed", "lost") else None,
    )
    return sql(db, stmt.returning(r.c.id))[0][0]


def stats(client, headers, *, days: int | None = None, sink: str | None = None) -> dict:
    query = {} if days is None else {"days": days}
    response = client.get(STATS, params=query, headers={**headers, **({SINK_HEADER: sink} if sink else {})})
    assert response.status_code == 200, response.text
    return response.json()


def column(shown: dict, name: str) -> list:
    return [day[name] for day in shown["by_day"]]


def tokens(figures: dict) -> tuple:
    return tuple(figures[name] for name in TOKENS)


def without_day(day: dict) -> dict:
    return {key: value for key, value in day.items() if key != "day"}


def reregister(client, members, sinks) -> None:
    response = client.put(f"/v1/projects/{A}", json=registration(sinks), headers=members[ADMIN])
    assert response.status_code == 200, response.text
    assert response.json()["changed"]


# Days


def test_runs_count_on_the_utc_day_they_ended(client, members, worker, hub_db):
    assert sql(hub_db, select(func.current_setting("TimeZone"))) == [(SESSION_ZONE,)]
    today = utc_today_start(hub_db)
    first = today - timedelta(days=6)
    for state, finished_at in (
        ("done", today),  # the first instant of today
        ("done", today - timedelta(microseconds=1)),  # the last of yesterday, already today at UTC+7
        ("failed", today - timedelta(microseconds=1)),
        ("done", first),  # the first instant of the oldest of seven days
        ("lost", first - timedelta(microseconds=1)),  # a day too old for seven
        ("cancelled", db_now(hub_db)),
    ):
        add_run(hub_db, worker, state, finished_at)
    shown = stats(client, members[OWNER], days=7)
    days = [(first + timedelta(days=n)).date().isoformat() for n in range(7)]
    assert (shown["project"], shown["days"], shown["first_day"], shown["last_day"]) == (A, 7, days[0], days[-1])
    assert column(shown, "day") == days
    assert column(shown, "done") == [1, 0, 0, 0, 0, 1, 1]
    assert column(shown, "failed") == [0, 0, 0, 0, 0, 1, 0]
    assert column(shown, "cancelled") == [0, 0, 0, 0, 0, 0, 1]
    assert column(shown, "lost") == [0] * 7
    assert {state: shown["total"][state] for state in TERMINAL_STATES} == {
        "done": 3,
        "failed": 1,
        "lost": 0,
        "cancelled": 1,
    }
    # thirty days without ?days, which reach the lost run
    month = stats(client, members[OWNER])
    assert (month["days"], len(month["by_day"]), month["last_day"]) == (STATS_DAYS, STATS_DAYS, days[-1])
    assert month["first_day"] == (today - timedelta(days=STATS_DAYS - 1)).date().isoformat()
    assert month["by_day"][-7:] == shown["by_day"]
    assert month["by_day"][-8]["lost"] == month["total"]["lost"] == 1


def test_a_day_without_a_run_has_zeros(client, members, worker, hub_db):
    empty = stats(client, members[OWNER], days=7)
    assert [without_day(day) for day in empty["by_day"]] == [NOTHING] * 7
    assert empty["total"] == NOTHING
    three_days_ago = utc_today_start(hub_db) - timedelta(days=3) + timedelta(hours=12)
    add_run(hub_db, worker, "done", three_days_ago, ran=timedelta(minutes=20), usage=CLAUDE_CODE)
    shown = stats(client, members[OWNER], days=7)
    (busy,) = [day for day in shown["by_day"] if day["done"]]
    assert busy["day"] == three_days_ago.date().isoformat()
    assert [without_day(day) for day in shown["by_day"] if day is not busy] == [NOTHING] * 6
    assert without_day(busy) == shown["total"]
    assert (busy["p50_seconds"], busy["p90_seconds"], busy["runs_with_usage"]) == (1200.0, 1200.0, 1)
    assert tokens(busy) == USAGES["claude-code"][1]


def test_durations_are_percentiles_of_the_runs_that_ended(client, members, worker, hub_db):
    now = db_now(hub_db)
    today = utc_today_start(hub_db)
    yesterday = today - timedelta(hours=12)
    for state, minutes in (("done", 10), ("done", 20), ("done", 30), ("done", 40), ("failed", 100)):
        add_run(hub_db, worker, state, now, ran=timedelta(minutes=minutes))
    add_run(hub_db, worker, "lost", now, ran=timedelta(minutes=60), leased_only=True)  # leased, then lost: 60 min
    add_run(hub_db, worker, "cancelled", now, ran=None)  # cancelled while queued: no time to count
    add_run(hub_db, worker, "done", yesterday, ran=timedelta(minutes=5))
    add_run(hub_db, worker, "cancelled", today - timedelta(days=2), ran=None)
    for step, state in enumerate(("running", "verifying", "review", "queued"), start=2):  # not ended: in no figure
        ran = None if state == "queued" else timedelta(hours=9)
        add_run(hub_db, worker, state, ran=ran, usage=CODEX, step=str(step))
    shown = stats(client, members[OWNER], days=7)
    last, before, two_days_ago = shown["by_day"][-1], shown["by_day"][-2], shown["by_day"][-3]
    # today: 10, 20, 30, 40, 60 and 100 minutes; the median between 30 and 40, the 90th between 60 and 100
    assert (last["done"], last["failed"], last["lost"], last["cancelled"]) == (4, 1, 1, 1)
    assert (last["p50_seconds"], last["p90_seconds"]) == (35 * 60.0, 80 * 60.0)
    assert (before["done"], before["p50_seconds"], before["p90_seconds"]) == (1, 300.0, 300.0)
    assert (two_days_ago["cancelled"], two_days_ago["p50_seconds"], two_days_ago["p90_seconds"]) == (1, None, None)
    # the whole span: 5, 10, 20, 30, 40, 60 and 100 minutes
    total = shown["total"]
    assert (total["p50_seconds"], total["p90_seconds"]) == (30 * 60.0, 76 * 60.0)
    assert {state: total[state] for state in TERMINAL_STATES} == {"done": 5, "failed": 1, "lost": 1, "cancelled": 2}
    assert total["runs_with_usage"] == 0 and tokens(total) == (0, 0, 0, 0)


# Usage


def test_usage_is_read_as_the_usage_card_reads_it(client, members, worker, hub_db):
    now = db_now(hub_db)
    yesterday = utc_today_start(hub_db) - timedelta(hours=1)
    names = list(USAGES)
    on_day = {name: (now if number % 2 == 0 else yesterday) for number, name in enumerate(names)}
    for name, (usage, _) in USAGES.items():
        add_run(hub_db, worker, "done", on_day[name], usage=usage)
    add_run(hub_db, worker, "running", usage=CLAUDE_CODE)  # usage of a run that has not ended counts nowhere
    shown = stats(client, members[OWNER], days=7)

    def expected(day: datetime | None) -> tuple:
        read = [found for name, (_, found) in USAGES.items() if found and (day is None or on_day[name] == day)]
        return tuple(sum(parts) for parts in zip(*read, strict=True)), len(read)

    for day, figures in ((now, shown["by_day"][-1]), (yesterday, shown["by_day"][-2]), (None, shown["total"])):
        sums, read = expected(day)
        assert (tokens(figures), figures["runs_with_usage"]) == (sums, read), day
    assert shown["total"]["done"] == len(USAGES)
    assert shown["total"]["runs_with_usage"] == sum(found is not None for _, found in USAGES.values()) == 8


# Who sees what


def test_runs_of_a_plan_hidden_by_its_label_are_not_counted(client, members, worker, hub_db):
    now = db_now(hub_db)
    add_run(hub_db, worker, "done", now, usage=CLAUDE_CODE)
    add_run(hub_db, worker, "failed", now, plan=VAULT, usage=CODEX, ran=timedelta(minutes=10))
    add_run(hub_db, worker, "done", now, project=B, plan=BETA_PLAN, owner=BEA, usage=OPENCODE)
    hidden = stats(client, members[READER])
    assert (hidden["total"]["done"], hidden["total"]["failed"], hidden["total"]["runs_with_usage"]) == (1, 0, 1)
    assert tokens(hidden["total"]) == USAGES["claude-code"][1]
    assert hidden["total"]["p90_seconds"] == 50 * 60.0
    # a grant that reaches customer counts vault through the hub sink, which clears customer
    for login in (INSIDER, OWNER):
        shown = stats(client, members[login])
        assert (shown["total"]["done"], shown["total"]["failed"], shown["total"]["runs_with_usage"]) == (1, 1, 2), login
        both = zip(USAGES["claude-code"][1], USAGES["codex"][1], strict=True)
        assert tokens(shown["total"]) == tuple(map(sum, both))
    # but not through a sink that clears internal, and nothing through a sink the project does not declare
    assert stats(client, members[INSIDER], sink=AGENT_SINK)["total"] == hidden["total"]
    assert stats(client, members[INSIDER], sink="no-such-sink")["total"] == NOTHING
    # B's runs count in B alone
    beta = client.get(f"/v1/projects/{B}/runs/stats", headers=members[BEA])
    assert beta.status_code == 200, beta.text
    assert (beta.json()["total"]["done"], tokens(beta.json()["total"])) == (1, USAGES["opencode"][1])


def test_a_hub_sink_below_the_grant_limits_what_is_counted(client, members, worker, hub_db):
    now = db_now(hub_db)
    add_run(hub_db, worker, "done", now)
    add_run(hub_db, worker, "done", now, plan=VAULT, usage=CODEX)
    assert stats(client, members[INSIDER])["total"]["done"] == 2
    reregister(client, members, [{"id": "hub", "kind": "hub", "clearance": {"level": "internal"}}])
    for login in (INSIDER, OWNER):  # their grants reach customer, the hub sink internal: the sink limits
        shown = stats(client, members[login])["total"]
        assert (shown["done"], shown["runs_with_usage"], tokens(shown)) == (1, 0, (0, 0, 0, 0)), login


def test_a_project_without_a_hub_sink_shows_nothing(client, members, worker, hub_db):
    now = db_now(hub_db)
    add_run(hub_db, worker, "done", now, usage=CLAUDE_CODE)
    add_run(hub_db, worker, "lost", now, plan=VAULT)
    reregister(client, members, [{"id": AGENT_SINK, "kind": "agent-session", "clearance": {"level": "customer"}}])
    for login in (READER, INSIDER, OWNER):
        shown = stats(client, members[login])
        assert shown["total"] == NOTHING, login
        assert [without_day(day) for day in shown["by_day"]] == [NOTHING] * STATS_DAYS
    # a sink the project still declares is read through when the caller names it, by the grant's reach
    assert stats(client, members[READER], sink=AGENT_SINK)["total"]["done"] == 1
    assert stats(client, members[INSIDER], sink=AGENT_SINK)["total"]["lost"] == 1


def test_only_a_member_with_a_grant_reads_the_stats(client, members, worker):
    assert stats(client, members[READER])["project"] == A
    admin = client.get(STATS, headers=members[ADMIN])  # a hub admin without a grant manages, but does not read
    assert admin.status_code == 403, admin.text
    assert "needs a grant" in admin.json()["message"]
    for login in (STRANGER, BEA):  # a member without a grant on A learns nothing of it, as with the runs list
        assert client.get(STATS, headers=members[login]).status_code == 404, login
    assert client.get(STATS).status_code == 401
    as_worker = client.get(STATS, headers={**worker["headers"], **PROTOCOL})
    assert as_worker.status_code == 403, as_worker.text


@pytest.mark.parametrize("days", [MIN_STATS_DAYS - 1, MAX_STATS_DAYS + 1, 0, -7, 365, "week"])
def test_days_out_of_range_are_refused(client, members, days):
    response = client.get(STATS, params={"days": days}, headers=members[OWNER])
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("days", [MIN_STATS_DAYS, MAX_STATS_DAYS])
def test_days_at_either_end_of_the_range_are_answered(client, members, days):
    shown = stats(client, members[OWNER], days=days)
    assert shown["days"] == len(shown["by_day"]) == days

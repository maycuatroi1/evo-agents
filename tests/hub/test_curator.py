"""The night shift on the hub: a project's charter, the schedule it makes, pausing it, and the caps and cost of the runs
the schedule queues (``evo_agents.hub.server.curator``).

The checks step 1 of the curator-agent plan names. Charter (a5): an admin of the project writes it with a machine
token or a web session, every revision is kept, anyone else gets 403 and a worker token 403; ``charter show --json``
prints ``auto_merge`` and ``judge.runtime``. Schedule (a1): inside the charter's window the hub queues a plan run of a
plan the charter allows, pinned to its worker and dispatched by the worker's owner, which only that worker claims;
outside the window, or once the night's cost reaches its budget, nothing is queued; ``curator pause`` stops it within
the minute of the job and ``resume`` lets it run again. Budget (a2): each run carries its caps to the worker, with what
it spent already when it resumes a parked run, and the night's cost counts each agent session once."""

from datetime import UTC, date, datetime, timedelta
from functools import partial
from types import SimpleNamespace

import pytest
import yaml

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import psycopg
from sqlalchemy import func, select, update

from evo_agents.hub import jobs, tables
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.curator import fire_schedules, night_figures
from evo_agents.hub.worker import queue
from tests.hub.contract_keys import assert_json_keys
from tests.hub.fake_github import Account
from tests.hub.live import sql
from tests.hub.test_decisions import answered, asked, park
from tests.hub.test_migrate import move_to
from tests.hub.test_plan_runs import PLAN as FLEET
from tests.hub.test_plan_runs import fleet_worker, push, reported
from tests.hub.test_plan_runs import plan_body as fleet_body
from tests.hub.test_run_cli import as_json, cli, ok, write_credentials
from tests.hub.test_run_stream import serving
from tests.hub.test_runs import (
    GITHUB_IDS,
    OTHER,
    OWNER,
    PLAN,
    PROJECT,
    READER,
    claim,
    control,
    dispatched,
    members,
    moved,
    recover,
    report,
    web_client,  # noqa: F401 (a fixture)
)
from tests.hub.test_web_auth import cookie, csrf_for, web_sign_in

WORKER = "night-mac"
SESSION = "0199a3c1-0000-7000-8000-0000000000aa"
# Asia/Ho_Chi_Minh is UTC+7: the window 22:00 to 06:00 there is 15:00 to 23:00 UTC.
NIGHT = datetime(2026, 10, 8, 16, 30, tzinfo=UTC)  # 23:30 local, the night of 2026-10-08
AFTER_MIDNIGHT = datetime(2026, 10, 8, 21, 0, tzinfo=UTC)  # 04:00 local on the 9th, still that night
DAY = datetime(2026, 10, 9, 5, 0, tzinfo=UTC)  # 12:00 local
NEXT_NIGHT = NIGHT + timedelta(days=1)
THE_NIGHT = date(2026, 10, 8)
HIDDEN = ["python -m pytest -q tests/hidden"]


def charter_body(**changes) -> dict:
    body = {
        "goals": [{"id": "night-shift", "what": "Run the plans the owner approved while the owner sleeps."}],
        "window": {"start": "22:00", "end": "06:00", "timezone": "Asia/Ho_Chi_Minh"},
        "worker": WORKER,
        "night_budget_usd": 2.0,
        "run_budget_usd": 0.5,
        "run_max_turns": 40,
        "run_minutes": 30,
        "max_runs_per_night": 3,
        "night_plans": [FLEET],
        "max_decisions_per_day": 5,
        "brief_at": "06:30",
        "auto_merge": [0],
        "protected_paths": ["curator.yaml", ".github/workflows/**"],
        "judge": {"runtime": "codex", "model": None, "hidden_checks": HIDDEN},
    }
    return {**body, **changes}


def grant(client, headers, login: str, role: str) -> None:
    body = {"role": role, "max_level": "internal"}
    response = client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=body, headers=headers["admin"])
    assert response.status_code == 200, response.text


@pytest.fixture
def night(web_client, github):  # noqa: F811
    """test_runs' members, with owner an admin of the project, the plan fleet, and owner's worker night-mac with a
    checkout of both its repos."""
    headers = members(web_client, github)
    grant(web_client, headers, OWNER, "admin")
    push(web_client, headers["owner"], fleet_body())
    worker = fleet_worker(web_client, headers["owner"], WORKER)
    return SimpleNamespace(client=web_client, headers=headers, worker=worker, github=github)


def charter_path(*parts: str) -> str:
    return "/".join((f"/v1/projects/{PROJECT}/curator", *parts))


def put_charter(client, headers, body: dict):
    return client.put(charter_path("charter"), json=body, headers=headers)


def written(client, headers, body: dict) -> dict:
    response = put_charter(client, headers, body)
    assert response.status_code == 200, response.text
    return response.json()


def shown(client, headers, **query) -> dict:
    response = client.get(charter_path("charter"), params=query, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def status_of(client, headers) -> dict:
    response = client.get(charter_path(), headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def fire(client, now: datetime) -> dict:
    return client.portal.call(partial(fire_schedules, client.app.state.engine, now=now))


def count(db, table, *conditions) -> int:
    return sql(db, select(func.count()).select_from(table).where(*conditions))[0][0]


def user_id(db, login: str) -> int:
    return sql(db, select(tables.users.c.id).where(tables.users.c.login == login))[0][0]


RUN_COLUMNS = (
    "id",
    "state",
    "kind",
    "plan_id",
    "dispatched_by",
    "dispatched_via",
    "pinned_worker_id",
    "runtime",
    "approval",
    "mode",
    "timeout_s",
    "budget",
    "schedule_night",
    "resume_of_run_id",
)


def scheduled(db) -> list[dict]:
    """The runs a schedule queued, oldest first, with RUN_COLUMNS."""
    r = tables.runs
    rows = sql(db, select(*(r.c[name] for name in RUN_COLUMNS)).where(r.c.schedule_id.is_not(None)).order_by(r.c.id))
    return [dict(zip(RUN_COLUMNS, row, strict=True)) for row in rows]


def set_run(db, run_id: int, **values) -> None:
    sql(db, update(tables.runs).values(**values).where(tables.runs.c.id == run_id))


def audit_of(db, family: str) -> list[tuple]:
    a, u = tables.audit, tables.users
    query = (
        select(a.c.action, a.c.target, u.c.login, a.c.token_id)
        .join_from(a, u, u.c.id == a.c.actor_id)
        .where(a.c.action.like(f"{family}.%"))
        .order_by(a.c.id)
    )
    return sql(db, query)


def ended(client, worker: dict, run_id: int) -> None:
    """The worker claims run ``run_id``, starts it and fails it."""
    assert claim(client, worker)["id"] == run_id
    moved(client, worker, run_id, "running")
    assert report(client, worker, run_id, "failed", error="the agent gave up").status_code == 200


# The charter


def test_an_admin_writes_the_charter_and_every_revision_is_kept(night, hub_db):
    client, owner = night.client, night.headers["owner"]
    assert client.get(charter_path("charter"), headers=owner).status_code == 404
    first = written(client, owner, charter_body())  # a machine token
    assert {key: first[key] for key in ("project", "revision", "updated_by", "worker", "worker_id")} == {
        "project": PROJECT,
        "revision": 1,
        "updated_by": OWNER,
        "worker": WORKER,
        "worker_id": night.worker["id"],
    }
    assert first["schedule_owner"] == OWNER and first["auto_merge"] == [0] and first["judge"]["runtime"] == "codex"
    assert first["judge"]["hidden_checks"] == HIDDEN and first["circuit_breaker"] == {"max_failed_in_a_row": 2}
    assert first["reviewer"] == first["builder"] == {"runtime": "claude-code", "model": None}

    assert written(client, owner, charter_body())["revision"] == 1  # the same body writes nothing
    second = written(client, owner, charter_body(night_budget_usd=3.0, worker=WORKER.upper()))
    assert (second["revision"], second["night_budget_usd"], second["worker"]) == (2, 3.0, WORKER)

    assert shown(client, night.headers["reader"])["revision"] == 2
    assert shown(client, owner, revision=1)["night_budget_usd"] == 2.0
    assert client.get(charter_path("charter"), params={"revision": 3}, headers=owner).status_code == 404
    revisions = client.get(charter_path("charter", "revisions"), headers=owner).json()
    assert [(item["revision"], item["updated_by"], item["worker"]) for item in revisions] == [
        (2, OWNER, WORKER),
        (1, OWNER, WORKER),
    ]
    assert count(hub_db, tables.charters) == 2
    s = tables.schedules
    rows = sql(hub_db, select(s.c.kind, s.c.owner_id, s.c.worker_id, s.c.paused_at))
    assert rows == [("night_shift", user_id(hub_db, OWNER), night.worker["id"], None)]
    assert [row[:3] for row in audit_of(hub_db, "curator")] == [
        ("curator.charter", f"{PROJECT} revision=1 worker={WORKER}", OWNER),
        ("curator.charter", f"{PROJECT} revision=2 worker={WORKER}", OWNER),
    ]


def test_only_an_admin_of_the_project_changes_the_charter_and_a_worker_token_gets_403(night, hub_db):
    client, headers = night.client, night.headers
    for who, status in (("other", 403), ("reader", 403), ("admin", 403), ("stranger", 404)):
        response = put_charter(client, headers[who], charter_body())
        assert response.status_code == status, (who, response.text)
    response = put_charter(client, headers["other"], charter_body())
    assert "needs the admin role on it; you hold writer" in response.json()["message"]
    worker_token = {"Authorization": night.worker["headers"]["Authorization"]}
    response = put_charter(client, worker_token, charter_body())
    assert response.status_code == 403 and "a worker token works only on /v1/worker/" in response.json()["message"]
    assert client.get(charter_path("charter"), headers=worker_token).status_code == 403
    assert count(hub_db, tables.charters) == 0

    # an admin signed in on the web writes it too, with the session's CSRF header
    session = web_sign_in(client, night.github, Account(OWNER, GITHUB_IDS[OWNER]))
    web = {**cookie(session), "X-Evo-CSRF": csrf_for(client, session)}
    assert written(client, web, charter_body())["revision"] == 1
    assert put_charter(client, cookie(session), charter_body(night_budget_usd=4.0)).status_code == 403  # no CSRF


def test_the_charter_shows_the_judges_hidden_checks_to_admins_only(night, hub_db):
    client, headers = night.client, night.headers
    written(client, headers["owner"], charter_body())
    read = shown(client, headers["reader"])
    assert read["judge"] == {"runtime": "codex", "model": None, "hidden_checks": None}
    assert read["auto_merge"] == [0]
    assert shown(client, headers["owner"])["judge"]["hidden_checks"] == HIDDEN
    # null in a write keeps the checks: the body is the same, so no revision is written
    kept = written(client, headers["owner"], charter_body(judge={"runtime": "codex", "hidden_checks": None}))
    assert (kept["revision"], kept["judge"]["hidden_checks"]) == (1, HIDDEN)


def test_a_charter_shown_can_be_written_back_as_it_is(night, hub_db):
    client, owner = night.client, night.headers["owner"]
    written(client, owner, charter_body())
    again = written(client, owner, shown(client, owner))
    assert again["revision"] == 1


@pytest.mark.parametrize(
    "changes, status, words",
    [
        ({"window": {"start": "22:00", "end": "06:00", "timezone": "Mars/Olympus_Mons"}}, 422, "not a time zone"),
        ({"window": {"start": "22:00", "end": "22:00", "timezone": "UTC"}}, 422, "give it a length"),
        ({"window": {"start": "24:00", "end": "06:00", "timezone": "UTC"}}, 422, None),
        ({"run_budget_usd": 5.0}, 422, "run_budget_usd is over night_budget_usd"),
        ({"night_budget_usd": 0}, 422, None),
        ({"auto_merge": [1]}, 422, None),
        ({"run_minutes": 5}, 422, None),
        ({"goals": [{"id": "a", "what": "one"}, {"id": "a", "what": "two"}]}, 422, "named once each"),
        ({"surprise": True}, 422, None),
        ({"worker": "no-such-worker"}, 403, "you have no live worker no-such-worker"),
    ],
)
def test_a_charter_the_hub_does_not_take_writes_nothing(night, hub_db, changes, status, words):
    response = put_charter(night.client, night.headers["owner"], charter_body(**changes))
    assert response.status_code == status, response.text
    if words:
        assert words in response.text
    assert count(hub_db, tables.charters) == 0 and count(hub_db, tables.schedules) == 0


def test_a_charter_names_a_worker_of_its_writer_that_takes_runs_from_the_hub(night, hub_db):
    client, headers = night.client, night.headers
    fleet_worker(client, headers["other"], "other-mac")
    response = put_charter(client, headers["owner"], charter_body(worker="other-mac"))
    assert response.status_code == 403 and "you have no live worker other-mac" in response.text
    w = tables.workers
    sql(hub_db, update(w).values(dispatch_from="web").where(w.c.id == night.worker["id"]))
    response = put_charter(client, headers["owner"], charter_body())
    assert response.status_code == 409 and "could never hand it a run" in response.text
    assert count(hub_db, tables.charters) == 0


# The schedule


def test_the_schedule_queues_a_plan_run_inside_its_window_as_its_owner_on_its_worker(night, hub_db):
    client, headers = night.client, night.headers
    written(client, headers["owner"], charter_body())
    stranger = fleet_worker(client, headers["other"], "other-mac")  # the same checkouts, another member's

    assert fire(client, DAY) == {"outside": 1} and scheduled(hub_db) == []
    assert fire(client, NIGHT) == {"queued": 1}
    (run,) = scheduled(hub_db)
    assert {key: value for key, value in run.items() if key != "id"} == {
        "state": "queued",
        "kind": "plan",
        "plan_id": FLEET,
        "dispatched_by": user_id(hub_db, OWNER),
        "dispatched_via": "schedule",
        "pinned_worker_id": night.worker["id"],
        "runtime": "claude-code",
        "approval": "auto",
        "mode": "headless",
        "timeout_s": 30 * 60 + 300,
        "budget": {"max_usd": 0.5, "max_turns": 40, "max_seconds": 1800},
        "schedule_night": THE_NIGHT,
        "resume_of_run_id": None,
    }
    view = client.get(f"/v1/projects/{PROJECT}/runs/{run['id']}", headers=headers["reader"]).json()
    assert (view["dispatched_by"], view["dispatched_via"]) == (OWNER, "schedule")
    assert view["budget"] == {"max_usd": 0.5, "max_turns": 40, "max_seconds": 1800}
    e = tables.run_events
    (note,) = sql(hub_db, select(e.c.body).where(e.c.run_id == run["id"], e.c.kind == "system"))
    assert note[0]["text"].startswith(f"Queued by the night shift of project {PROJECT} (charter revision 1)")
    action, target, login, token_id = audit_of(hub_db, "curator")[-1]
    assert (action, login, token_id) == ("curator.dispatch", OWNER, None)  # for the owner, with no credential
    assert target.startswith(f"{PROJECT}/{FLEET} run:{run['id']} schedule:")
    assert target.endswith(f"night={THE_NIGHT.isoformat()}")

    # the rule holds: only a worker of the member it was dispatched by takes it, and only the one it is pinned to
    assert claim(client, stranger) is None
    spec = claim(client, night.worker)
    assert spec["id"] == run["id"] and spec["kind"] == "plan"


def test_the_schedule_queues_one_run_at_a_time_and_at_most_max_runs_a_night(night, hub_db):
    client, headers = night.client, night.headers
    written(client, headers["owner"], charter_body(max_runs_per_night=2))
    assert fire(client, NIGHT) == {"queued": 1}
    assert fire(client, NIGHT) == {"busy": 1}  # its run is queued
    first = scheduled(hub_db)[0]["id"]
    assert claim(client, night.worker)["id"] == first
    moved(client, night.worker, first, "running")
    assert fire(client, AFTER_MIDNIGHT) == {"busy": 1}  # and now held
    assert report(client, night.worker, first, "failed", error="the agent gave up").status_code == 200

    assert fire(client, AFTER_MIDNIGHT) == {"queued": 1}  # the same night, after midnight
    second = scheduled(hub_db)[1]
    assert second["schedule_night"] == THE_NIGHT
    ended(client, night.worker, second["id"])
    assert fire(client, AFTER_MIDNIGHT) == {"max_runs": 1}
    assert fire(client, NEXT_NIGHT) == {"queued": 1}  # a new night
    assert scheduled(hub_db)[2]["schedule_night"] == THE_NIGHT + timedelta(days=1)


def test_the_schedule_cancels_the_run_it_queued_once_its_window_ends(night, hub_db):
    client, headers = night.client, night.headers
    written(client, headers["owner"], charter_body())
    assert fire(client, NIGHT) == {"queued": 1}
    assert fire(client, DAY) == {"cancelled": 1}
    (run,) = scheduled(hub_db)
    assert run["state"] == "cancelled"
    assert claim(client, night.worker) is None
    assert fire(client, DAY) == {"outside": 1}


def test_the_schedule_passes_over_a_busy_plan_and_waits_for_an_offline_worker(night, hub_db):
    client, headers = night.client, night.headers
    written(client, headers["owner"], charter_body(night_plans=[PLAN, FLEET]))
    # rollout has a run of a step, dispatched by hand: the schedule takes fleet instead
    dispatched(client, headers["owner"], [2])
    assert fire(client, NIGHT) == {"queued": 1}
    assert scheduled(hub_db)[0]["plan_id"] == FLEET
    assert control(client, headers["owner"], scheduled(hub_db)[0]["id"], "cancel").status_code == 200

    w = tables.workers
    sql(hub_db, update(w).values(last_heartbeat_at=func.now() - timedelta(hours=1)).where(w.c.id == night.worker["id"]))
    assert fire(client, NIGHT) == {"worker": 1}
    assert len(scheduled(hub_db)) == 1


def test_the_schedule_queues_nothing_once_its_plans_have_no_ready_step(night, hub_db):
    client, headers = night.client, night.headers
    body = fleet_body()
    for step in body["steps"]:
        if step["status"] == "pending":
            step["status"] = "blocked"
    replaced = client.put(
        f"/v1/projects/{PROJECT}/plans/{FLEET}", json={"body": body, "if_revision": 1}, headers=headers["owner"]
    )
    assert replaced.status_code == 200, replaced.text
    written(client, headers["owner"], charter_body())
    assert fire(client, NIGHT) == {"no_plan": 1}
    assert scheduled(hub_db) == []


def test_the_fire_schedules_job_runs_every_minute():
    periodic = {p.task.name: p for p in queue.periodic_registry.periodic_tasks.values()}
    assert periodic[jobs.FIRE_SCHEDULES].cron == "* * * * *"
    assert queue.tasks[jobs.FIRE_SCHEDULES].queueing_lock == jobs.FIRE_SCHEDULES


# Pause and resume


def test_curator_pause_stops_every_schedule_and_resume_lets_them_run_again(night, hub_db):
    client, headers = night.client, night.headers
    response = client.post(charter_path("pause"), headers=headers["owner"])
    assert response.status_code == 409 and "has no schedule to pause" in response.text
    written(client, headers["owner"], charter_body())
    assert fire(client, NIGHT) == {"queued": 1}

    for who, code in (("other", 403), ("reader", 403), ("stranger", 404)):
        assert client.post(charter_path("pause"), headers=headers[who]).status_code == code, who
    paused = client.post(charter_path("pause"), headers=headers["owner"])
    assert paused.status_code == 200, paused.text
    found = paused.json()
    assert found["paused"] is True and found["schedules"][0]["paused_by"] == OWNER
    assert found["night"]["active_run_id"] is None
    (run,) = scheduled(hub_db)
    assert run["state"] == "cancelled"  # what it had queued goes at once
    assert fire(client, NIGHT) == {"paused": 1}  # and the job queues nothing
    assert scheduled(hub_db) == [run]
    again = client.post(charter_path("pause"), headers=headers["owner"]).json()
    assert again["schedules"][0]["paused_at"] == found["schedules"][0]["paused_at"]  # the first pause's time stays

    resumed = client.post(charter_path("resume"), headers=headers["owner"])
    assert resumed.status_code == 200 and resumed.json()["paused"] is False
    assert fire(client, NIGHT) == {"queued": 1}
    assert [row[:3] for row in audit_of(hub_db, "curator") if row[0] in ("curator.pause", "curator.resume")] == [
        ("curator.pause", PROJECT, OWNER),
        ("curator.pause", PROJECT, OWNER),
        ("curator.resume", PROJECT, OWNER),
    ]


# The night's cost and the caps of a run


def test_night_cost_counts_each_session_once_and_the_schedule_stops_at_the_nights_budget(night, hub_db):
    client, headers = night.client, night.headers
    written(client, headers["owner"], charter_body(night_budget_usd=1.0, run_budget_usd=None, max_runs_per_night=10))
    usages = [
        ({"total_cost_usd": 0.4, "input_tokens": 10}, "s-1"),
        ({"total_cost_usd": 0.35}, "s-2"),
        ({"total_cost_usd": 0.5}, "s-2"),  # the same session went on: its running total includes the 0.35
        ({"inputTokens": 900, "outputTokens": 40}, "t-4"),  # Codex: tokens, no cost
        ({"cost": 0.15}, "o-5"),  # opencode
    ]
    budgets = []
    for usage, session in usages:
        assert fire(client, NIGHT) == {"queued": 1}
        run = scheduled(hub_db)[-1]
        budgets.append(run["budget"]["max_usd"])
        ended(client, night.worker, run["id"])
        set_run(hub_db, run["id"], usage=usage, session_id=session)
    assert budgets == [1.0, 0.6, 0.25, 0.1, 0.1]
    assert fire(client, NIGHT) == {"spent": 1}

    schedule_id = sql(hub_db, select(tables.schedules.c.id))[0][0]

    async def figures():
        async with client.app.state.engine.connect() as conn:
            return await night_figures(conn, schedule_id, THE_NIGHT)

    found = client.portal.call(figures)
    assert (found.runs, round(found.cost_usd, 6), found.active_run_id) == (5, 1.05, None)
    night_now = status_of(client, headers["reader"])["night"]  # the night at the hub's own now, whichever it is
    assert (night_now["budget_usd"], night_now["max_runs"]) == (1.0, 10)


def test_a_scheduled_runs_budget_reaches_its_worker_with_what_a_parked_run_spent(night, hub_db):
    client, headers, worker = night.client, night.headers, night.worker
    written(client, headers["owner"], charter_body())
    assert fire(client, NIGHT) == {"queued": 1}
    run_id = scheduled(hub_db)[0]["id"]
    spec = claim(client, worker)
    assert spec["budget"] == {
        "max_usd": 0.5,
        "max_turns": 40,
        "max_seconds": 1800,
        "spent_usd": 0.0,
        "spent_seconds": 0,
    }

    moved(client, worker, run_id, "running", session_id=SESSION)
    reported(client, worker, run_id, 2, "in_progress")
    question = asked(client, worker, run_id)
    moved(client, worker, run_id, "waiting")
    set_run(hub_db, run_id, usage={"total_cost_usd": 0.12}, run_seconds=95)
    park(hub_db, run_id)
    recover(client)
    assert fire(client, DAY) == {"outside": 1}  # a parked run is neither queued nor held

    new_id = answered(client, headers["owner"], question["id"], option="postgres")["answer_run_id"]
    resumed = next(row for row in scheduled(hub_db) if row["id"] == new_id)
    assert (resumed["resume_of_run_id"], resumed["dispatched_via"], resumed["schedule_night"]) == (
        run_id,
        "schedule",
        THE_NIGHT,
    )
    assert resumed["budget"] == {"max_usd": 0.5, "max_turns": 40, "max_seconds": 1800}
    assert fire(client, DAY) == {"outside": 1}  # the owner's answer queued it: the schedule leaves it
    assert next(row for row in scheduled(hub_db) if row["id"] == new_id)["state"] == "queued"
    spec = claim(client, worker)
    assert spec["id"] == new_id
    assert spec["budget"] == {
        "max_usd": 0.5,
        "max_turns": 40,
        "max_seconds": 1800,
        "spent_usd": 0.12,
        "spent_seconds": 95,  # the agent time of the parked run, which the new one goes on counting from
    }


def test_a_run_dispatched_by_hand_has_no_budget(night, hub_db):
    client, headers = night.client, night.headers
    (run,) = dispatched(client, headers["owner"], [2])
    assert run["budget"] is None and run["dispatched_via"] == "machine"
    assert claim(client, night.worker)["budget"] is None


# Schema 0012


def test_schedule_columns_of_0012_hold_together(night, hub_db):
    client, headers = night.client, night.headers
    (run,) = dispatched(client, headers["owner"], [2])
    with pytest.raises(psycopg.errors.CheckViolation):  # dispatched by a schedule names it
        set_run(hub_db, run["id"], dispatched_via="schedule")
    written(client, headers["owner"], charter_body())
    schedule_id = sql(hub_db, select(tables.schedules.c.id))[0][0]
    with pytest.raises(psycopg.errors.CheckViolation):  # and only such a run does
        set_run(hub_db, run["id"], schedule_id=schedule_id, schedule_night=THE_NIGHT)
    with pytest.raises(psycopg.errors.CheckViolation):
        set_run(hub_db, run["id"], budget=[1, 2])
    s = tables.schedules
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(hub_db, update(s).values(paused_at=func.now()))  # paused_by goes with it


def test_schedule_migration_0012_goes_down_to_0011_and_up_again(night, hub_db):
    client, headers = night.client, night.headers
    written(client, headers["owner"], charter_body())
    assert fire(client, NIGHT) == {"queued": 1}
    run_id = scheduled(hub_db)[0]["id"]
    move_to(hub_db, "0011", down=True)
    r = tables.runs.c
    rows = sql(hub_db, select(r.dispatched_via).select_from(tables.runs).where(r.id == run_id))
    assert rows == [("machine",)]  # as dispatched with a token: the schema of 0011 has no schedule
    move_to(hub_db, "0012")
    assert count(hub_db, tables.charters) == 0 and count(hub_db, tables.schedules) == 0


# From the command line


@pytest.fixture
def served(hub_db, tmp_path, github):
    """The hub under uvicorn with night's project, plan and worker, and a home signed in for owner and reader."""
    import httpx

    app = create_app(live.hub_config(hub_db, tmp_path, github))
    with serving(app) as url, httpx.Client(base_url=url, timeout=10) as client:
        headers = members(client, github)
        grant(client, headers, OWNER, "admin")
        push(client, headers["owner"], fleet_body())
        fleet_worker(client, headers["owner"], WORKER)
        homes = {
            who: write_credentials(tmp_path / who, url, login, headers[who]["Authorization"].removeprefix("Bearer "))
            for who, login in (("owner", OWNER), ("reader", READER), ("other", OTHER))
        }
        yield SimpleNamespace(url=url, client=client, headers=headers, homes=homes, tmp_path=tmp_path)


def curator_cli(served, monkeypatch, capsys, who: str, *args: str, stdin: str | None = None):
    return cli(monkeypatch, capsys, served.homes[who], "hub", "curator", *args, "--project", PROJECT, stdin=stdin)


def test_charter_and_curator_pause_from_the_command_line(served, monkeypatch, capsys, hub_db):
    path = served.tmp_path / "charter.yaml"
    path.write_text(yaml.safe_dump(charter_body()), encoding="utf-8")
    printed = ok(curator_cli(served, monkeypatch, capsys, "owner", "charter", "set", str(path))).out
    assert f"Charter of {PROJECT} is at revision 1." in printed

    charter = as_json(curator_cli(served, monkeypatch, capsys, "reader", "charter", "show", "--json"))
    assert_json_keys("hub curator charter show", charter)
    assert charter["auto_merge"] == [0] and charter["judge"]["runtime"] == "codex"
    assert charter["judge"]["hidden_checks"] is None  # a reader's
    text = ok(curator_cli(served, monkeypatch, capsys, "owner", "charter", "show")).out
    assert "22:00 to 06:00, Asia/Ho_Chi_Minh" in text and "hidden checks  1" in text

    # what show --json printed goes back through set, from stdin, and writes nothing new
    shown_json = as_json(curator_cli(served, monkeypatch, capsys, "owner", "charter", "show", "--json"))
    stdin = yaml.safe_dump({**shown_json, "night_budget_usd": 3.0})
    again = as_json(curator_cli(served, monkeypatch, capsys, "owner", "charter", "set", "-", "--json", stdin=stdin))
    assert again["revision"] == 2
    history = as_json(curator_cli(served, monkeypatch, capsys, "owner", "charter", "history", "--json"))
    assert_json_keys("hub curator charter history", history)
    assert [item["revision"] for item in history] == [2, 1]

    refused = curator_cli(served, monkeypatch, capsys, "reader", "pause")
    assert refused.code == 1 and "needs the admin role on it, or one of its schedules" in refused.err
    paused = as_json(curator_cli(served, monkeypatch, capsys, "owner", "pause", "--json"))
    assert_json_keys("hub curator pause", paused)
    assert paused["paused"] is True
    status = as_json(curator_cli(served, monkeypatch, capsys, "reader", "status", "--json"))
    assert_json_keys("hub curator status", status)
    assert status["paused"] is True and status["charter"]["revision"] == 2
    resumed = ok(curator_cli(served, monkeypatch, capsys, "owner", "resume")).out
    assert f"The night shift of {PROJECT} runs again" in resumed and "Night shift of evo-agents: on" in resumed

    bad = served.tmp_path / "bad.yaml"
    bad.write_text("- just\n- a list\n", encoding="utf-8")
    refused = curator_cli(served, monkeypatch, capsys, "owner", "charter", "set", str(bad))
    assert refused.code == 1 and "holds no mapping" in refused.err

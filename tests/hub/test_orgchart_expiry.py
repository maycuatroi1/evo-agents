"""Seats that end (plan org-chart, criterion a5; ``evo_agents.hub.org``, ``evo_agents.hub.server.org_expiry``): the
hourly job ends the seats past their end date or that of their cohort, with the hub as the actor of the audit, and
running it again writes nothing; ``POST /v1/org/seats/{id}/end`` does the same at once. A person whose primary seat
ended is at A0: the leases still out of their runs, on every project, go back and their GitHub tokens are revoked, the
worker of such a run gets no new ones, and a run they dispatch is refused with 403 naming the unit and the day. What
their grant lets them read stays, and a person whose seat is live sees nothing change.

The model runs without Postgres; everything that needs the hub skips without EVO_HUB_TEST_DSN."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from evo_agents.hub import org
from evo_agents.hub.org import Chart, Seat, Unit
from tests.hub import pg

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
TODAY = date(2026, 10, 22)


def chart() -> Chart:
    """root > lab (team) > intern-2026 (cohort, ends 2026-10-20); lena leads lab, ivan interns in the cohort, mia's
    seat at the root ends 2026-10-21, and sam's seat at the root ended by hand on 2026-10-05."""
    units = {
        1: Unit(1, None, "root", "Organization", "org", ceiling="A5"),
        2: Unit(2, 1, "lab", "Lab", "team"),
        3: Unit(3, 2, "intern-2026", "Interns 2026", "cohort", ends_on=date(2026, 10, 20), ceiling="A1"),
    }
    seats = {
        10: Seat(10, 2, "lead", user_id=100, primary=True),
        11: Seat(11, 3, "intern", user_id=101, primary=True, reports_to=10),
        12: Seat(12, 1, "member", user_id=102, primary=True, ends_on=date(2026, 10, 21)),
        13: Seat(13, 1, "member", user_id=103, primary=True, ended_at=datetime(2026, 10, 5, 9, tzinfo=UTC)),
        14: Seat(14, 2, "member", user_id=101, primary=False),  # a secondary seat keeps nobody in office
    }
    return Chart(units, seats)


def test_orgchart_expiry_the_day_a_seat_ended_and_who_is_at_a0():
    c = chart()
    assert org.end_day(c, c.seats[11], TODAY) == date(2026, 10, 20), "the end of the cohort holding it"
    assert org.end_day(c, c.seats[12], TODAY) == date(2026, 10, 21)
    assert org.end_day(c, c.seats[13], TODAY) == date(2026, 10, 5)
    assert org.end_day(c, c.seats[10], TODAY) is None
    assert org.end_day(c, c.seats[12], date(2026, 10, 21)) is None, "its last day is still a day of it"
    assert [seat.id for seat in org.overdue(c, TODAY)] == [11, 12], "sam's has its ended_at already"
    assert org.out_of_office(c, TODAY) == [101, 102, 103]
    assert org.ended_primary(c, 100, TODAY) is None and org.ended_primary(c, 999, TODAY) is None
    assert org.ended_refusal(c, 101, "ivan", TODAY, what="dispatching a run") == (
        "the primary seat of ivan in unit intern-2026 ended on 2026-10-20, which leaves them at level A0: "
        "dispatching a run is refused"
    )
    assert org.ended_refusal(c, 100, "lena", TODAY, what="dispatching a run") is None
    # a seat ended by hand after its end date passed names the end date
    c.seats[12] = Seat(
        12, 1, "member", user_id=102, primary=True, ends_on=date(2026, 10, 21), ended_at=datetime.now(UTC)
    )
    assert org.end_day(c, c.seats[12], TODAY) == date(2026, 10, 21)


# The job queue is the hub's (the hub-server extra, procrastinate), which macOS and Windows do not install: like the
# other tests of the hub's periodic jobs, it runs where the hub tests do.
@needs_pg
def test_orgchart_expiry_the_job_runs_hourly_and_queues_at_most_one():
    from evo_agents.hub import jobs
    from evo_agents.hub.worker import queue

    periodic = {p.task.name: p for p in queue.periodic_registry.periodic_tasks.values()}
    assert periodic[jobs.EXPIRE_SEATS].cron == "5 * * * *"
    assert queue.tasks[jobs.EXPIRE_SEATS].queueing_lock == jobs.EXPIRE_SEATS


# On the hub

if pg.DSN:
    from sqlalchemy import func, select, update

    from evo_agents.hub import tables
    from evo_agents.hub.server.org_expiry import expire_seats
    from tests.hub.live import sql
    from tests.hub.test_credentials_api import (  # noqa: F401 (app_key, client, config and hub are fixtures)
        ADMIN,
        MINE,
        OTHER,
        OWNER,
        PLAN,
        PROJECT,
        app_key,
        ask,
        audit_rows,
        by_name,
        client,
        config,
        env_secret,
        hub,
        install,
        leased,
        leases_of,
        revoked_at_github,
        sample,
        step_run,
        worker_of,
    )


def seat_of(client, headers, login: str) -> dict:
    (seat,) = [
        s for s in client.get("/v1/org", headers=headers).json()["seats"] if s["login"] == login and s["primary"]
    ]
    return seat


def db_today(db) -> date:
    return sql(db, select(func.current_date()))[0][0]


def expire(client) -> dict:
    state = client.app.state
    return client.portal.call(expire_seats, state.engine, state.sealer, state.github_app)


def two_runs_with_leases(client, hub, github) -> tuple:
    """A run of owner and one of someone-else, each holding its leases: (owner's worker, run, GitHub token; theirs)."""
    install(github, "evo-agents")
    env_secret(client, hub["owner"], "claude-oauth", "CLAUDE_CODE_OAUTH_TOKEN", sample())
    env_secret(client, hub["other"], "their-oauth", "CLAUDE_CODE_OAUTH_TOKEN", sample())
    worker = worker_of(client, hub["owner"], "mac-mini", slots=2)
    run_id = step_run(client, hub["owner"], "evo-agents", worker)
    token = by_name(leased(client, worker, run_id))[f"github-app:{MINE}"]["value"]
    theirs = worker_of(client, hub["other"], "their-box")
    their_run = step_run(client, hub["other"], "notes", theirs)
    assert len(leased(client, theirs, their_run)["leases"]) == 1
    return worker, run_id, token, their_run


def dispatch(client, headers, step: int = 4):
    return client.post(f"/v1/projects/{PROJECT}/runs", json={"plan_id": PLAN, "steps": [step]}, headers=headers)


@needs_pg
def test_orgchart_expiry_ending_a_primary_seat_takes_back_the_leases_and_refuses_a_dispatch(
    client, hub, github, hub_db
):
    worker, run_id, token, their_run = two_runs_with_leases(client, hub, github)
    seat = seat_of(client, hub["admin"], OWNER)
    ended = client.post(f"/v1/org/seats/{seat['id']}/end", headers=hub["admin"])
    assert ended.status_code == 200, ended.text
    assert (ended.json()["effective"], ended.json()["source"]) == ("A0", f"seat:{seat['id']}")

    # The leases of owner's run go back at once, and GitHub revokes its token; someone-else's run keeps its own.
    assert [row[4] for row in leases_of(hub_db, run_id)] == [True, True]
    assert revoked_at_github(github) == [token]
    assert audit_rows(hub_db, "credential.revoke")[-1][0].endswith(" by=org-seat-end")
    assert audit_rows(hub_db, "credential.revoke")[-1][1] == ADMIN
    assert [row[4] for row in leases_of(hub_db, their_run)] == [False]

    day = db_today(hub_db)
    why = f"the primary seat of {OWNER} in unit root ended on {day}, which leaves them at level A0"
    refused = ask(client, worker["headers"], run_id)
    assert refused.status_code == 403 and refused.json()["message"] == f"{why}: leasing run {run_id} is refused"
    refused = dispatch(client, hub["owner"])
    assert refused.status_code == 403, refused.text
    assert refused.json()["message"] == f"{why}: dispatching a run is refused"
    plan_run = client.post(f"/v1/projects/{PROJECT}/plan-runs", json={"plan_id": PLAN}, headers=hub["owner"])
    assert plan_run.status_code == 403 and refused.json()["message"] == plan_run.json()["message"]

    # What owner's grant lets them read stays, and someone-else dispatches as before.
    assert client.get(f"/v1/projects/{PROJECT}/runs", headers=hub["owner"]).status_code == 200
    assert client.get(f"/v1/projects/{PROJECT}/plans/{PLAN}", headers=hub["owner"]).status_code == 200
    assert dispatch(client, hub["other"], 2).status_code == 201

    # A new primary seat puts owner back in office.
    added = client.post(
        "/v1/org/seats", json={"login": OWNER, "unit": "root", "role": "member", "primary": True}, headers=hub["admin"]
    )
    assert added.status_code == 201, added.text
    assert dispatch(client, hub["owner"], 3).status_code == 201


@needs_pg
def test_orgchart_expiry_the_hourly_job_ends_the_seats_of_a_cohort_past_its_end_once(client, hub, github, hub_db):
    worker, run_id, token, their_run = two_runs_with_leases(client, hub, github)
    cohort = {"slug": "intern-2026", "parent": "root", "kind": "cohort", "ends_on": "2099-12-31"}
    assert client.post("/v1/org/units", json=cohort, headers=hub["admin"]).status_code == 201
    body = {"login": OWNER, "unit": "intern-2026", "role": "member"}
    placed = client.post("/v1/org/seats", json=body, headers=hub["admin"])
    assert placed.status_code == 201 and placed.json()["primary"], placed.text
    assert expire(client) == {"ended": 0, "runs": 0}, "nothing ended yet"

    # The cohort's end passes: owner is at A0 before the job runs, and the job ends the seat and takes the leases.
    u = tables.org_units
    sql(hub_db, update(u).values(ends_on=func.current_date() - 2).where(u.c.slug == "intern-2026"))
    day = db_today(hub_db).toordinal() - 2
    ended_on = date.fromordinal(day)
    assert dispatch(client, hub["owner"]).json()["message"] == (
        f"the primary seat of {OWNER} in unit intern-2026 ended on {ended_on}, which leaves them at level A0: "
        "dispatching a run is refused"
    )
    assert expire(client) == {"ended": 1, "runs": 1}
    s = tables.org_seats
    ((ended_at,),) = sql(hub_db, select(s.c.ended_at).where(s.c.id == placed.json()["id"]))
    assert ended_at is not None
    seat_audit = audit_rows(hub_db, "org.seat_end")
    assert seat_audit == [
        (
            f"seat:{placed.json()['id']} unit=intern-2026 ended=none->now ends_on={ended_on} by=hub revision=3",
            None,
            None,
            False,
        )
    ], "no actor: the hub"
    assert [row[4] for row in leases_of(hub_db, run_id)] == [True, True]
    assert revoked_at_github(github) == [token]
    revoke = audit_rows(hub_db, "credential.revoke")
    assert revoke[-1][0].endswith(" by=org-expiry") and revoke[-1][1] is None
    assert [row[4] for row in leases_of(hub_db, their_run)] == [False]
    assert ask(client, worker["headers"], run_id).status_code == 403

    # Running it again writes nothing.
    assert expire(client) == {"ended": 0, "runs": 0}
    assert audit_rows(hub_db, "org.seat_end") == seat_audit
    assert audit_rows(hub_db, "credential.revoke") == revoke

"""The Curator's morning brief (``evo_agents.hub.server.brief``) and schemas 0015 and 0016.

The checks step 5 of the curator-agent plan names for a6: at the charter's brief_at, in the charter's time zone, the
owner of the night shift's schedule gets a notice curator_brief, once a day, on the web and on a channel of theirs (the
sample core plugin northwind-chat, ``tests.hub.northwind_channel``); it holds the
night's runs, the merges into a default branch and the runs waiting for approval, the cost against the budget, the
decisions and proposals waiting, and the last heartbeat of the worker on duty, so a worker that stopped shows; and
`evo-agents hub curator status --json` names the last brief under ``last_brief``, which steps 13 and 15 and a16 read.
Then the constraints of schema 0015, and its way down to 0014 and up again; and schema 0016, by which a brief
outlives its notification, and its way down to 0015 and up again."""

from datetime import UTC, date, datetime, timedelta
from functools import partial

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import psycopg
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, insert, select, update

from evo_agents.hub import curator, jobs, tables
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.brief import send_briefs, send_loop_notices
from evo_agents.hub.worker import queue
from tests.hub.contract_keys import assert_json_keys
from tests.hub.live import sql
from tests.hub.northwind_channel import (
    cleared,
    deliver,
    hub_config,
    linked,
    northwind,  # noqa: F401 (a fixture)
    set_up,
)
from tests.hub.test_curator import (
    NIGHT,
    OWNER,
    PROJECT,
    THE_NIGHT,
    WORKER,
    charter_body,
    fire,
    night,  # noqa: F401 (a fixture, on this module's web_client)
    scheduled,
    set_run,
    written,
)
from tests.hub.test_decisions import QUESTION, asked
from tests.hub.test_migrate import HEAD, columns, move_to
from tests.hub.test_review_runs import (
    held_review,
    proposed,
    review,  # noqa: F401 (a fixture)
    served,  # noqa: F401 (a fixture)
)
from tests.hub.test_run_cli import as_json, cli, ok
from tests.hub.test_runs import SHA, claim, moved, report

# Asia/Ho_Chi_Minh is UTC+7: the charter's brief_at 06:30 there is 23:30 UTC the evening before.
BRIEF = datetime(2026, 10, 8, 23, 30, tzinfo=UTC)  # 06:30 local on 2026-10-09, after the night of 2026-10-08
THE_DAY = date(2026, 10, 9)
MINUTE = timedelta(minutes=1)
BRIEFS = tables.curator_briefs


@pytest.fixture
def web_client(hub_db, tmp_path, github, northwind):  # noqa: F811
    """The hub test_curator's fixtures build on, with the sample core plugin northwind-chat installed."""
    with TestClient(create_app(hub_config(hub_db, tmp_path, github)), base_url="https://hub.test") as client:
        yield client


def briefed(client, now: datetime) -> dict:
    return client.portal.call(partial(send_briefs, client.app.state.engine, now=now))


def briefs_of(client, headers) -> list[dict]:
    query = {"kind": "notice"}
    found = client.get("/v1/me/notifications", params=query, headers=headers).json()["notifications"]
    return [item for item in found if item["notice_kind"] == "curator_brief"]


def body_of(db, day: date = THE_DAY) -> dict:
    ((body,),) = sql(db, select(BRIEFS.c.body).where(BRIEFS.c.day == day))
    return body


def user_id(db, login: str) -> int:
    return sql(db, select(tables.users.c.id).where(tables.users.c.login == login))[0][0]


def test_brief_due_from_brief_at_for_three_hours_of_the_local_day():
    at = datetime(2026, 10, 9, 6, 30)
    assert not curator.brief_due(at - MINUTE, "06:30")
    assert curator.brief_due(at, "06:30") and curator.brief_due(at + timedelta(hours=2, minutes=59), "06:30")
    assert not curator.brief_due(at + timedelta(hours=3), "06:30")
    assert curator.brief_due(datetime(2026, 10, 9, 0, 0), "00:00")


def test_the_brief_goes_out_at_brief_at_in_the_charters_time_zone_once_a_day(night, hub_db):  # noqa: F811
    client, headers = night.client, night.headers
    written(client, headers["owner"], charter_body())
    assert briefed(client, BRIEF - MINUTE) == {"not_due": 1}  # 06:29 in Ho Chi Minh City
    assert briefed(client, BRIEF) == {"brief": 1}
    assert briefed(client, BRIEF + MINUTE) == {"sent": 1}
    assert briefed(client, BRIEF + timedelta(hours=2, minutes=59)) == {"sent": 1}
    assert briefed(client, BRIEF + timedelta(hours=3)) == {"not_due": 1}
    (note,) = briefs_of(client, headers["owner"])
    assert note["title"].startswith(f"Morning brief of {PROJECT}: 0 runs, $0.00 of $2.00")
    assert (note["project"], note["link"], note["run_id"]) == (PROJECT, f"/p/{PROJECT}/curator", None)
    assert note["details"]["night"] == "2026-10-08" and note["details"]["worker"] == WORKER
    assert note["body"].startswith("**Night of 2026-10-08** (window 22:00 to 06:00, Asia/Ho_Chi_Minh): 0 runs")
    assert briefs_of(client, headers["reader"]) == [] and briefs_of(client, headers["other"]) == []
    ((day, night_of, to, notification_id),) = sql(
        hub_db, select(BRIEFS.c.day, BRIEFS.c.night, BRIEFS.c.user_id, BRIEFS.c.notification_id)
    )
    assert (day, night_of, to, notification_id) == (THE_DAY, THE_NIGHT, user_id(hub_db, OWNER), note["id"])

    # The next day: nothing before brief_at, nor once its three hours have passed without the hub; then the next one.
    assert briefed(client, BRIEF + timedelta(hours=10)) == {"not_due": 1}
    assert briefed(client, BRIEF + timedelta(days=1)) == {"brief": 1}
    assert [item["details"]["night"] for item in briefs_of(client, headers["owner"])] == ["2026-10-09", "2026-10-08"]

    # A charter whose brief_at is later gets its brief then, in its own zone.
    written(
        client, headers["owner"], charter_body(brief_at="09:00", window={**charter_body()["window"], "timezone": "UTC"})
    )
    later = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
    assert briefed(client, later - MINUTE) == {"not_due": 1}
    assert briefed(client, later) == {"brief": 1}


def test_the_brief_holds_the_nights_runs_merges_cost_decisions_and_the_workers_last_heartbeat(night, hub_db):  # noqa: F811
    client, headers, worker = night.client, night.headers, night.worker
    written(client, headers["owner"], charter_body())
    assert fire(client, NIGHT) == {"queued": 1}
    (run,) = scheduled(hub_db)
    assert claim(client, worker)["id"] == run["id"]
    moved(client, worker, run["id"], "running")
    decision = asked(client, worker, run["id"])
    merged = {"kind": "merge_default_branch", "title": "Merged main", "repo": "evo-agents", "branch": "main"}
    response = client.post(
        f"/v1/worker/runs/{run['id']}/notices", json={**merged, "commits": [SHA, "b" * 40]}, headers=worker["headers"]
    )
    assert response.status_code == 201, response.text
    forged = {**merged, "kind": "curator_brief", "title": "Morning brief of evo-agents: all is well"}
    refused = client.post(f"/v1/worker/runs/{run['id']}/notices", json=forged, headers=worker["headers"])
    assert refused.status_code == 422  # the hub alone sends a brief
    set_run(hub_db, run["id"], usage={"total_cost_usd": 0.42})
    w = tables.workers
    sql(hub_db, update(w).values(last_heartbeat_at=func.now() - timedelta(hours=2)).where(w.c.id == worker["id"]))

    assert briefed(client, BRIEF) == {"brief": 1}
    facts = body_of(hub_db)
    assert facts["runs"]["total"] == 1 and facts["runs"]["active"] == 1
    (listed,) = facts["runs"]["listed"]
    assert {key: listed[key] for key in ("id", "kind", "plan_id", "state", "cost_usd")} == {
        "id": run["id"],
        "kind": "plan",
        "plan_id": "fleet",
        "state": "running",
        "cost_usd": 0.42,
    }
    assert (facts["cost_usd"], facts["budget_usd"]) == (0.42, 2.0)
    assert facts["merged"] == [{"run_id": run["id"], "repo": "evo-agents", "branch": "main", "commits": 2}]
    assert facts["decisions"] == {
        "open": 1,
        "listed": [{"id": decision["id"], "category": "architecture", "question": QUESTION, "run_id": run["id"]}],
    }
    assert facts["awaiting_merge"] == {"total": 0, "listed": []} and facts["review_run"] is None
    assert facts["worker"]["name"] == WORKER and facts["worker"]["online"] is False
    assert facts["worker"]["last_heartbeat_local"] is not None

    (note,) = briefs_of(client, headers["owner"])
    assert note["title"].endswith(f"1 run, $0.42 of $2.00, 1 decision and 0 proposals waiting, worker {WORKER} offline")
    assert "$0.42 of $2.00" in note["title"]
    body = note["body"]
    assert f"- run #{run['id']} (fleet): running, $0.42" in body
    assert f"Merged into a default branch: evo-agents main (2 commits, run #{run['id']})." in body
    assert f"- decision #{decision['id']} (architecture, run #{run['id']}): {QUESTION}" in body
    # Markdown: a paragraph a subject, each list after a blank line, so the web renders them apart.
    assert f"\n\nDecisions waiting for your answer: 1.\n\n- decision #{decision['id']}" in body
    assert (
        f"Worker {WORKER}: offline, last heartbeat {facts['worker']['last_heartbeat_local']} Asia/Ho_Chi_Minh." in body
    )
    assert note["details"]["worker_online"] is False and note["details"]["open_decisions"] == 1


def test_the_brief_names_the_review_run_and_the_proposals_waiting(review):  # noqa: F811
    client, worker, db, headers = review.client, review.worker, review.db, review.headers
    run_id = held_review(review)
    made = proposed(client, worker, run_id, kind="feature", title="A wait helper instead of sleep and tail")
    for state in ("verifying", "done"):
        assert report(client, worker, run_id, state).status_code == 200
    assert briefed(client, BRIEF) == {"brief": 1}
    facts = body_of(db)
    assert facts["review_run"] == {"id": run_id, "state": "done", "findings": 0, "proposals": 1}
    assert facts["proposals"] == {
        "open": 1,
        "in_inbox": 1,
        "listed": [{"id": made["id"], "tier": 2, "title": "A wait helper instead of sleep and tail", "evidence": 1}],
    }
    assert facts["runs"]["total"] == 1 and facts["runs"]["done"] == 1
    (note,) = briefs_of(client, headers["owner"])
    assert f"Review run #{run_id}: done, 0 findings, 1 proposal." in note["body"]
    assert "Proposals waiting for an answer: 1, 1 of them in your Inbox." in note["body"]


def test_the_brief_has_the_loops_part_once_the_projects_loop_is_on_and_none_while_it_is_off(night, hub_db):  # noqa: F811
    client, headers = night.client, night.headers
    written(client, headers["owner"], charter_body())
    assert briefed(client, BRIEF) == {"brief": 1}
    assert "loop" not in body_of(hub_db) and "**Loop**" not in briefs_of(client, headers["owner"])[0]["body"]

    # the Loop on: a run handed out within the 24 hours before the next brief, one before them, a work item waiting
    pid = sql(hub_db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    sql(hub_db, insert(tables.loop_states).values(project_id=pid, enabled=True, enabled_at=func.now()))
    i, e = tables.loop_items, tables.loop_item_events
    work = {"project_id": pid, "kind": "deliver", "cycle": "development", "key": "deliver:fleet", "engine": "rules/1"}
    ((held,),) = sql(hub_db, insert(i).values(**work, state="waiting_human", reason="merge_blocked").returning(i.c.id))
    morning = BRIEF + timedelta(days=1)
    ledger = [
        {"item_id": held, "kind": "dispatched", "state": "running", "at": morning - timedelta(hours=5)},
        {"item_id": held, "kind": "dispatched", "state": "running", "at": morning - timedelta(hours=25)},
    ]
    sql(hub_db, insert(e).values(ledger))
    assert briefed(client, morning) == {"brief": 1}
    facts = body_of(hub_db, THE_DAY + timedelta(days=1))
    assert facts["loop"] == {
        "handed_out": {"deliver": 1},
        "done": {},
        "waiting": {"merge_blocked": 1},
        "listed": [{"id": held, "kind": "deliver", "reason": "merge_blocked"}],
        "breaker": "armed",
    }
    note = briefs_of(client, headers["owner"])[0]
    assert note["details"]["loop"] == {"handed_out": 1, "done": 0, "waiting": 1}
    loop = (
        "**Loop**: 1 run handed out and 0 work items done in the last 24 hours; 1 waiting for a person "
        "(1 a merge request held)."
    )
    assert f"\n\n{loop}\n\n- work item #{held} (deliver): a merge request held\n\n" in note["body"]
    assert f"Decide on the decision board: /p/{PROJECT}/decide.\n\nWorker {WORKER}" in note["body"]

    # the Curator sends this project's brief, which carries the Loop's part: the owner of its goal gets no brief of the
    # Loop of its own at 07:00
    g, r, owner = tables.goals, tables.goal_revisions, user_id(hub_db, OWNER)
    ((goal,),) = sql(hub_db, insert(g).values(project_id=pid, number=1, revision=1).returning(g.c.id))
    goal_row = {"goal_id": goal, "revision": 1, "statement": "Fewer nights wasted", "rank": 1, "status": "active"}
    sql(hub_db, insert(r).values(**goal_row, owner_id=owner, created_by=owner))
    assert client.portal.call(partial(send_loop_notices, client.app.state.engine, now=morning + MINUTE * 30)) == {}


def test_the_brief_goes_to_the_owners_channel_and_a_restricted_project_gets_the_link_only(
    night,  # noqa: F811
    hub_db,
    northwind,  # noqa: F811
):
    client, headers = night.client, night.headers
    written(client, headers["owner"], charter_body())
    set_up(client, headers["admin"])
    cleared(hub_db)
    linked(hub_db, OWNER)
    assert briefed(client, BRIEF) == {"brief": 1}
    assert deliver(client) == {"delivered": 2, "retried": 0, "failed": 0}  # the web's, and the channel's
    link = f"/p/{PROJECT}/curator"
    url = f"https://hub.test{link}"
    (sent,) = northwind.outgoing
    assert (sent.login, sent.notice_kind, sent.project, sent.link) == (OWNER, "curator_brief", PROJECT, link)
    assert sent.extra == {"restricted": False} and "Night of 2026-10-08" in sent.body
    (message,) = northwind.chat.messages
    assert message["text"].startswith(f"Morning brief of {PROJECT}") and message["text"] == f"{sent.title}: {url}"

    cleared(hub_db, {"level": "customer"})
    assert briefed(client, BRIEF + timedelta(days=1)) == {"brief": 1}
    deliver(client)
    assert northwind.outgoing[-1].extra == {"restricted": True}
    assert northwind.chat.last()["text"] == f"Something of {PROJECT} waits for you: {url}"


def test_no_brief_goes_to_an_owner_without_a_grant_and_the_job_runs_every_minute(night, hub_db):  # noqa: F811
    client, headers = night.client, night.headers
    written(client, headers["owner"], charter_body())
    g = tables.grants
    sql(hub_db, delete(g).where(g.c.user_id == user_id(hub_db, OWNER)))
    assert briefed(client, BRIEF) == {"owner": 1}
    assert sql(hub_db, select(func.count()).select_from(BRIEFS)) == [(0,)]
    periodic = {task.task.name: task for task in queue.periodic_registry.periodic_tasks.values()}
    assert periodic[jobs.CURATOR_BRIEF].cron == "* * * * *"
    assert queue.tasks[jobs.CURATOR_BRIEF].queueing_lock == jobs.CURATOR_BRIEF == "curator.brief"


def test_curator_status_names_the_last_brief_on_the_api_and_the_command_line(served, monkeypatch, capsys):  # noqa: F811
    path = f"/v1/projects/{PROJECT}/curator"
    assert served.client.get(path, headers=served.headers["reader"]).json()["last_brief"] is None
    printed = ok(cli(monkeypatch, capsys, served.homes["reader"], "hub", "curator", "status", "--project", PROJECT)).out
    assert "no morning brief yet: the first goes out at 06:30 in Asia/Ho_Chi_Minh" in printed
    with TestClient(create_app(served.config)) as jobs_side:
        assert jobs_side.portal.call(partial(send_briefs, jobs_side.app.state.engine, now=BRIEF)) == {"brief": 1}
    status = served.client.get(path, headers=served.headers["reader"]).json()
    last = status["last_brief"]
    assert (last["day"], last["night"], last["to"]) == ("2026-10-09", "2026-10-08", OWNER)
    assert last["title"].startswith(f"Morning brief of {PROJECT}") and isinstance(last["notification_id"], int)

    shown = as_json(
        cli(monkeypatch, capsys, served.homes["reader"], "hub", "curator", "status", "--project", PROJECT, "--json")
    )
    assert_json_keys("hub curator status", shown)
    assert shown["charter"] is not None and shown["last_brief"]["day"] == "2026-10-09"
    assert "last_review_run" in shown
    printed = ok(cli(monkeypatch, capsys, served.homes["reader"], "hub", "curator", "status", "--project", PROJECT)).out
    assert f"last brief: 2026-10-09 (night of 2026-10-08) to {OWNER}" in printed


# Schema 0015


def test_the_columns_of_0015_hold_together(night, hub_db):  # noqa: F811
    client, headers = night.client, night.headers
    written(client, headers["owner"], charter_body())
    assert briefed(client, BRIEF) == {"brief": 1}
    with pytest.raises(psycopg.errors.UniqueViolation):  # one brief a project and day
        sql(
            hub_db,
            insert(BRIEFS).from_select(
                ["project_id", "day", "night", "user_id", "body"],
                select(BRIEFS.c.project_id, BRIEFS.c.day, BRIEFS.c.night, BRIEFS.c.user_id, BRIEFS.c.body),
            ),
        )
    with pytest.raises(psycopg.errors.CheckViolation):  # a brief reports on a night before its day
        sql(hub_db, update(BRIEFS).values(night=BRIEFS.c.day + 1))
    n = tables.notifications
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(hub_db, update(n).values(notice_kind="curator_gossip").where(n.c.notice_kind == "curator_brief"))
    links = tables.channel_link_codes  # telegram_links, whose checks 0063 kept
    owner = user_id(hub_db, OWNER)
    for bad in (
        {"code_hash": "A" * 64},
        {"code_hash": "a" * 63},
        {"expires_at": func.now() + timedelta(minutes=11)},
        {"expires_at": func.now()},
        {"kind": "web"},  # a code links a kind of members' channel (0063), never the web
        {"kind": "Not a kind"},
    ):
        values = {"user_id": owner, "kind": "telegram", "code_hash": "a" * 64, **bad}
        values.setdefault("expires_at", func.now() + timedelta(minutes=10))
        with pytest.raises(psycopg.errors.CheckViolation):
            sql(hub_db, insert(links).values(**values))
    d = tables.notification_deliveries
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(hub_db, update(d).values(external_id=""))


def test_migration_0015_goes_down_to_0014_and_up_again(night, hub_db, northwind):  # noqa: F811
    client, headers = night.client, night.headers
    written(client, headers["owner"], charter_body())
    owner = user_id(hub_db, OWNER)
    channels = tables.notification_channels
    set_up(client, headers["admin"])
    linked(hub_db, OWNER)
    sql(
        hub_db,
        insert(tables.channel_link_codes).values(
            user_id=owner, kind="telegram", code_hash="b" * 64, expires_at=func.now() + timedelta(minutes=10)
        ),
    )
    assert briefed(client, BRIEF) == {"brief": 1}
    deliver(client)
    d = tables.notification_deliveries
    assert sql(hub_db, select(func.count()).select_from(d).where(d.c.external_id.is_not(None))) == [(1,)]
    move_to(hub_db, "0014", down=True)
    move_to(hub_db, HEAD)  # up again to the head the code runs on (0017 added what the brief reads of the Curator)
    n = tables.notifications
    assert sql(hub_db, select(func.count()).select_from(n).where(n.c.kind == "notice")) == [(0,)]
    assert sql(hub_db, select(func.count()).select_from(BRIEFS)) == [(0,)]
    assert sql(hub_db, select(func.count()).select_from(tables.channel_link_codes)) == [(0,)]
    assert sql(hub_db, select(d.c.external_id)) == []
    assert sql(hub_db, select(func.count()).select_from(channels)) == [(1,)]  # a member's link survives
    assert briefed(client, BRIEF + timedelta(days=1)) == {"brief": 1}


# Schema 0016


def test_a_brief_outlives_its_notification_and_0016_goes_down_to_0015_and_up_again(night, hub_db):  # noqa: F811
    client, headers = night.client, night.headers
    written(client, headers["owner"], charter_body())
    assert briefed(client, BRIEF) == {"brief": 1}
    n = tables.notifications
    ((first,),) = sql(hub_db, select(BRIEFS.c.notification_id))
    sql(hub_db, delete(n).where(n.c.id == first))  # ON DELETE SET NULL: the brief stays, without its notification
    assert sql(hub_db, select(BRIEFS.c.day, BRIEFS.c.notification_id)) == [(THE_DAY, None)]

    # the second brief is sent at the head the code runs on (0017 added what the brief reads of the Curator); 0015's
    # key is the constraint's, whatever revision wrote the row
    assert briefed(client, BRIEF + timedelta(days=1)) == {"brief": 1}
    second_day = BRIEFS.c.day == THE_DAY + timedelta(days=1)
    ((second,),) = sql(hub_db, select(BRIEFS.c.notification_id).where(second_day))
    move_to(hub_db, "0015", down=True)
    assert "token_id" not in columns(hub_db, "notification_channels") | columns(hub_db, "telegram_links")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):  # 0015's key refused the delete
        sql(hub_db, delete(n).where(n.c.id == second))

    move_to(hub_db, "0016")
    assert "token_id" in columns(hub_db, "notification_channels") & columns(hub_db, "telegram_links")
    sql(hub_db, delete(n).where(n.c.id == second))
    assert sql(hub_db, select(func.count()).select_from(BRIEFS).where(BRIEFS.c.notification_id.is_(None))) == [(2,)]

"""Cron jobs of a project (``docs/hub.md``, Cron jobs; plan hub-cron-jobs, criterion a1): ``evo_agents.hub.cron``, the
routes of ``evo_agents.hub.server.cron_jobs`` and ``evo-agents hub cron``.

Without a database: the helper validates five fields in an IANA zone, refuses aliases, crons that never fire and
crons finer than the limit of their kind, and finds the next fire in the job's zone; ``hub cron set`` merges the flags
over the job it read. With a test database (``EVO_HUB_TEST_DSN``): a writer creates and changes a job by revision, with
409 ``revision_conflict``; validate refusals are 422; the roles: a reader reads, a writer manages only their own, an
admin pauses, runs and deletes another's but does not change its work; run now records a fire at the press.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("sqlalchemy", reason="the hub tables need the hub-server extra, evo-ak[hub-server]")
pytest.importorskip("croniter", reason="the cron helper needs the hub-server extra, evo-ak[hub-server]")

from evo_agents.hub import cli_cron, cron
from tests.hub import pg

# The helper


@pytest.mark.parametrize(
    "expr",
    ["* * * *", "0 9 * * * *", "@daily", "H 9 * * *", "61 * * * *", "0 25 * * *", "a b c d e", "0 0 30 2 *"],
)
def test_validate_refuses_a_cron_that_does_not_read_or_never_fires(expr):
    with pytest.raises(cron.CronRefused):
        cron.check(expr, "UTC", "command")


def test_validate_refuses_an_unknown_time_zone():
    with pytest.raises(cron.CronRefused, match="Mars/Olympus"):
        cron.check("0 9 * * *", "Mars/Olympus", "agent")
    with pytest.raises(cron.CronRefused):
        cron.zone("../etc/passwd")


@pytest.mark.parametrize(
    ("expr", "kind", "taken"),
    [
        ("*/15 * * * *", "agent", True),
        ("*/10 * * * *", "agent", False),
        ("0,10 9 * * *", "plan", False),  # 10 minutes apart once a day is still too close
        ("*/10 * * * *", "command", True),
        ("*/5 * * * *", "command", True),
        ("*/4 * * * *", "command", False),
        ("* * * * *", "command", False),
        ("0 9 * * 1-5", "agent", True),
    ],
)
def test_validate_holds_each_kind_to_its_shortest_gap(expr, kind, taken):
    if taken:
        assert cron.check(expr, "UTC", kind) == expr
    else:
        with pytest.raises(cron.CronRefused, match=f"every {cron.MIN_MINUTES[kind]} minutes"):
            cron.check(expr, "UTC", kind)


def test_the_next_fire_is_read_in_the_jobs_time_zone():
    after = datetime(2026, 11, 12, 0, 0, tzinfo=UTC)
    assert cron.next_fire("0 9 * * *", "Asia/Ho_Chi_Minh", after) == datetime(2026, 11, 12, 2, 0, tzinfo=UTC)
    assert cron.next_fire("0 9 * * *", "UTC", after) == datetime(2026, 11, 12, 9, 0, tzinfo=UTC)


# The CLI


class FakeHub:
    def __init__(self, held: dict | None):
        self.held, self.calls = held, []

    def call(self, method, path, body=None):
        from evo_agents.hub.client import HubError

        self.calls.append((method, path, body))
        if method == "GET":
            if self.held is None:
                raise HubError("no such job", status=404)
            return self.held
        return {**(body or {}), "revision": 2, "created": False, "changed": True, "paused": False}


def test_set_keeps_what_it_is_not_given_and_sends_the_revision_it_read(monkeypatch):
    from evo_agents.cli import main

    held = {"kind": "agent", "cron": "0 9 * * *", "timezone": "Europe/Paris", "revision": 4}
    held |= {"agent": "lumen-pm", "chat_id": None, "prompt": "Standup"}
    hub = FakeHub(held)
    monkeypatch.setattr(cli_cron, "signed_in", lambda: (hub, None))
    assert main(["hub", "cron", "set", "standup", "--project", "northwind", "--cron", "0 10 * * *"]) == 0
    method, path, body = hub.calls[-1]
    assert (method, path) == ("PUT", "/v1/projects/northwind/cron-jobs/standup")
    assert body["cron"] == "0 10 * * *" and body["timezone"] == "Europe/Paris"
    assert (body["agent"], body["prompt"], body["if_revision"]) == ("lumen-pm", "Standup", 4)


def test_set_refuses_flags_of_two_kinds(monkeypatch):
    from evo_agents.cli import main

    hub = FakeHub(None)
    monkeypatch.setattr(cli_cron, "signed_in", lambda: (hub, None))
    argv = [
        "hub",
        "cron",
        "set",
        "x",
        "--project",
        "northwind",
        "--cron",
        "0 9 * * *",
        "--plan",
        "p",
        "--command",
        "ls",
    ]
    assert main(argv) == 2
    assert [c[0] for c in hub.calls] == ["GET"]


# The API

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
AGENT = "lumen-pm"


@pytest.fixture
def client(hub_db, tmp_path, github, s3):
    from fastapi.testclient import TestClient

    from evo_agents.hub.server.app import create_app
    from tests.hub import live

    config = live.hub_config(hub_db, tmp_path, github, **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as found:
        yield found


@pytest.fixture
def hub(client, github):
    """The members of ``tests.hub.test_runs`` (owner and other writers, reader a reader) with ``admin`` an admin of
    the project (their stranger, granted it), and agent AGENT, made by that admin."""
    from tests.hub.test_runs import PROJECT, STRANGER, members

    headers = members(client, github)
    grant = {"role": "admin", "max_level": "internal"}
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{STRANGER}", json=grant, headers=headers["admin"])
    assert granted.status_code == 200, granted.text
    admin = headers["stranger"]
    made = client.post(f"/v1/projects/{PROJECT}/agents", json={"handle": AGENT, "name": AGENT}, headers=admin)
    assert made.status_code in (200, 201), made.text
    return {**headers, "admin": admin, "hub_admin": headers["admin"], "project": PROJECT}


def path(hub, name: str | None = None) -> str:
    base = f"/v1/projects/{hub['project']}/cron-jobs"
    return base if name is None else f"{base}/{name}"


def agent_job() -> dict:
    return {"cron": "0 9 * * 1-5", "timezone": "Asia/Ho_Chi_Minh", "kind": "agent", "agent": AGENT, "prompt": "Standup"}


@needs_pg
def test_a_writer_creates_and_changes_a_job_by_revision(client, hub, hub_db):
    from tests.hub.test_runs import audit_rows

    made = client.put(path(hub, "standup"), json=agent_job(), headers=hub["owner"])
    assert made.status_code == 200, made.text
    job = made.json()
    assert (job["revision"], job["created"], job["owner"], job["agent"]) == (1, True, "owner", AGENT)
    assert job["next_fire_at"] is not None and job["cost_24h_usd"] == 0
    again = client.put(path(hub, "standup"), json=agent_job(), headers=hub["owner"])
    assert again.status_code == 409 and again.json()["error"] == "revision_conflict"
    same = client.put(path(hub, "standup"), json={**agent_job(), "if_revision": 1}, headers=hub["owner"])
    assert same.status_code == 200 and same.json()["changed"] is False
    changed = client.put(
        path(hub, "standup"), json={**agent_job(), "cron": "0 10 * * *", "if_revision": 1}, headers=hub["owner"]
    )
    assert changed.status_code == 200 and changed.json()["revision"] == 2
    stale = client.put(
        path(hub, "standup"), json={**agent_job(), "cron": "0 11 * * *", "if_revision": 1}, headers=hub["owner"]
    )
    assert stale.status_code == 409
    assert stale.json()["error"] == "revision_conflict" and stale.json()["current"]["revision"] == 2
    assert [row[0] for row in audit_rows(hub_db, "cron")] == ["cron.create", "cron.update"]


@needs_pg
@pytest.mark.parametrize(
    "body",
    [
        {"cron": "0 9 * *"},
        {"cron": "@daily"},
        {"timezone": "Mars/Olympus"},
        {"cron": "*/10 * * * *"},  # an agent job every 10 minutes
        {"agent": "lumen-nobody"},
        {"plan": "rollout"},  # a field of another kind
        {"kind": "command", "agent": None, "prompt": None, "command": "make test", "cron": "*/2 * * * *"},
        {"kind": "command", "agent": None, "prompt": None, "command": "make test", "repo": "nowhere"},
        {"kind": "plan", "agent": None, "prompt": None, "plan": "no-such-plan"},
    ],
)
def test_validate_refusals_are_422_and_write_nothing(client, hub, body):
    refused = client.put(path(hub, "standup"), json={**agent_job(), **body}, headers=hub["owner"])
    assert refused.status_code == 422, refused.text
    assert client.get(path(hub), headers=hub["owner"]).json() == []


@needs_pg
def test_validate_takes_a_command_job_every_five_minutes_and_a_plan_job(client, hub):
    from tests.hub.test_plans import REPOS

    command = {"cron": "*/5 * * * *", "kind": "command", "command": "make test", "repo": REPOS[0]}
    made = client.put(path(hub, "tests"), json=command, headers=hub["owner"])
    assert made.status_code == 200, made.text
    assert (made.json()["timeout_minutes"], made.json()["repo"]) == (30, REPOS[0])
    plan = client.put(
        path(hub, "rollout"), json={"cron": "0 3 * * *", "kind": "plan", "plan": "rollout"}, headers=hub["owner"]
    )
    assert plan.status_code == 200, plan.text


@needs_pg
def test_role_a_reader_reads_and_cannot_write(client, hub):
    assert client.put(path(hub, "standup"), json=agent_job(), headers=hub["owner"]).status_code == 200
    assert client.put(path(hub, "mine"), json=agent_job(), headers=hub["reader"]).status_code == 403
    listed = client.get(path(hub), headers=hub["reader"])
    assert listed.status_code == 200 and [j["name"] for j in listed.json()] == ["standup"]
    assert listed.json()[0]["can_manage"] is False
    for action in ("pause", "resume", "run"):
        assert client.post(f"{path(hub, 'standup')}/{action}", json={}, headers=hub["reader"]).status_code == 403
    assert client.delete(path(hub, "standup"), headers=hub["reader"]).status_code == 403


@needs_pg
def test_role_a_writer_manages_only_their_own_job(client, hub):
    assert client.put(path(hub, "standup"), json=agent_job(), headers=hub["owner"]).status_code == 200
    body = {**agent_job(), "prompt": "Mine now", "if_revision": 1}
    assert client.put(path(hub, "standup"), json=body, headers=hub["other"]).status_code == 403
    assert client.post(f"{path(hub, 'standup')}/pause", json={}, headers=hub["other"]).status_code == 403
    assert client.delete(path(hub, "standup"), headers=hub["other"]).status_code == 403
    paused = client.post(f"{path(hub, 'standup')}/pause", json={"reason": "holiday"}, headers=hub["owner"])
    assert paused.status_code == 200
    assert (paused.json()["paused"], paused.json()["paused_by"], paused.json()["next_fire_at"]) == (True, "owner", None)
    assert paused.json()["revision"] == 1  # a pause changes no work


@needs_pg
def test_role_an_admin_pauses_runs_and_deletes_another_persons_job_but_does_not_change_its_work(client, hub):
    assert client.put(path(hub, "standup"), json=agent_job(), headers=hub["owner"]).status_code == 200
    body = {**agent_job(), "prompt": "Changed by the admin", "if_revision": 1}
    assert client.put(path(hub, "standup"), json=body, headers=hub["admin"]).status_code == 403
    paused = client.post(f"{path(hub, 'standup')}/pause", json={"reason": "budget"}, headers=hub["admin"])
    assert paused.status_code == 200 and paused.json()["paused_reason"] == "budget"
    assert client.post(f"{path(hub, 'standup')}/resume", json={}, headers=hub["admin"]).json()["paused"] is False
    before = datetime.now(UTC)
    fired = client.post(f"{path(hub, 'standup')}/run", json={}, headers=hub["admin"])
    assert fired.status_code == 200, fired.text
    fire = fired.json()
    assert fire["manual"] is True and fire["outcome"] is None
    assert datetime.fromisoformat(fire["scheduled_for"]) >= before
    fires = client.get(f"{path(hub, 'standup')}/fires", headers=hub["reader"]).json()
    assert [f["id"] for f in fires] == [fire["id"]]
    shown = client.get(path(hub, "standup"), headers=hub["owner"]).json()
    assert shown["last_fire"]["id"] == fire["id"] and shown["prompt"] == "Standup"
    deleted = client.delete(path(hub, "standup"), headers=hub["admin"])
    assert deleted.status_code == 200 and deleted.json()["deleted"] == "standup"
    assert client.get(path(hub, "standup"), headers=hub["owner"]).status_code == 404


# Firing (``evo_agents.hub.server.cron_fire``; criteria a2, a3, a4 and a6)

DAY = datetime(2026, 11, 10, tzinfo=UTC)


def at(hour: int, minute: int = 0, second: int = 0, day: datetime = DAY) -> datetime:
    return day.replace(hour=hour, minute=minute, second=second)


def fired(client, now: datetime, times: int = 1) -> list[dict]:
    """``times`` passes of the cron job, run together, at ``now``; what each counted."""
    import asyncio

    from evo_agents.hub.server.cron_fire import cron_pass

    async def passes() -> list[dict]:
        engine = client.app.state.engine
        return list(await asyncio.gather(*(cron_pass(engine, now=now) for _ in range(times))))

    return client.portal.call(passes)


def made(client, hub, name: str, body: dict, created: datetime, headers: str = "owner") -> int:
    """Job ``name`` of ``body``, made by ``headers``' member, as if made at ``created``; its id."""
    from sqlalchemy import update

    from evo_agents.hub import tables
    from tests.hub import live
    from tests.hub.live import sql

    response = client.put(path(hub, name), json=body, headers=hub[headers])
    assert response.status_code == 200, response.text
    j = tables.cron_jobs
    query = update(j).values(created_at=created).where(j.c.name == name).returning(j.c.id)
    return sql(live.database_of(client), query)[0][0]


def fires_of(client, job_id: int) -> list[tuple]:
    """(scheduled_for, manual, outcome, reason, run_id) of each fire of job ``job_id``, the oldest due first."""
    from sqlalchemy import select

    from evo_agents.hub import tables
    from tests.hub import live
    from tests.hub.live import sql

    f = tables.cron_fires
    query = select(f.c.scheduled_for, f.c.manual, f.c.outcome, f.c.reason, f.c.run_id).where(f.c.job_id == job_id)
    return [tuple(row) for row in sql(live.database_of(client), query.order_by(f.c.scheduled_for, f.c.id))]


def runs_of(client, job_id: int) -> list[tuple]:
    """(id, kind, state, dispatched_by login, dispatched_via) of each run job ``job_id`` queued."""
    from sqlalchemy import select

    from evo_agents.hub import tables
    from tests.hub import live
    from tests.hub.live import sql

    r, u = tables.runs, tables.users
    query = (
        select(r.c.id, r.c.kind, r.c.state, u.c.login, r.c.dispatched_via)
        .join_from(r, u, u.c.id == r.c.dispatched_by)
        .where(r.c.cron_job_id == job_id)
        .order_by(r.c.id)
    )
    return [tuple(row) for row in sql(live.database_of(client), query)]


def shown(client, hub, name: str) -> dict:
    return client.get(path(hub, name), headers=hub["owner"]).json()


def paused_notices(client) -> list[tuple]:
    """(login, title, body) of each cron_paused notice."""
    from sqlalchemy import select

    from evo_agents.hub import tables
    from tests.hub import live
    from tests.hub.live import sql

    n, u = tables.notifications, tables.users
    query = (
        select(u.c.login, n.c.title, n.c.body)
        .join_from(n, u, u.c.id == n.c.user_id)
        .where(n.c.notice_kind == "cron_paused")
        .order_by(n.c.id)
    )
    return [tuple(row) for row in sql(live.database_of(client), query)]


def command_worker(client, hub) -> dict:
    """A worker of the owner's that serves the project and runs command runs."""
    from evo_agents.hub import runs
    from tests.hub.test_runs import add_worker, beat

    worker = add_worker(client, hub["owner"], "mac-mini")
    beat(client, worker, run_kinds=list(runs.RUN_KINDS))
    return worker


def command_job(cron_expr: str = "0 9 * * *", tz: str = "UTC") -> dict:
    return {"cron": cron_expr, "timezone": tz, "kind": "command", "command": "make nightly-report"}


def test_dst_fall_back_fires_a_wall_clock_time_shown_twice_once():
    """Europe/Paris goes back from 03:00 CEST to 02:00 CET on 2026-10-25: 02:30 is 00:30 and 01:30 UTC."""
    night = datetime(2026, 10, 24, 22, 0, tzinfo=UTC)
    assert cron.fires_between("30 2 * * *", "Europe/Paris", night, night.replace(day=25, hour=4)) == [
        datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    ]
    spring = datetime(2026, 3, 28, 22, 0, tzinfo=UTC)  # 02:30 does not exist on 2026-03-29: the next minute that does
    assert cron.fires_between("30 2 * * *", "Europe/Paris", spring, spring.replace(day=29, hour=4)) == [
        datetime(2026, 3, 29, 1, 0, tzinfo=UTC)
    ]
    assert cron.fires_between("0 9 * * *", "UTC", at(9), at(9, day=DAY.replace(day=11))) == [
        at(9, day=DAY.replace(day=11))
    ]


@needs_pg
def test_fire_each_scheduled_minute_once_whatever_the_passes_that_overlap(client, hub):
    command_worker(client, hub)
    job = made(client, hub, "nightly", command_job(), at(8))
    assert fired(client, at(8, 59, 50)) == [{}], "not due yet"
    counts = fired(client, at(9, 0, 20), times=3)  # three passes at once, as two workers and a restart give
    assert sum(count.get("started", 0) for count in counts) == 1 and not any("error" in count for count in counts)
    assert fired(client, at(9, 1, 5)) == [{}], "a later pass of the same day fires the minute no more"
    (fire,) = fires_of(client, job)
    assert fire[:3] == (at(9), False, "started") and fire[4] is not None
    assert runs_of(client, job) == [(fire[4], "command", "queued", "owner", "machine")]


@needs_pg
def test_fire_handles_a_run_now_press(client, hub):
    command_worker(client, hub)
    job = made(client, hub, "nightly", command_job(), datetime.now(UTC))
    pressed = client.post(f"{path(hub, 'nightly')}/run", json={}, headers=hub["owner"])
    assert pressed.status_code == 200 and pressed.json()["outcome"] is None
    assert fired(client, datetime.now(UTC)) == [{"started": 1}]
    (fire,) = fires_of(client, job)
    assert fire[1:3] == (True, "started")


@needs_pg
def test_catch_up_fires_the_latest_minute_of_the_hour_and_records_the_others_missed(client, hub):
    command_worker(client, hub)
    every = made(client, hub, "every-quarter", command_job("*/15 * * * *"), at(8))
    early = made(client, hub, "early", command_job("0 6 * * *"), at(5))
    assert fired(client, at(8, 50, 10)) == [{"missed": 3, "started": 1}]
    assert [(f[0], f[2]) for f in fires_of(client, every)] == [
        (at(8, 15), "missed"),
        (at(8, 30), "missed"),
        (at(8, 45), "started"),
    ]
    assert [(f[0], f[2]) for f in fires_of(client, early)] == [(at(6), "missed")], "older than an hour: not fired"
    assert len(runs_of(client, every)) == 1 and runs_of(client, early) == []


@needs_pg
def test_busy_while_the_last_run_is_active_or_the_plan_has_a_plan_run(client, hub):
    from tests.hub.test_runs import PLAN, PROJECT

    command_worker(client, hub)
    job = made(client, hub, "every-quarter", command_job("*/15 * * * *"), at(8, 59))
    assert fired(client, at(9, 0, 5)) == [{"started": 1}]
    assert fired(client, at(9, 15, 5)) == [{"busy": 1}]
    (first, second) = fires_of(client, job)
    assert second[2] == "busy" and f"run #{first[4]}" in second[3] and second[4] is None
    assert len(runs_of(client, job)) == 1

    dispatched = client.post(f"/v1/projects/{PROJECT}/plan-runs", json={"plan_id": PLAN}, headers=hub["owner"])
    assert dispatched.status_code == 201, dispatched.text
    plan = made(client, hub, "rollout", {"cron": "30 9 * * *", "kind": "plan", "plan": PLAN}, at(9, 20))
    assert fired(client, at(9, 30, 5)) == [{"busy": 2}]  # the quarter's job, whose run is still queued, and the plan's
    ((_, _, outcome, reason, run_id),) = fires_of(client, plan)
    assert (outcome, run_id) == ("busy", None) and f"plan run #{dispatched.json()['id']}" in reason


@needs_pg
def test_dst_a_job_in_a_zone_with_daylight_saving_fires_once_at_the_fall_back(client, hub):
    command_worker(client, hub)
    job = made(client, hub, "night", command_job("30 2 * * *", "Europe/Paris"), datetime(2026, 10, 24, 20, tzinfo=UTC))
    assert fired(client, datetime(2026, 10, 25, 0, 30, 10, tzinfo=UTC)) == [{"started": 1}]
    assert fired(client, datetime(2026, 10, 25, 1, 30, 10, tzinfo=UTC)) == [{}], "02:30 again in CET fires no more"
    assert [f[0] for f in fires_of(client, job)] == [datetime(2026, 10, 25, 0, 30, tzinfo=UTC)]


def agent_worker(client, hub) -> dict:
    """A worker of the owner's in the pool of agent AGENT, with an account pool of the owner's for it, so a mention of
    AGENT by the owner starts a chat run."""
    from evo_agents.hub import runs
    from tests.hub import live
    from tests.hub.account_pool_setup import give_account_pool
    from tests.hub.test_runs import OWNER, PROJECT, add_worker, beat

    give_account_pool(live.database_of(client), PROJECT, AGENT, OWNER)
    worker = add_worker(client, hub["owner"], "agent-box", pool=AGENT)
    features = [feature for feature in runs.WORKER_FEATURES if feature != runs.LAZY_CHAT_REPOS]
    beat(client, worker, run_kinds=list(runs.RUN_KINDS), features=features)
    assert client.post("/v1/worker/doctor", json={"findings": []}, headers=worker["headers"]).status_code == 200
    return worker


def messages_of(client, chat_id: int) -> list[tuple]:
    """(author user, author profile, thread root, body) of each message of chat ``chat_id``, the oldest first."""
    from sqlalchemy import select

    from evo_agents.hub import tables
    from tests.hub import live
    from tests.hub.live import sql

    m = tables.chat_messages
    query = select(m.c.author_user_id, m.c.author_profile_id, m.c.thread_root_id, m.c.body).where(
        m.c.chat_id == chat_id
    )
    return [tuple(row) for row in sql(live.database_of(client), query.order_by(m.c.seq))]


@needs_pg
def test_agent_job_writes_a_hub_thread_in_the_owners_direct_chat_that_starts_a_chat_run_as_the_owner(client, hub):
    from sqlalchemy import select

    from evo_agents.hub import tables
    from tests.hub import live
    from tests.hub.live import sql

    agent_worker(client, hub)
    job = made(client, hub, "standup", {**agent_job(), "cron": "0 9 * * *", "timezone": "UTC"}, at(8))
    assert fired(client, at(9, 0, 5)) == [{"started": 1}]
    ((_, _, outcome, reason, run_id),) = fires_of(client, job)
    assert (outcome, reason) == ("started", None)
    assert runs_of(client, job) == [(run_id, "chat", "queued", "owner", "machine")]
    r, c = tables.runs, tables.chats
    db = live.database_of(client)
    ((chat_id, kind),) = sql(
        db, select(c.c.id, c.c.kind).join_from(r, c, c.c.id == r.c.chat_id).where(r.c.id == run_id)
    )
    assert kind == "direct"
    ((author, profile, root, body),) = messages_of(client, chat_id)
    assert (author, profile, root) == (None, None, None), "a message of the hub that starts a thread"
    assert body.startswith("Cron job standup of owner:") and "Standup" in body and body.endswith(f"@{AGENT}")


@needs_pg
def test_agent_job_in_a_chat_the_agent_is_not_in_is_refused_with_the_reason(client, hub):
    agent_worker(client, hub)
    group = client.post("/v1/chats", json={"title": "Standups", "people": [], "agents": []}, headers=hub["owner"])
    assert group.status_code == 201, group.text
    chat_id = group.json()["id"]
    body = {**agent_job(), "cron": "0 9 * * *", "timezone": "UTC", "chat_id": chat_id}
    job = made(client, hub, "standup", body, at(8))
    assert fired(client, at(9, 0, 5)) == [{"refused": 1}]
    ((_, _, outcome, reason, run_id),) = fires_of(client, job)
    assert (outcome, run_id) == ("refused", None) and f"@{AGENT}" in reason, reason
    assert runs_of(client, job) == []
    said = messages_of(client, chat_id)
    assert said[0][3].endswith(f"@{AGENT}") and reason in said[-1][3], "the refusal is said in the thread too"


@needs_pg
def test_agent_job_of_an_owner_whose_seat_ended_pauses_and_tells_them(client, hub):
    job = made(client, hub, "standup", {**agent_job(), "cron": "0 9 * * *", "timezone": "UTC"}, at(8))
    with pytest.MonkeyPatch.context() as patch:
        from evo_agents.hub.server import cron_fire

        async def ended(conn, user_id, login, *, what, lock=False):
            return f"{login}'s seat ended on 2026-11-01: {what} cannot start"

        patch.setattr(cron_fire, "ended_refusal", ended)
        assert fired(client, at(9, 0, 5)) == [{"paused": 1, "refused": 1}]
    ((_, _, outcome, reason, _),) = fires_of(client, job)
    assert outcome == "refused" and "seat ended" in reason
    held = shown(client, hub, "standup")
    assert (held["paused"], held["paused_by"]) == (True, None) and "seat ended" in held["paused_reason"]
    assert [(login, title) for login, title, _ in paused_notices(client)] == [
        ("owner", "Cron job standup of evo-agents paused")
    ]
    assert fired(client, at(9, 0, 5, day=DAY.replace(day=11))) == [{}], "a paused job fires nothing"


@needs_pg
def test_plan_job_dispatches_a_plan_run_as_its_owner(client, hub):
    from sqlalchemy import select

    from evo_agents.hub import tables
    from tests.hub import live
    from tests.hub.live import sql
    from tests.hub.test_runs import PLAN

    job = made(client, hub, "rollout", {"cron": "0 3 * * *", "kind": "plan", "plan": PLAN}, at(2))
    assert fired(client, at(3, 0, 5)) == [{"started": 1}]
    ((_, _, outcome, _, run_id),) = fires_of(client, job)
    assert outcome == "started" and runs_of(client, job) == [(run_id, "plan", "queued", "owner", "machine")]
    r = tables.runs
    assert sql(live.database_of(client), select(r.c.plan_id).where(r.c.id == run_id)) == [(PLAN,)]
    assert fired(client, at(3, 0, 5, day=DAY.replace(day=11))) == [{"busy": 1}], "its plan run is still queued"


@needs_pg
def test_plan_job_of_a_plan_with_no_pending_step_is_refused_and_pauses(client, hub):
    from tests.hub.test_runs import PLAN, PROJECT, plan_body

    job = made(client, hub, "rollout", {"cron": "0 3 * * *", "kind": "plan", "plan": PLAN}, at(2))
    done = plan_body()
    done["steps"] = [{**step, "status": "done", "evidence": "evo-agents@abc1234: done"} for step in done["steps"]]
    pushed = client.put(
        f"/v1/projects/{PROJECT}/plans/{PLAN}", json={"body": done, "if_revision": 1}, headers=hub["owner"]
    )
    assert pushed.status_code == 200, pushed.text
    assert fired(client, at(3, 0, 5)) == [{"paused": 1, "refused": 1}]
    ((_, _, outcome, reason, run_id),) = fires_of(client, job)
    assert (outcome, run_id) == ("refused", None) and "has no pending step" in reason
    held = shown(client, hub, "rollout")
    assert held["paused"] is True and "has no pending step" in held["paused_reason"]
    assert [login for login, _, _ in paused_notices(client)] == ["owner"]


@needs_pg
def test_plan_job_of_a_writer_who_is_now_a_reader_pauses_and_tells_them(client, hub):
    from tests.hub.test_runs import OWNER, PLAN, PROJECT, READ_ONLY

    job = made(client, hub, "rollout", {"cron": "0 3 * * *", "kind": "plan", "plan": PLAN}, at(2))
    regranted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=READ_ONLY, headers=hub["hub_admin"])
    assert regranted.status_code == 200, regranted.text
    assert fired(client, at(3, 0, 5)) == [{"paused": 1, "refused": 1}]
    ((_, _, outcome, reason, _),) = fires_of(client, job)
    assert outcome == "refused" and "no longer a writer" in reason
    assert runs_of(client, job) == []
    held = shown(client, hub, "rollout")
    assert (held["paused"], held["paused_by"]) == (True, None)
    ((login, title, body),) = paused_notices(client)
    assert login == "owner" and "rollout" in title and "no longer a writer" in body


@needs_pg
def test_breaker_pauses_a_job_whose_last_three_runs_failed_not_counting_infra_failures(client, hub):
    from sqlalchemy import select

    from evo_agents.hub import tables
    from tests.hub import live
    from tests.hub.live import sql
    from tests.hub.test_runs import claim, moved

    worker = command_worker(client, hub)
    job = made(client, hub, "every-quarter", command_job("*/15 * * * *"), at(8, 59))

    def failed_run(quarter: int, cause: str) -> None:
        assert fired(client, at(9, quarter, 5)) == [{"started": 1}]
        spec = claim(client, worker)
        moved(client, worker, spec["id"], "running")
        moved(client, worker, spec["id"], "failed", error="it went wrong", failure_cause=cause)

    failed_run(0, "command_failed")
    failed_run(15, "command_failed")
    failed_run(30, "network")  # the network's, not the job's
    failed_run(45, "command_failed")  # the third of the job's, but with the network's between: still three in a row
    assert fired(client, at(10, 0, 5)) == [{"paused": 1, "refused": 1}]
    held = shown(client, hub, "every-quarter")
    assert held["paused"] is True and "failed in a row" in held["paused_reason"]
    assert [login for login, _, _ in paused_notices(client)] == ["owner"]
    assert fires_of(client, job)[-1][2:4] == ("refused", f"the job is paused: {held['paused_reason']}")

    resumed = client.post(f"{path(hub, 'every-quarter')}/resume", json={}, headers=hub["owner"])
    assert resumed.status_code == 200, resumed.text
    r = tables.runs
    states = sql(live.database_of(client), select(r.c.state).where(r.c.cron_job_id == job).order_by(r.c.id))
    assert [state for (state,) in states] == ["failed"] * 4
    later = datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(minutes=16)
    counts = fired(client, later)
    assert "paused" not in counts[0], "a resumed job's breaker counts only the runs since"
    assert shown(client, hub, "every-quarter")["paused"] is False


@needs_pg
def test_breaker_of_a_job_with_a_run_that_did_its_work_does_not_trip(client, hub):
    from sqlalchemy import func

    from tests.hub import live
    from tests.hub.test_runs import claim, moved, set_run

    worker = command_worker(client, hub)
    made(client, hub, "every-quarter", command_job("*/15 * * * *"), at(8, 59))
    for quarter, state in ((0, "failed"), (15, "done"), (30, "failed"), (45, "failed")):
        assert fired(client, at(9, quarter, 5)) == [{"started": 1}]
        spec = claim(client, worker)
        moved(client, worker, spec["id"], "running")
        if state == "failed":
            moved(client, worker, spec["id"], "failed", error="it went wrong", failure_cause="command_failed")
        else:  # the command exited 0, as the daemon's report of its verify says
            set_run(live.database_of(client), spec["id"], state="done", finished_at=func.now())
    assert fired(client, at(10, 0, 5)) == [{"started": 1}]
    assert shown(client, hub, "every-quarter")["paused"] is False


@needs_pg
def test_archived_project_fires_nothing(client, hub):
    from tests.hub.test_runs import PROJECT

    command_worker(client, hub)
    job = made(client, hub, "nightly", command_job(), at(8))
    archived = client.post(f"/v1/projects/{PROJECT}/archive", headers=hub["admin"])
    assert archived.status_code == 200, archived.text
    assert fired(client, at(9, 0, 5)) == [{}]
    assert fires_of(client, job) == [] and runs_of(client, job) == []

"""Today (plan hub-mobile-phase-0, criterion a7): GET /v1/me/today worked out at each call, over the projects the
caller holds a grant on, with the checks of GET /v1/me/overview; the questions asked of the caller; at most eight things
that changed since the caller's seen mark, each with a link; the seen mark that only moves forward, the window of 24
hours without one and of seven days for an old one; and the job hub.today_digest, which pushes the summary to a member
away from the hub, at the local time they chose, once a day.

The world is test_overview's: projects A (hub sink clearing customer) and B, plans rollout, docs and vault (labelled
customer) of A, and their members."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from evo_agents.hub import jobs, push_payload, tables
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.brief import send_today_digests
from evo_agents.hub.server.today import MAX_ITEMS, summary
from evo_agents.hub.worker import queue
from tests.hub.live import sql
from tests.hub.test_overview import (
    BEA,
    DOCS,
    INSIDER,
    OTHER,
    OWNER,
    READER,
    ROLLOUT,
    SESSION_ZONE,
    STRANGER,
    VAULT,
    WAIT,
    A,
    add_run,
    db_now,
    members,  # noqa: F401 (a fixture, on this module's client)
    world,  # noqa: F401 (a fixture)
)
from tests.hub.test_overview import ask_of as asked_of
from tests.hub.test_push import HUB_URL, VAPID_KEY, browser, deliver
from tests.hub.test_push import service as service  # noqa: F401 (a fixture)
from tests.hub.test_runs import add_worker

MINUTE = timedelta(minutes=1)
MARKS, NOTIFICATIONS, DELIVERIES = tables.today_marks, tables.notifications, tables.notification_deliveries


@pytest.fixture
def client(hub_db, tmp_path, github, monkeypatch):
    monkeypatch.setenv("PGTZ", SESSION_ZONE)  # libpq starts each session of this process in that zone
    config = live.hub_config(hub_db, tmp_path, github, public_url=HUB_URL, vapid_private_key=VAPID_KEY)
    with TestClient(create_app(config), base_url=HUB_URL) as client:
        yield client


def today_of(client, headers) -> dict:
    response = client.get("/v1/me/today", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def seen(client, headers, seen_at: datetime | str | None = None) -> datetime:
    body = None if seen_at is None else {"seen_at": seen_at if isinstance(seen_at, str) else seen_at.isoformat()}
    response = client.post("/v1/me/today/seen", json=body, headers=headers)
    assert response.status_code == 200, response.text
    return datetime.fromisoformat(response.json()["seen_at"])


def user_id(db, login: str) -> int:
    return sql(db, select(tables.users.c.id).where(tables.users.c.login == login))[0][0]


def project_id(db, name: str) -> int:
    return sql(db, select(tables.projects.c.id).where(tables.projects.c.name == name))[0][0]


def set_mark(db, login: str, at: datetime) -> None:
    marked = pg_insert(MARKS).values(user_id=user_id(db, login), seen_at=at)
    sql(db, marked.on_conflict_do_update(constraint="today_marks_pkey", set_={"seen_at": at}))


def at(item: dict) -> datetime:
    return datetime.fromisoformat(item["at"])


def done(client, headers, project: str, plan_id: str, step: int) -> None:
    """Step ``step`` of the plan marked done, as `evo-agents hub plan step` does."""
    url = f"/v1/projects/{project}/plans/{plan_id}"
    revision = client.get(url, headers=headers).json()["revision"]
    updates = {"status": "done", "done_at": "2026-10-25"}
    body = {"section": "steps", "step": step, "updates": updates, "if_revision": revision}
    response = client.patch(url, json=body, headers=headers)
    assert response.status_code == 200, response.text


def cost(db, run_id: int, usd: float) -> None:
    sql(db, update(tables.runs).values(usage={"total_cost_usd": usd}).where(tables.runs.c.id == run_id))


def published(db, project: str, owner: str, ended_at: datetime, tag: str = "v1.2.3") -> int:
    """A cycle of the project's daily release, published at ``ended_at`` with ``tag``."""
    owner_id, pid = user_id(db, owner), project_id(db, project)
    schedule = {"project_id": pid, "repo": "evo-agents", "check_at": "09:00", "timezone": "UTC", "target_ref": "main"}
    schedule |= {"tag_pattern": "v*", "workflow": "release.yml", "paths": ["pyproject.toml"]}
    s, c, t = tables.release_schedules, tables.release_cycles, tables.release_tags
    schedule_id = sql(db, insert(s).values(**schedule, owner_id=owner_id, updated_by=owner_id).returning(s.c.id))[0][0]
    cycle = {"project_id": pid, "schedule_id": schedule_id, "state": "published", "start": "cut", "owner_id": owner_id}
    cycle |= {"local_day": ended_at.date(), "target_ref": "main", "ended_at": ended_at}
    cycle_id = sql(db, insert(c).values(**cycle).returning(c.c.id))[0][0]
    sql(db, insert(t).values(cycle_id=cycle_id, tag=tag, commit="a" * 40, ci_state="passed"))
    return cycle_id


def owned_problem(db, project: str, owner: str, values: list[tuple[datetime, float]]) -> None:
    """Problem P-1 of the project, owned by ``owner``, whose metric is the last value of key app.latency, measured
    at each moment of ``values``."""
    owner_id, pid = user_id(db, owner), project_id(db, project)
    k, s, p, r = tables.signal_keys, tables.signals, tables.problems, tables.problem_revisions
    key_id = sql(
        db, insert(k).values(project_id=pid, name="app.latency", kind="number", created_by=owner_id).returning(k.c.id)
    )[0][0]
    for index, (measured_at, value) in enumerate(values):
        signal = {"project_id": pid, "key_id": key_id, "value": value, "measured_at": measured_at, "via": "member"}
        signal |= {"label": {"level": "internal"}, "user_id": owner_id, "batch_id": UUID(int=index + 1)}
        sql(db, insert(s).values(**signal))
    problem_id = sql(db, insert(p).values(project_id=pid, number=1, revision=1).returning(p.c.id))[0][0]
    revision = {"problem_id": problem_id, "revision": 1, "statement": "Search is slow", "who": "readers"}
    revision |= {
        "when": "each search",
        "workaround": "they wait",
        "metric": {"key": "app.latency", "aggregate": "last"},
    }
    sql(db, insert(r).values(**revision, rank=1, owner_id=owner_id, status="open", created_by=owner_id))


def proposed(db, project: str, run_id: int, title: str = "Say where the tests live") -> int:
    proposal = {"project_id": project_id(db, project), "run_id": run_id, "lens": "harness", "kind": "docs"}
    proposal |= {"title": title, "summary": "s", "paths": [], "tier": 2, "tier_reasons": ["a docs change"]}
    proposal |= {"evidence": [{"text": "e"}], "evidence_count": 3, "draft": {}, "fingerprint": "f" * 64}
    p = tables.proposals
    return sql(db, insert(p).values(**proposal, label={"level": "internal"}).returning(p.c.id))[0][0]


def admin_of(db, project: str, login: str) -> None:
    g = tables.grants
    where = (g.c.user_id == user_id(db, login), g.c.project_id == project_id(db, project))
    sql(db, update(g).values(role="admin").where(*where))


# Needs you


def test_needs_you_lists_the_questions_asked_of_you_the_oldest_first_with_when_their_run_parks(
    client, members, world, hub_db
):
    shown = today_of(client, members[OWNER])
    assert [item["id"] for item in shown["needs_you"]] == [world["d_mine"], world["d_vault"]]
    assert shown["needs_you_count"] == 2
    mine = shown["needs_you"][0]
    assert (mine["project"], mine["run_id"], mine["plan_id"], mine["plan_title"], mine["step_key"]) == (
        A,
        world["waiting"],
        ROLLOUT,
        "Run the fleet",
        "2",
    )
    assert (mine["category"], mine["question"], mine["link"]) == (
        "scope",
        f"Go on with run {world['waiting']}?",
        f"/inbox?decision={world['d_mine']}",
    )
    runs = tables.runs
    ((waiting_since,),) = sql(hub_db, select(runs.c.waiting_since).where(runs.c.id == world["waiting"]))
    assert datetime.fromisoformat(mine["parks_at"]) == waiting_since + WAIT
    assert shown["summary"].startswith("2 questions wait for your answer. In the last 24 hours: ")
    # A question of another member's run asked of you is yours; one of your run asked of someone else is theirs.
    asked_of(hub_db, world["d_other"], OWNER)
    asked_of(hub_db, world["d_mine"], OTHER)
    shown = today_of(client, members[OWNER])
    assert [item["id"] for item in shown["needs_you"]] == [world["d_other"], world["d_vault"]]
    parked = shown["needs_you"][0]
    ((parked_at,),) = sql(hub_db, select(runs.c.parked_at).where(runs.c.id == world["parked"]))
    assert datetime.fromisoformat(parked["parks_at"]) == parked_at
    assert [item["id"] for item in today_of(client, members[OTHER])["needs_you"]] == [world["d_mine"]]
    # A question asked of you on a plan your grant does not reach is not shown, as the overview hides it.
    asked_of(hub_db, world["d_vault"], READER)
    assert today_of(client, members[READER])["needs_you"] == []
    assert today_of(client, members[BEA])["needs_you_count"] == 1
    assert today_of(client, members[STRANGER])["needs_you"] == []


# What changed


@pytest.fixture
def news(client, members, hub_db) -> dict:
    """Since owner's mark two hours ago: a release, a failed run, a problem whose metric moved, runs that cost, a
    parked run, two plan steps done (one of vault) and a proposal; and things before the mark or of others. The plans
    were pushed with steps 1 and 5 done after the mark: those are none of the news."""
    now = db_now(hub_db)
    wid = add_worker(client, members[OWNER], "mac-mini")["id"]
    for login in (OWNER, READER, INSIDER):
        set_mark(hub_db, login, now - timedelta(hours=2))
    found = {"now": now}
    found["release"] = published(hub_db, A, OWNER, now - timedelta(minutes=100))
    found["failed"] = add_run(hub_db, wid, A, ROLLOUT, "failed", OWNER, step="1", finished_at=now - 90 * MINUTE)
    cost(hub_db, found["failed"], 1.25)
    found["before"] = add_run(hub_db, wid, A, ROLLOUT, "lost", OWNER, step="1", finished_at=now - timedelta(hours=3))
    cost(hub_db, found["before"], 3.0)
    found["theirs"] = add_run(hub_db, wid, A, ROLLOUT, "failed", OTHER, step="3", finished_at=now - 60 * MINUTE)
    owned_problem(hub_db, A, OWNER, [(now - timedelta(days=10), 10.0), (now - 80 * MINUTE, 7.0)])
    found["done"] = add_run(hub_db, wid, A, ROLLOUT, "done", OWNER, step="2", finished_at=now - 70 * MINUTE)
    cost(hub_db, found["done"], 0.5)
    found["parked"] = add_run(hub_db, wid, A, DOCS, "parked", OWNER, since=now - 30 * MINUTE)
    done(client, members[OWNER], A, ROLLOUT, 2)
    done(client, members[OWNER], A, VAULT, 4)
    admin_of(hub_db, A, OWNER)
    found["proposal"] = proposed(hub_db, A, found["failed"])
    return found


def test_today_lists_what_changed_since_your_mark_in_the_order_it_happened_each_with_a_link(
    client, members, news, hub_db
):
    shown = today_of(client, members[OWNER])
    items = shown["items"]
    assert [item["kind"] for item in items] == [
        "release",
        "run_failed",
        "problem",
        "cost",
        "run_parked",
        "step_done",
        "step_done",
        "proposal",
    ]
    assert [at(item) for item in items] == sorted(at(item) for item in items)
    assert all(item["project"] == A for item in items)
    now = news["now"]
    release, failed, problem, spent, parked, step, vault, proposal = items
    assert (release["title"], release["link"], at(release)) == (
        "Released v1.2.3 of evo-agents",
        f"/p/{A}/releases",
        now - timedelta(minutes=100),
    )
    assert (failed["title"], failed["body"], failed["link"]) == (
        f"Run #{news['failed']} failed",
        "Step 1: verify failed: pytest exited 1",
        f"/p/{A}/runs/{news['failed']}",
    )
    assert (problem["title"], problem["link"], at(problem)) == (
        "P-1: Search is slow",
        f"/p/{A}/problems/P-1",
        now - 80 * MINUTE,
    )
    assert problem["body"] == "app.latency last over 90 days: 10 seven days ago, 7 now."
    assert (spent["title"], spent["body"], at(spent), spent["link"]) == (
        "Your runs cost $1.75",
        f"2 runs you dispatched ended in {A}.",
        now - 70 * MINUTE,
        f"/p/{A}/runs",
    )
    assert (parked["title"], parked["link"]) == (f"Run #{news['parked']} parked", f"/p/{A}/runs/{news['parked']}")
    assert (step["title"], step["body"], step["link"]) == (
        "API",
        "Step 2 of Run the fleet is done.",
        f"/p/{A}/plans/{ROLLOUT}",
    )
    assert (vault["title"], vault["link"]) == ("Docs", f"/p/{A}/plans/{VAULT}")
    assert (proposal["title"], proposal["link"]) == ("Say where the tests live", f"/inbox?proposal={news['proposal']}")
    assert (
        proposal["body"] == "A tier 2 proposal of the Curator, with 3 pieces of evidence, waits for an admin's answer."
    )
    assert shown["caught_up"] is True and len(items) == MAX_ITEMS
    assert (
        datetime.fromisoformat(shown["since"])
        == datetime.fromisoformat(shown["seen_at"])
        == now - 2 * timedelta(hours=1)
    )
    assert datetime.fromisoformat(shown["seen_until"]) > at(proposal)
    assert shown["summary"] == (
        "Nothing waits for your answer. Since you last looked: a release, 2 plan steps done, a failed run, a parked "
        "run, a problem whose metric moved, $1.75 spent by your runs and a proposal to answer."
    )


def test_today_shows_a_member_only_what_their_grant_reaches_and_what_is_theirs(client, members, news, hub_db):
    # reader: the release and the step of rollout; nothing of owner's runs, problem or proposals, nor vault's step
    shown = today_of(client, members[READER])
    assert [(item["kind"], item["title"]) for item in shown["items"]] == [
        ("release", "Released v1.2.3 of evo-agents"),
        ("step_done", "API"),
    ]
    # insider reaches customer: vault's step too
    assert [item["title"] for item in today_of(client, members[INSIDER])["items"]] == [
        "Released v1.2.3 of evo-agents",
        "API",
        "Docs",
    ]
    # bea holds a grant on B alone, and the stranger on nothing
    assert today_of(client, members[BEA])["items"] == []
    assert today_of(client, members[STRANGER])["items"] == []
    # other's own failed run is theirs; with no mark their window is the last 24 hours
    theirs = today_of(client, members[OTHER])
    assert [item["kind"] for item in theirs["items"]] == ["release", "run_failed", "step_done"]
    assert theirs["seen_at"] is None
    assert (
        theirs["summary"]
        == "Nothing waits for your answer. In the last 24 hours: a release, a plan step done and a failed run."
    )


def test_today_stops_at_eight_items_and_the_mark_of_the_last_shows_the_rest(client, members, hub_db):
    now = db_now(hub_db)
    wid = add_worker(client, members[OWNER], "mac-mini")["id"]
    set_mark(hub_db, OWNER, now - timedelta(hours=2))
    failed = [
        add_run(hub_db, wid, A, ROLLOUT, "failed", OWNER, step="1", finished_at=now - (11 - n) * 5 * MINUTE)
        for n in range(10)
    ]
    shown = today_of(client, members[OWNER])
    assert [item["link"] for item in shown["items"]] == [f"/p/{A}/runs/{run_id}" for run_id in failed[:MAX_ITEMS]]
    assert shown["caught_up"] is False
    assert shown["seen_until"] == shown["items"][-1]["at"]
    assert shown["summary"] == "Nothing waits for your answer. Since you last looked: 8 failed runs, and more."
    seen(client, members[OWNER], shown["seen_until"])
    rest = today_of(client, members[OWNER])
    assert [item["link"] for item in rest["items"]] == [f"/p/{A}/runs/{run_id}" for run_id in failed[MAX_ITEMS:]]
    assert rest["caught_up"] is True
    assert datetime.fromisoformat(rest["seen_at"]) == datetime.fromisoformat(shown["seen_until"])


def test_the_seen_mark_only_moves_forward_and_sets_the_window(client, members, hub_db):
    headers = members[OWNER]
    first = today_of(client, headers)
    assert first["seen_at"] is None and first["items"] == [] and first["caught_up"] is True
    assert datetime.fromisoformat(first["seen_until"]) - datetime.fromisoformat(first["since"]) == timedelta(hours=24)
    marked = seen(client, headers)  # now
    assert datetime.fromisoformat(today_of(client, headers)["since"]) == marked
    assert seen(client, headers, marked - timedelta(hours=1)) == marked  # an earlier mark leaves it
    later = seen(client, headers, marked + timedelta(days=3))  # never past now
    assert marked <= later <= db_now(hub_db)
    # A mark older than seven days counts from seven days ago.
    set_mark(hub_db, OWNER, db_now(hub_db) - timedelta(days=30))
    old = today_of(client, headers)
    window = datetime.fromisoformat(old["seen_until"]) - datetime.fromisoformat(old["since"])
    assert window == timedelta(days=7) and old["summary"].endswith("nothing changed in the last 7 days.")
    # A time without its zone, or another key, is refused; the routes need a sign-in.
    url = "/v1/me/today/seen"
    assert client.post(url, json={"seen_at": "2026-10-25T08:00:00"}, headers=headers).status_code == 422
    assert client.post(url, json={"seen": "2026-10-25T08:00:00Z"}, headers=headers).status_code == 422
    assert client.post(url).status_code == 401 and client.get("/v1/me/today").status_code == 401


def test_the_summary_is_made_from_templates():
    def found(*kinds: str, usd: float = 0.0):
        return [SimpleNamespace(item=SimpleNamespace(kind=kind), cost_usd=usd) for kind in kinds]

    assert summary(0, [], True, "since you last looked") == (
        "Nothing waits for your answer, and nothing changed since you last looked."
    )
    assert summary(1, found("release", "release"), True, "in the last 24 hours") == (
        "1 question waits for your answer. In the last 24 hours: 2 releases."
    )
    assert summary(3, found("cost", usd=0.004), False, "since you last looked") == (
        "3 questions wait for your answer. Since you last looked: $0.0040 spent by your runs, and more."
    )


# The summary push


@pytest.fixture
def away(client, members, hub_db) -> dict:
    """Owner, with a browser that takes pushes, away from the hub and from Today for nine hours, with a failed run an
    hour ago; bea with a browser too, seen an hour ago. Owner's summary is due now, at their digest_at in a fixed-offset
    zone where it is noon, whatever the wall clock, so no step of a test crosses a local midnight; bea's digest_at, in
    the same zone, is twelve hours away, so her default (07:30 in the hub's zone) never falls due by the clock of CI."""
    now = db_now(hub_db)
    wid = add_worker(client, members[OWNER], "mac-mini")["id"]
    run_id = add_run(hub_db, wid, A, ROLLOUT, "failed", OWNER, step="1", finished_at=now - timedelta(hours=1))
    phones = {}
    for login in (OWNER, BEA):
        phones[login] = browser(login)
        response = client.post("/v1/me/push/devices", json=phones[login].body(), headers=members[login])
        assert response.status_code == 201, response.text
    ahead = (12 - now.astimezone(UTC).hour) % 24
    ahead -= 24 if ahead > 12 else 0
    zone = f"Etc/GMT{-ahead:+d}" if ahead else "UTC"  # POSIX signs: Etc/GMT-5 is UTC+5
    local = now.astimezone(ZoneInfo(zone))
    for login, digest_at in ((OWNER, local), (BEA, local + timedelta(hours=12))):
        settings = {"time_zone": zone, "digest_at": f"{digest_at:%H:%M}"}
        response = client.put("/v1/me/notification-settings", json=settings, headers=members[login])
        assert response.status_code == 200, response.text
    users = tables.users
    for login, ago in ((OWNER, timedelta(hours=9)), (BEA, timedelta(hours=1))):
        sql(hub_db, update(users).values(last_seen_at=now - ago).where(users.c.login == login))
    set_mark(hub_db, OWNER, now - timedelta(hours=9))
    return {"now": now, "run": run_id, "phones": phones}


def digests(client, now: datetime) -> dict:
    app = client.app
    return client.portal.call(partial(send_today_digests, app.state.engine, config=app.state.config, now=now))


def summaries(db) -> list[tuple]:
    n = NOTIFICATIONS
    query = select(n.c.user_id, n.c.project_id, n.c.title, n.c.body, n.c.link, n.c.read_at.is_not(None))
    return sql(db, query.where(n.c.notice_kind == "today").order_by(n.c.id))


def test_a_member_away_gets_one_summary_push_at_their_time_with_a_link_home(client, members, away, hub_db, service):
    now = away["now"]
    assert digests(client, now - 2 * MINUTE) == {"not_due": 1}  # bea was seen an hour ago: not asked
    assert digests(client, now) == {"today": 1}
    assert digests(client, now + MINUTE) == {"sent": 1}  # once a day
    sentence = "Nothing waits for your answer. Since you last looked: a failed run."
    assert summaries(hub_db) == [(user_id(hub_db, OWNER), None, "Today on the hub", sentence, "/", True)]
    # It goes to owner's devices alone: no delivery on the web, read already, so the bell does not count it.
    targets = select(DELIVERIES.c.channel_id, DELIVERIES.c.device_id.is_not(None)).join_from(
        DELIVERIES, NOTIFICATIONS, NOTIFICATIONS.c.id == DELIVERIES.c.notification_id
    )
    assert sql(hub_db, targets.where(NOTIFICATIONS.c.notice_kind == "today")) == [(None, True)]
    deliver(client)
    (pushed,) = service.to(away["phones"][OWNER])
    shown = away["phones"][OWNER].decrypt(pushed.content)
    assert (shown["title"], shown["body"], shown["url"], shown["kind"], shown["project"]) == (
        "Today on the hub",
        sentence,
        f"{HUB_URL}/",
        "notice",
        None,
    )
    assert service.to(away["phones"][BEA]) == []
    # The next day it goes again, once; bea, away by then, waits for her own digest_at, twelve hours off.
    assert digests(client, now + timedelta(days=1)) == {"not_due": 1, "today": 1}
    assert len(summaries(hub_db)) == 2


def test_no_summary_push_goes_to_a_member_seen_lately_or_caught_up(client, members, away, hub_db):
    now, users, runs = away["now"], tables.users, tables.runs
    # Owner looked at Today an hour ago: not away, whatever last_seen_at says.
    set_mark(hub_db, OWNER, now - timedelta(hours=1))
    assert digests(client, now) == {}
    # Seen on the hub an hour ago.
    set_mark(hub_db, OWNER, now - timedelta(hours=9))
    sql(hub_db, update(users).values(last_seen_at=now - timedelta(hours=1)).where(users.c.login == OWNER))
    assert digests(client, now) == {}
    # Away, but nothing changed since the mark: no push, and the day is spent.
    sql(hub_db, update(users).values(last_seen_at=now - timedelta(hours=9)).where(users.c.login == OWNER))
    sql(hub_db, update(runs).values(finished_at=now - timedelta(hours=10)).where(runs.c.id == away["run"]))
    assert digests(client, now) == {"caught_up": 1}
    assert digests(client, now + MINUTE) == {"sent": 1}
    assert summaries(hub_db) == []


def test_a_member_who_turns_the_summary_off_or_has_no_device_gets_none(client, members, away, hub_db):
    off = {"tiers": [{"kind": "today", "tier": "off"}]}
    assert client.put("/v1/me/notification-settings", json=off, headers=members[OWNER]).status_code == 200
    assert digests(client, away["now"]) == {}
    back = {"tiers": [{"kind": "today", "tier": None}]}
    assert client.put("/v1/me/notification-settings", json=back, headers=members[OWNER]).status_code == 200
    sql(hub_db, tables.push_devices.delete().where(tables.push_devices.c.user_id == user_id(hub_db, OWNER)))
    assert digests(client, away["now"]) == {}
    assert summaries(hub_db) == []


def test_the_summary_push_is_due_at_digest_at_in_the_members_time_zone_for_three_hours(client, members, away):
    now = away["now"]
    assert digests(client, now - 2 * MINUTE) == {"not_due": 1}
    assert digests(client, now + timedelta(hours=3)) == {"not_due": 1}  # three hours late: it waits a day
    # In Tokyo (UTC+9 all year) a digest_at of now in UTC is nine hours gone, or fifteen hours ahead.
    tokyo = {"time_zone": "Asia/Tokyo", "digest_at": f"{now.astimezone(UTC):%H:%M}"}
    assert client.put("/v1/me/notification-settings", json=tokyo, headers=members[OWNER]).status_code == 200
    assert digests(client, now) == {"not_due": 1}
    there = {"digest_at": f"{now.astimezone(UTC) + timedelta(hours=9):%H:%M}"}
    assert client.put("/v1/me/notification-settings", json=there, headers=members[OWNER]).status_code == 200
    assert digests(client, now) == {"today": 1}


def test_the_summary_push_job_runs_every_minute_once_at_a_time():
    periodic = {task.task.name: task for task in queue.periodic_registry.periodic_tasks.values()}
    assert periodic[jobs.TODAY_DIGEST].cron == "* * * * *"
    assert queue.tasks[jobs.TODAY_DIGEST].queueing_lock == jobs.TODAY_DIGEST == "hub.today_digest"


def test_payload_of_the_summary_push_carries_its_sentence_and_nothing_when_details_are_hidden():
    notice = SimpleNamespace(
        id=9,
        kind="notice",
        notice_kind="today",
        project=None,
        decision_id=None,
        title="Today on the hub",
        body="1 question waits for your answer. Since you last looked: a release.",
        link="/",
    )
    shown = push_payload.payload(SimpleNamespace(**vars(notice), extra={"restricted": False}), HUB_URL)
    assert (shown["title"], shown["body"], shown["url"]) == ("Today on the hub", notice.body, f"{HUB_URL}/")
    hidden = push_payload.payload(
        SimpleNamespace(**vars(notice), extra={"restricted": False, "hide_details": True}), HUB_URL
    )
    assert (hidden["title"], hidden["body"]) == ("Today on the hub", None)

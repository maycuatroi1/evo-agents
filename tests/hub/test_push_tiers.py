"""The tiers of notifications and the quiet hours of members (plan hub-mobile-phase-0, criterion a3): which
notifications are pushed by default (a decision asked of the member, a run_failed of a run they dispatched, a release
that went wrong, merge_paused) and which are brief, a kind added later included; a tier the member chose for a kind,
which holds for every notification of it; the settings that read and set them, with the quiet hours, the time zone
(the hub's web time zone until the member chooses one) and the time of the summary push; quiet hours that hold a push
until they end, but not one of a decision whose run parks within two hours; and the Inbox and the bell, which get every
notification whatever its tier.

The push service is the fake of tests/hub/test_push.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import extract, func, select, update

from evo_agents.hub import runs, tables
from evo_agents.hub.config import ConfigError, load_config
from evo_agents.hub.server import notifications, push_tiers
from evo_agents.hub.server.push_tiers import BRIEF, INTERRUPT, Facts
from tests.hub.live import sql
from tests.hub.test_decisions import answered, asked
from tests.hub.test_notifications import PUSHED, notice
from tests.hub.test_plan_runs import plan_body, push, started
from tests.hub.test_push import (  # noqa: F401 (client, hub and service are fixtures)
    VAPID_KEY,
    added,
    asked_decision,
    browser,
    client,
    deliver,
    hub,
    make_client,
    service,
)
from tests.hub.test_runs import OTHER, OWNER, PROJECT, members

PATH = "/v1/me/notification-settings"
DELIVERIES, STORED = tables.notification_deliveries, tables.notifications
DEFAULT_INTERRUPTS = {"decision", "run_failed", "release", "merge_paused"}


def settings(client, headers) -> dict:
    response = client.get(PATH, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def put(client, headers, **body):
    return client.put(PATH, json=body, headers=headers)


def chosen(client, headers, **body) -> dict:
    response = put(client, headers, **body)
    assert response.status_code == 200, response.text
    return response.json()


def tiers_of(shown: dict) -> dict[str, tuple[str, bool]]:
    return {entry["kind"]: (entry["tier"], entry["chosen"]) for entry in shown["tiers"]}


def user_id(db, login: str) -> int:
    return sql(db, select(tables.users.c.id).where(tables.users.c.login == login))[0][0]


def project_id(db) -> int:
    return sql(db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]


def notified(client, **fields) -> int:
    """A notification stored through ``notifications.notify``, as the hub's own senders store theirs; its id."""

    async def store():
        async with client.app.state.engine.begin() as conn:
            return await notifications.notify(conn, **fields)

    return client.portal.call(store)


def pushes_of(db) -> dict[str, list[str]]:
    """The title of each notification and the states of its deliveries to devices."""
    query = (
        select(STORED.c.title, DELIVERIES.c.state)
        .join_from(DELIVERIES, STORED, STORED.c.id == DELIVERIES.c.notification_id)
        .where(DELIVERIES.c.device_id.is_not(None))
        .order_by(DELIVERIES.c.id)
    )
    found: dict[str, list[str]] = {}
    for title, state in sql(db, query):
        found.setdefault(title, []).append(state)
    return found


def device_rows(db) -> list[tuple]:
    """(state, attempts, seconds until next_at, last_error) of each delivery to a device, oldest first."""
    wait = extract("epoch", DELIVERIES.c.next_at - func.now())
    query = (
        select(DELIVERIES.c.state, DELIVERIES.c.attempts, wait, DELIVERIES.c.last_error)
        .where(DELIVERIES.c.device_id.is_not(None))
        .order_by(DELIVERIES.c.id)
    )
    return sql(db, query)


def due_now(db) -> None:
    """Time passes: every pending delivery to a device is due."""
    sql(db, update(DELIVERIES).values(next_at=func.now()).where(DELIVERIES.c.device_id.is_not(None)))


def hhmm(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def quiet_now(client, headers, *, zone: str = "UTC", offset: timedelta = timedelta(0), ends_in: float = 3) -> None:
    """Quiet hours from an hour ago until ``ends_in`` hours from now, in ``zone`` (``offset`` ahead of UTC now)."""
    local = datetime.now(UTC) + offset
    hours = {"start": hhmm(local - timedelta(hours=1)), "end": hhmm(local + timedelta(hours=ends_in))}
    chosen(client, headers, quiet_hours=hours, time_zone=zone)


def quiet_later(client, headers) -> None:
    """Quiet hours that start in five hours, in UTC: now is outside them."""
    now = datetime.now(UTC)
    hours = {"start": hhmm(now + timedelta(hours=5)), "end": hhmm(now + timedelta(hours=6))}
    chosen(client, headers, quiet_hours=hours, time_zone="UTC")


# The settings


def test_settings_show_every_kind_with_the_hubs_tier_no_quiet_hours_and_the_hubs_time_zone(client, hub):
    shown = settings(client, hub["owner"])
    assert shown["notices"] == [{"kind": "run_outputs", "on": True}] and shown["hide_details"] is False
    tiers = tiers_of(shown)
    assert list(tiers) == ["decision", "proposal", *runs.NOTICE_KINDS]
    assert {kind for kind, (tier, _) in tiers.items() if tier == INTERRUPT} == DEFAULT_INTERRUPTS
    assert {tier for kind, (tier, _) in tiers.items() if kind not in DEFAULT_INTERRUPTS} == {BRIEF}
    assert not any(was_chosen for _, was_chosen in tiers.values())
    assert (shown["quiet_hours"], shown["time_zone"], shown["time_zone_chosen"]) == (None, "Asia/Ho_Chi_Minh", False)
    assert shown["digest_at"] == "07:30"


def test_the_time_zone_of_a_member_who_chose_none_is_the_hubs_web_time_zone(hub_db, tmp_path, github):
    with make_client(hub_db, tmp_path, github, web_time_zone="Europe/Berlin") as client:
        headers = members(client, github)
        assert settings(client, headers["owner"])["time_zone"] == "Europe/Berlin"
    assert load_config({"EVO_HUB_DSN": "postgresql://hub@db/hub"}).web_time_zone == "Asia/Ho_Chi_Minh"
    env = {"EVO_HUB_DSN": "postgresql://hub@db/hub", "EVO_HUB_WEB_TIME_ZONE": "Asia/Tokyo"}
    assert load_config(env).web_time_zone == "Asia/Tokyo"
    with pytest.raises(ConfigError, match="EVO_HUB_WEB_TIME_ZONE"):
        load_config({**env, "EVO_HUB_WEB_TIME_ZONE": "not a zone"})


def test_a_member_sets_tiers_quiet_hours_time_zone_and_summary_and_a_put_leaves_the_rest(client, hub, hub_db):
    shown = chosen(
        client,
        hub["owner"],
        tiers=[{"kind": "plan_finished", "tier": "interrupt"}, {"kind": "decision", "tier": "off"}],
        quiet_hours={"start": "22:00", "end": "07:00"},
        time_zone="Asia/Tokyo",
        digest_at="08:15",
    )
    tiers = tiers_of(shown)
    assert (tiers["plan_finished"], tiers["decision"], tiers["run_failed"]) == (
        ("interrupt", True),
        ("off", True),
        ("interrupt", False),
    )
    assert shown["quiet_hours"] == {"start": "22:00", "end": "07:00"}
    assert (shown["time_zone"], shown["time_zone_chosen"], shown["digest_at"]) == ("Asia/Tokyo", True, "08:15")
    assert settings(client, hub["owner"]) == shown

    later = chosen(client, hub["owner"], hide_details=True, tiers=[{"kind": "release", "tier": "brief"}])
    assert later["hide_details"] is True and later["quiet_hours"] == shown["quiet_hours"]
    assert tiers_of(later)["decision"] == ("off", True) and tiers_of(later)["release"] == ("brief", True)
    muted = chosen(client, hub["owner"], notices=[{"kind": "run_outputs", "on": False}])
    assert muted["notices"] == [{"kind": "run_outputs", "on": False}], "turning run_outputs off still works"
    assert {k: v for k, v in muted.items() if k != "notices"} == {k: v for k, v in later.items() if k != "notices"}

    back = chosen(
        client,
        hub["owner"],
        tiers=[{"kind": "decision", "tier": None}],
        quiet_hours=None,
        time_zone=None,
        digest_at=None,
    )
    assert tiers_of(back)["decision"] == ("interrupt", False) and tiers_of(back)["plan_finished"] == ("interrupt", True)
    assert (back["quiet_hours"], back["time_zone"], back["time_zone_chosen"]) == (None, "Asia/Ho_Chi_Minh", False)
    assert back["digest_at"] == "07:30" and back["hide_details"] is True

    others = settings(client, hub["other"])
    assert others["quiet_hours"] is None and not any(was for _, was in tiers_of(others).values())
    audit = tables.audit
    targets = [row[0] for row in sql(hub_db, select(audit.c.target).where(audit.c.action == "notification.settings"))]
    assert targets[0] == (
        "notifications:settings tier:plan_finished=interrupt tier:decision=off quiet_hours=22:00-07:00 "
        "time_zone=Asia/Tokyo digest_at=08:15"
    )
    assert targets[-1] == (
        "notifications:settings tier:decision=default quiet_hours=none time_zone=default digest_at=default"
    )


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"tiers": []},
        {"hide_details": None},
        {"tiers": [{"kind": "no_such_kind", "tier": "brief"}]},
        {"tiers": [{"kind": "decision", "tier": "loud"}]},
        {"tiers": [{"kind": "decision"}]},
        {"tiers": [{"kind": "decision", "tier": "off"}, {"kind": "decision", "tier": "brief"}]},
        {"quiet_hours": {"start": "22:00", "end": "22:00"}},
        {"quiet_hours": {"start": "24:00", "end": "07:00"}},
        {"quiet_hours": {"start": "22:00"}},
        {"time_zone": "Mars/Olympus_Mons"},
        {"time_zone": "not a zone"},
        {"time_zone": "A" * 65},
        {"digest_at": "7:30"},
    ],
)
def test_a_put_of_no_setting_or_a_wrong_one_gets_422_and_changes_nothing(client, hub, body):
    before = settings(client, hub["owner"])
    response = put(client, hub["owner"], **body)
    assert response.status_code == 422, response.text
    assert settings(client, hub["owner"]) == before


# What is pushed


def test_the_hubs_tiers_push_a_decision_asked_of_the_member_their_failed_run_a_failed_release_and_merge_paused(
    client, hub, hub_db, service
):
    for who in ("owner", "other"):
        added(client, hub[who], browser(who).body())
    worker, run = started(client, hub)
    decision = asked(client, worker, run["id"])
    assert notice(client, worker, run["id"], kind="run_failed", title="Run failed").status_code == 201
    assert notice(client, worker, run["id"], **PUSHED).status_code == 201
    owner, other, project = user_id(hub_db, OWNER), user_id(hub_db, OTHER), project_id(hub_db)
    common = {"kind": "notice", "project_id": project}
    notified(
        client, user_id=owner, notice_kind="release", title="Released 1.0", details={"event": "published"}, **common
    )
    notified(client, user_id=owner, notice_kind="release", title="CI unread", details={"event": "actions"}, **common)
    notified(client, user_id=owner, notice_kind="merge_paused", title="Queue stopped", **common)
    notified(client, user_id=owner, notice_kind="chat_mention", title="Mentioned", **common)
    notified(client, user_id=other, notice_kind="run_failed", title="Not theirs", run_id=run["id"], **common)
    question = {"kind": "decision", "project_id": project, "run_id": run["id"], "decision_id": decision["id"]}
    notified(client, user_id=other, title="Asked of the owner", **question)
    deliver(client)
    pushed = pushes_of(hub_db)
    assert set(pushed) == {decision_title(hub_db, decision["id"]), "Run failed", "CI unread", "Queue stopped"}
    assert all(states == ["delivered"] for states in pushed.values())
    titles = {item["title"] for item in inbox(client, hub["owner"])}
    assert {"Released 1.0", "Mentioned", PUSHED["title"]} <= titles, "the Inbox gets what is not pushed too"
    assert {"Not theirs", "Asked of the owner"} <= {item["title"] for item in inbox(client, hub["other"])}


def decision_title(db, decision_id: int) -> str:
    return sql(db, select(STORED.c.title).where(STORED.c.decision_id == decision_id))[0][0]


def inbox(client, headers) -> list[dict]:
    response = client.get("/v1/me/notifications", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["notifications"]


def test_a_tier_the_member_chose_holds_for_every_notification_of_its_kind_and_the_inbox_gets_them_all(
    client, hub, hub_db, service
):
    for who in ("owner", "other"):
        added(client, hub[who], browser(who).body())
    tiers = [{"kind": "decision", "tier": "off"}, {"kind": "plan_finished", "tier": "interrupt"}]
    chosen(client, hub["owner"], tiers=tiers + [{"kind": "release", "tier": "brief"}])
    chosen(client, hub["other"], tiers=[{"kind": "decision", "tier": "interrupt"}])
    worker, run = started(client, hub)
    first = asked(client, worker, run["id"])
    assert notice(client, worker, run["id"], kind="plan_finished", title="Plan done").status_code == 201
    owner, other, project = user_id(hub_db, OWNER), user_id(hub_db, OTHER), project_id(hub_db)
    notified(client, user_id=owner, kind="notice", notice_kind="release", title="CI unread", project_id=project)
    question = {"kind": "decision", "project_id": project, "run_id": run["id"], "decision_id": first["id"]}
    notified(client, user_id=other, title="A copy of the question", **question)
    deliver(client)
    assert set(pushes_of(hub_db)) == {"Plan done", "A copy of the question"}
    counted = client.get("/v1/me/notifications/count", headers=hub["owner"]).json()
    assert (counted["open_decisions"], counted["unread"]) == (1, 3), "the bell counts what is not pushed too"

    chosen(client, hub["owner"], tiers=[{"kind": "decision", "tier": None}])
    answered(client, hub["owner"], first["id"], option="postgres")
    second = asked(client, worker, run["id"], question="And the cache?")
    deliver(client)
    assert decision_title(hub_db, second["id"]) in pushes_of(hub_db), "back to the hub's tier"


def test_the_hubs_tier_is_brief_for_every_other_kind_a_kind_added_later_and_the_night_shifts_failed_runs():
    assert push_tiers.default_tier(Facts("a_kind_added_later")) == BRIEF
    assert push_tiers.kind_default("a_kind_added_later") == BRIEF
    assert push_tiers.default_tier(Facts("decision", asked_of_member=True)) == INTERRUPT
    assert push_tiers.default_tier(Facts("decision")) == BRIEF
    assert push_tiers.default_tier(Facts("run_failed", dispatched_by_member=True, run_kind="plan")) == INTERRUPT
    assert push_tiers.default_tier(Facts("run_failed", dispatched_by_member=True, run_kind="chat")) == INTERRUPT
    for kind in runs.CURATOR_KINDS:
        assert push_tiers.default_tier(Facts("run_failed", dispatched_by_member=True, run_kind=kind)) == BRIEF
    assert push_tiers.default_tier(Facts("run_failed", run_kind="plan")) == BRIEF
    assert push_tiers.default_tier(Facts("release", event="published")) == BRIEF
    assert push_tiers.default_tier(Facts("release", event="actions_permission")) == INTERRUPT
    assert push_tiers.default_tier(Facts("merge_paused")) == INTERRUPT
    for kind in ("proposal", "plan_finished", "push_default_branch", "curator_brief", "run_outputs"):
        assert push_tiers.default_tier(Facts(kind)) == BRIEF
    assert set(push_tiers.KINDS) == {"decision", "proposal", *runs.NOTICE_KINDS}


# Quiet hours


def test_quiet_hours_hold_a_push_until_they_end_and_it_goes_once_they_have(client, hub, hub_db, service):
    found = browser("laptop")
    added(client, hub["owner"], found.body())
    quiet_now(client, hub["owner"])
    worker, run = started(client, hub)
    assert notice(client, worker, run["id"], kind="run_failed", title="Run failed").status_code == 201
    assert deliver(client) == {"delivered": 1, "retried": 0, "failed": 0}, "the web's; the push is held"
    ((state, attempts, wait, error),) = device_rows(hub_db)
    assert (state, attempts, error) == ("pending", 0, None)
    assert 3 * 3600 - 120 < wait <= 3 * 3600
    assert deliver(client) == {"delivered": 0, "retried": 0, "failed": 0} and service.pushes == []

    quiet_later(client, hub["owner"])
    due_now(hub_db)
    assert deliver(client) == {"delivered": 1, "retried": 0, "failed": 0}
    assert len(service.to(found)) == 1


def test_quiet_hours_are_the_members_time_zones(client, hub, hub_db, service):
    added(client, hub["owner"], browser("laptop").body())
    tokyo = timedelta(hours=9)
    quiet_now(client, hub["owner"], zone="Asia/Tokyo", offset=tokyo)
    worker, run = started(client, hub)
    assert notice(client, worker, run["id"], kind="run_failed", title="Held in Tokyo").status_code == 201
    deliver(client)
    assert pushes_of(hub_db) == {"Held in Tokyo": ["pending"]} and service.pushes == []
    quiet_now(client, hub["owner"], zone="UTC", offset=tokyo)  # the same hours, in UTC: nine hours away from now
    due_now(hub_db)
    deliver(client)
    assert pushes_of(hub_db) == {"Held in Tokyo": ["delivered"]}


def test_quiet_hours_hold_a_decision_until_they_end_when_its_run_parks_a_day_later(client, hub, hub_db, service):
    added(client, hub["owner"], browser("laptop").body())
    quiet_now(client, hub["owner"])
    asked_decision(client, hub)
    deliver(client)
    ((state, _, wait, _),) = device_rows(hub_db)
    assert state == "pending" and 3 * 3600 - 120 < wait <= 3 * 3600 and service.pushes == []


@pytest.mark.parametrize(("wait_hours", "held_hours"), [(1, None), (4, 2), (4.5, 2.5)])
def test_quiet_hours_send_a_decision_whose_run_parks_within_two_hours_and_hold_another_until_then(
    hub_db, tmp_path, github, service, wait_hours, held_hours
):
    changes = {"vapid_private_key": VAPID_KEY, "decision_wait_seconds": int(wait_hours * 3600)}
    with make_client(hub_db, tmp_path, github, **changes) as client:
        headers = members(client, github)
        push(client, headers["owner"], plan_body())
        added(client, headers["owner"], browser("laptop").body())
        quiet_now(client, headers["owner"], ends_in=5)
        asked_decision(client, headers)
        deliver(client)
        ((state, _, wait, _),) = device_rows(hub_db)
        if held_hours is None:
            assert state == "delivered" and len(service.pushes) == 1, "its run parks within two hours"
        else:
            assert state == "pending" and service.pushes == []
            assert held_hours * 3600 - 60 < wait <= held_hours * 3600, "until two hours before its run parks"


def test_a_held_push_of_a_decision_answered_meanwhile_is_not_sent(client, hub, hub_db, service):
    added(client, hub["owner"], browser("laptop").body())
    quiet_now(client, hub["owner"])
    _, decision = asked_decision(client, hub)
    deliver(client)
    answered(client, hub["owner"], decision["id"], option="postgres")
    quiet_later(client, hub["owner"])
    due_now(hub_db)
    deliver(client)
    assert service.pushes == []
    assert [row[0::3] for row in device_rows(hub_db)] == [("failed", notifications.DECISION_CLOSED)]


def test_a_test_push_ignores_quiet_hours(client, hub, service):
    device = added(client, hub["owner"], browser("laptop").body())
    quiet_now(client, hub["owner"])
    response = client.post(f"/v1/me/push/devices/{device['id']}/test", headers=hub["owner"])
    assert response.status_code == 200, response.text
    assert len(service.pushes) == 1

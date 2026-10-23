"""The Growth section of a project's weekly bulletin and the channel facebook_group of the plugin marketing (plan
autonomous-oss-growth, step 20, criterion a17), on the hub with Postgres: a reader reads the week (each Goal's signal
against its target, the posts that went out, the breakers, the cost against the cap, the proposals for the blog
project); the admins are told once a week from Monday 08:00 UTC; a post for a Facebook group is a member's draft and
never goes out; the pages of both plugins are in their manifests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from psycopg import errors
from sqlalchemy import insert, select

from evo_agents.core_plugins.marketing import MANIFEST, settings, weekly
from evo_agents.hub import tables
from tests.hub import live, pg
from tests.hub.live import sql
from tests.hub.test_growth_marketer import project_id
from tests.hub.test_marketing_plugin import (  # noqa: F401 (fixtures)
    BASE,
    PROJECT,
    channel,
    client,
    github,
    members,
    mk,
    propose,
)
from tests.hub.test_tasks_plugin import configure

KEY = "gh.stars"
MONDAY = datetime(2026, 10, 26, 9, tzinfo=UTC)


def goal_with_signal(world, value: float) -> None:
    made = world.client.post(
        f"/v1/projects/{PROJECT}/signal-keys", json={"name": KEY, "kind": "number"}, headers=world.h["owner"]
    )
    assert made.status_code == 201, made.text
    metric = {"key": KEY, "aggregate": "last", "comparator": ">=", "target": 100}
    written = world.client.put(
        f"/v1/projects/{PROJECT}/goals/G-1",
        json={"statement": "People star the repo", "metric": metric},
        headers=world.h["owner"],
    )
    assert written.status_code in (200, 201), written.text
    pushed = world.client.post(
        f"/v1/signals/{PROJECT}", json={"items": [{"key": KEY, "value": value}]}, headers=world.h["alice"]
    )
    assert pushed.status_code == 202 and pushed.json()["accepted"] == 1, pushed.text


def test_growth_weekly_a_reader_reads_goals_posts_breakers_cost_and_proposals(mk):
    configure(mk.client, mk.h["owner"], "marketing", {"proposals_project": "team-blog", "budget_usd": 20})
    goal_with_signal(mk, 42)
    out = propose(mk, github(mk))
    assert out["state"] == "posted"
    group = channel(mk, "facebook_group", "alice-group", who="alice", kind="personal", mode="draft_only")
    kept = propose(mk, group, "Bài cho nhóm", route="drafts")
    assert kept["state"] == "draft"
    x = channel(mk, "x", "evo")
    paused = mk.client.post(f"{BASE}/channels/{x['id']}/pause", json={"reason": "two failures"}, headers=mk.h["owner"])
    assert paused.status_code == 200, paused.text

    answer = mk.client.get(f"{BASE}/weekly", headers=mk.h["eve"])  # a reader
    assert answer.status_code == 200, answer.text
    week = answer.json()
    (goal,) = week["goals"]
    assert (goal["code"], goal["value"], goal["target"], goal["reached"]) == ("G-1", 42, 100, False)
    assert goal["metric"].startswith(f"{KEY} last over")
    assert [(p["id"], p["channel"]) for p in week["posted"]] == [(out["id"], "github")]
    assert week["counts"]["posted"] == 1 and week["counts"]["draft"] == 1
    assert [(b["channel"], b["reason"]) for b in week["breakers"]] == [("x", "two failures")]
    assert week["budget"]["budget_usd"] == 20 and week["killed"] is False
    assert [(p["id"], p["channel"], p["state"]) for p in week["proposals"]] == [(kept["id"], "facebook_group", "draft")]
    assert week["proposals_project"] == "team-blog"
    assert "body" not in week["proposals"][0]  # a reader reads no post's text


def test_growth_weekly_tells_the_admins_once_a_week_from_monday_eight_utc(mk):
    engine, config = mk.client.app.state.engine, mk.client.app.state.config
    pid = project_id(mk)

    def send(at: datetime) -> bool:
        async def go():
            async with engine.begin() as conn:
                cfg = await settings.config(conn, config, PROJECT)
                return await weekly.send(conn, cfg, PROJECT, pid, at)

        return mk.client.portal.call(go)

    assert not send(MONDAY - timedelta(hours=2))  # Monday 07:00
    assert not send(MONDAY - timedelta(days=1))  # Sunday
    assert send(MONDAY)
    assert not send(MONDAY + timedelta(hours=3))  # told this week already
    n = tables.notifications.c
    told = sql(mk.db, select(n.title, n.body, n.link, n.details).where(n.notice_kind == "plugin_notice"))
    assert len(told) == 1  # the project's one admin
    title, body, link, details = told[0]
    assert "2026-W44" in title and details["weekly"] == "2026-W44"
    assert link == f"/p/{PROJECT}/x/marketing/weekly"
    for part in (
        "Growth",
        "Posts that went out: 0",
        "Breakers open: 0",
        "Cost:",
        "config proposals_project is not set",
    ):
        assert part in body
    assert send(MONDAY + timedelta(days=7))  # the next week


def test_growth_facebook_group_is_a_members_draft_and_never_goes_out(mk):
    refused = mk.client.post(
        f"{BASE}/channels", json={"channel": "facebook_group", "account": "g", "kind": "project"}, headers=mk.h["owner"]
    )
    assert refused.status_code == 422
    group = channel(mk, "facebook_group", "alice-group", who="alice", kind="personal", mode="draft_only")
    switched = mk.client.put(f"{BASE}/channels/{group['id']}/mode", json={"mode": "autonomous"}, headers=mk.h["alice"])
    assert switched.status_code == 422
    command = mk.client.post(f"{BASE}/posts", json={"channel_id": group["id"], "body": "x"}, headers=mk.h["alice"])
    assert command.status_code == 403
    assert mk.platform.sent == []


def test_growth_pages_of_crm_and_marketing_are_native_and_named():
    from evo_agents.core_plugins.crm import MANIFEST as CRM

    assert [page["name"] for page in MANIFEST["contributes"]["pages"]] == ["overview", "posts", "weekly"]
    assert [page["name"] for page in CRM["contributes"]["pages"]] == ["contacts"]
    for page in [*MANIFEST["contributes"]["pages"], *CRM["contributes"]["pages"]]:
        assert "entry" not in page and page["title"] and page["description"]


@pytest.mark.empty_db
def test_0128_takes_facebook_group_for_a_personal_draft_only_account_and_going_back_deletes_them(hub_db):
    from tests.hub.test_migrate import ALL, move_to, one, seed

    move_to(hub_db, "0127")
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    t = tables.marketing_channels
    group = {"project_id": ids["project"], "channel": "facebook_group", "account": "g", "account_kind": "personal"}
    group = {**group, "owner_id": ids["user"]}
    with live.connect(hub_db) as conn, pytest.raises(errors.CheckViolation):
        conn.execute(insert(t).values(group))  # not a channel yet
    move_to(hub_db, "0128")
    with live.connect(hub_db) as conn:
        assert one(conn, insert(t).values(group).returning(t.c.id))
        with pytest.raises(errors.CheckViolation):
            conn.execute(insert(t).values({**group, "account": "h", "mode": "autonomous"}))
    with live.connect(hub_db) as conn, pytest.raises(errors.CheckViolation):
        conn.execute(insert(t).values({**group, "account": "p", "account_kind": "project", "owner_id": None}))
    move_to(hub_db, "0127", down=True)
    with live.connect(hub_db) as conn:
        assert conn.execute(select(t.c.id)).all() == []
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0128") :]

"""The reminders of an opportunity's window (plan opportunity-intake, criterion a4;
``evo_agents.hub.server.opportunity_deadlines``, the job ``hub.opportunity_deadlines``) and
``hub opportunity list --closing-within``: the owner of an open opportunity with a window gets one notice at 14 days
left, one at 3 and one the day after, each once per value of ``window_until``; moving the date counts the milestones
again; closing the opportunity ends them; the hub never changes a status. The date of a pass is given, so no test waits.

The milestone arithmetic runs without Postgres; everything that needs the hub skips without EVO_HUB_TEST_DSN."""

from __future__ import annotations

from datetime import date, timedelta
from functools import partial

import pytest

pytest.importorskip("sqlalchemy")

from evo_agents.hub import jobs, runs
from evo_agents.hub.contract import commands
from evo_agents.hub.server.opportunity_deadlines import milestone, remind
from tests.hub import pg
from tests.hub.test_opportunities import DEPOT, _close, _get, _put

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
END = date(2026, 12, 1)

if pg.DSN:
    from tests.hub.test_autonomy_policy import hub  # noqa: F401 (a fixture)


def test_opportunity_window_milestones_follow_the_days_left():
    days = {offset: milestone(END, END - timedelta(days=offset)) for offset in (30, 15, 14, 4, 3, 1, 0, -1, -40)}
    assert days == {
        30: None,
        15: None,
        14: "due_14",
        4: "due_14",
        3: "due_3",
        1: "due_3",
        0: "due_3",
        -1: "past",
        -40: "past",
    }


def test_opportunity_window_notice_and_job_are_registered():
    assert "opportunity_window" in runs.NOTICE_KINDS
    assert jobs.OPPORTUNITY_DEADLINES == "hub.opportunity_deadlines"
    assert "--closing-within" in str(commands()["hub opportunity list"])


def _pass(hub, today: date) -> dict:
    return hub.client.portal.call(partial(remind, hub.client.app.state.engine, today=today))


def _notices(hub, who: str = "owner") -> list[dict]:
    found = hub.client.get("/v1/me/notifications", params={"limit": 100}, headers=hub.headers[who])
    assert found.status_code == 200, found.text
    return [n for n in found.json()["notifications"] if n["notice_kind"] == "opportunity_window"]


@needs_pg
def test_opportunity_window_owner_is_told_once_per_milestone_and_a_moved_date_counts_again(hub):
    assert _put(hub, "owner", "O-1", DEPOT).status_code == 200  # the window ends 2026-12-01
    assert _put(hub, "owner", "O-2", {**DEPOT, "statement": "No window", "window_until": None, "if_revision": 0}).json()
    assert _pass(hub, END - timedelta(days=20)) == {}
    assert _pass(hub, END - timedelta(days=14)) == {"due_14": 1}
    assert _pass(hub, END - timedelta(days=13)) == {}  # once
    first = _notices(hub)
    assert [n["details"] for n in first] == [{"code": "O-1", "window_until": "2026-12-01", "milestone": "due_14"}]
    assert first[0]["title"] == "Window of O-1 closes on 2026-12-01: 14 days left"
    assert _notices(hub, "reader") == []  # only the owner
    assert _pass(hub, END - timedelta(days=3)) == {"due_3": 1}
    assert _pass(hub, END - timedelta(days=2)) == {}
    assert _pass(hub, END + timedelta(days=1)) == {"past": 1}
    assert _pass(hub, END + timedelta(days=9)) == {}
    late = [n for n in _notices(hub) if n["details"]["milestone"] == "past"][0]
    assert "captured or missed" in late["body"] and "still open" in late["title"]
    assert _get(hub, "reader", "/O-1")["status"] == "open"  # the hub changed nothing
    # the window moves: the milestones count again for the new date
    moved = _put(hub, "owner", "O-1", {**DEPOT, "window_until": "2027-01-10", "if_revision": 1})
    assert moved.status_code == 200, moved.text
    assert _pass(hub, END + timedelta(days=9)) == {}  # 32 days left
    assert _pass(hub, date(2026, 12, 27)) == {"due_14": 1}
    assert _pass(hub, date(2026, 12, 27)) == {}
    assert len(_notices(hub)) == 4
    # closed: no more
    assert _close(hub, "owner", "O-1", {"status": "captured", "reason": "signed", "if_revision": 2}).status_code == 200
    assert _pass(hub, date(2027, 1, 20)) == {}


@needs_pg
def test_opportunity_window_a_pass_after_a_gap_sends_the_latest_milestone_alone(hub):
    assert _put(hub, "owner", "O-1", DEPOT).status_code == 200
    assert _pass(hub, END - timedelta(days=2)) == {"due_3": 1}  # due_14 is never sent late
    assert [n["details"]["milestone"] for n in _notices(hub)] == ["due_3"]


@needs_pg
def test_opportunity_window_list_closing_within_gives_the_open_ones_nearest_end_first(hub):
    today = hub.client.portal.call(partial(_today, hub))
    near, far = today + timedelta(days=5), today + timedelta(days=40)
    for ref, until in (("O-1", far), ("O-2", near), ("O-3", today - timedelta(days=2)), ("O-4", None)):
        body = {**DEPOT, "statement": ref, "window_until": until.isoformat() if until else None, "if_revision": 0}
        assert _put(hub, "owner", ref, body).status_code == 200, ref
    assert _close(hub, "owner", "O-3", {"status": "missed", "reason": "late", "if_revision": 1}).status_code == 200
    assert _put(hub, "owner", "O-5", {**DEPOT, "statement": "O-5", "window_until": str(near), "if_revision": 0})
    assert [o["code"] for o in _get(hub, "reader", "?closing_within=10")] == ["O-2", "O-5"]
    assert [o["code"] for o in _get(hub, "reader", "?closing_within=60")] == ["O-2", "O-5", "O-1"]
    assert [o["code"] for o in _get(hub, "reader", "?closing_within=0")] == []
    assert [o["code"] for o in _get(hub, "reader")] == ["O-1", "O-2", "O-3", "O-4", "O-5"]  # by rank, as before


async def _today(hub) -> date:
    from sqlalchemy import select

    from evo_agents.hub.server.opportunity_deadlines import today_utc

    async with hub.client.app.state.engine.begin() as conn:
        return await conn.scalar(select(today_utc()))

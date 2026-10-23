"""The weekly report of the Curator's probes (plan correction-recurrence, step 3, criterion a4): for each ISO week, probe
and model, arm on and arm off (sessions and failed), their 1 - pass^k, the status and the week before's; the week's
cost against probes.budget_usd; the merged changes born of a correction without a probe, with why. On Monday at the
charter's brief_at the owner of the night shift gets it once as a notice curator_probe_report; the API, `evo-agents hub
curator probe report --json` and the MCP tool curator_probe_report answer the same. Every name here is made up.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial

import pytest

from evo_agents.hub import probes
from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, insert, select, update

from evo_agents.hub import runs, tables
from evo_agents.hub.probe_cli import REPORT_KEYS
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.brief import send_briefs
from tests.hub.contract_keys import assert_json_keys
from tests.hub.live import sql
from tests.hub.test_credentials_api import app_key  # noqa: F401 (a fixture)
from tests.hub.test_curator import PROJECT, written
from tests.hub.test_curator_changes import curator_charter, path, world  # noqa: F401 (world is a fixture)
from tests.hub.test_probe_runs import OPUS, SONNET, WEEK, probe_run, probing, report, session
from tests.hub.test_probes import corrections_cited
from tests.hub.test_run_cli import as_json, cli, ok, write_credentials
from tests.hub.test_run_stream import serving
from tests.hub.test_runs import moved


@pytest.mark.parametrize(
    ("runs", "failed", "k", "rate"),
    [(3, 0, 3, 0.0), (3, 1, 3, 1.0), (3, 3, 3, 1.0), (4, 1, 2, 0.5), (2, 1, 3, 1 - 0.5**3), (0, 0, 3, None)],
)
def test_probe_recurrence_is_one_minus_pass_hat_k(runs, failed, k, rate):
    found = probes.recurrence(runs, failed, k)
    assert found == (None if rate is None else pytest.approx(rate))


@pytest.mark.parametrize(
    ("state", "on_failed", "off_runs", "off_failed", "status"),
    [
        ("done", 0, 3, 2, "holds"),
        ("done", 1, 3, 3, "recurring"),
        ("done", 2, 3, 0, "recurring"),  # arm on failing is recurring whatever arm off did
        ("done", 0, 3, 0, "not_reproduced"),
        ("stale", 0, 0, 0, "stale"),
        ("unmeasured", 0, 0, 0, "unmeasured"),
        ("failed", 0, 1, 1, "unmeasured"),
        ("queued", 0, 0, 0, "unmeasured"),
        (None, 0, 0, 0, "unmeasured"),
    ],
)
def test_probe_status_of_a_weeks_row(state, on_failed, off_runs, off_failed, status):
    assert probes.status_of(state, on_failed, off_runs, off_failed) == status


LAST_WEEK = WEEK - timedelta(days=7)
# Monday 2026-10-12, 06:30 in Asia/Ho_Chi_Minh (UTC+7): the charter's brief_at, after the week of 2026-10-05
MONDAY = datetime(2026, 10, 11, 23, 30, tzinfo=UTC)


def measured(world) -> dict:
    """The probe on two models: this week's run on opus with arm on passing twice and arm off failing twice, costing
    $0.40, sonnet not run; last week's opus row failing once on arm on. The probe."""
    probe = probing(world, models=(OPUS, SONNET), k=2)
    pr, pw = tables.curator_probes, tables.curator_probe_runs
    made = datetime(2026, 9, 1, tzinfo=UTC)
    sql(world.db, update(pr).where(pr.c.id == probe["id"]).values(created_at=made))
    spec = probe_run(world)
    key = spec["curator"]["probe_key"]
    for arm, index in (("on", 1), ("off", 1), ("on", 2), ("off", 2)):
        assert report(world, spec["id"], session(arm, index, passed=arm == "on"), key).status_code == 200
    moved(world.client, world.worker, spec["id"], "verifying", "done")
    r = tables.runs
    sql(world.db, update(r).where(r.c.id == spec["id"]).values(usage={"total_cost_usd": 0.4}))
    ((project_id,),) = sql(world.db, select(pw.c.project_id))
    earlier = [
        {"arm": "on", "index": 1, "passed": False},
        {"arm": "on", "index": 2, "passed": True},
        {"arm": "off", "index": 1, "passed": False},
        {"arm": "off", "index": 2, "passed": False},
    ]
    row = {"project_id": project_id, "probe_id": probe["id"], "change_id": probe["change_id"], "week": LAST_WEEK}
    row |= {"runtime": "claude-code", "model": "opus", "k": 2, "state": "done", "sessions": earlier}
    sql(world.db, insert(pw).values(row))
    return probe


def asked(world, who: str = "owner", **params) -> dict:
    found = world.client.get(path("probe-report"), params=params, headers=world.headers[who])
    assert found.status_code == 200, found.text
    return found.json()


def test_probe_report_counts_each_arm_its_status_the_week_before_and_the_weeks_cost(world):
    probe = measured(world)
    found = asked(world, weeks=2, week=str(WEEK + timedelta(days=3)))
    assert (found["project"], found["k"], found["budget_usd"], found["fixes"]) == (PROJECT, 2, 3.0, [])
    this, before = found["weeks"]
    assert (this["week"], before["week"], this["complete"]) == (WEEK.isoformat(), LAST_WEEK.isoformat(), True)
    assert this["cost_usd"] == pytest.approx(0.4) and this["budget_usd"] == 3.0
    assert before["cost_usd"] == 0
    opus, sonnet = this["probes"]
    assert (opus["probe_id"], opus["change_id"], opus["model"], opus["k"]) == (
        probe["id"],
        probe["change_id"],
        "opus",
        2,
    )
    assert opus["on"] == {"runs": 2, "failed": 0, "recurrence": 0.0}
    assert opus["off"] == {"runs": 2, "failed": 2, "recurrence": 1.0}
    assert (opus["status"], opus["previous"], opus["reason"]) == ("holds", "recurring", None)
    assert opus["run_id"] is not None
    assert (sonnet["model"], sonnet["status"], sonnet["previous"]) == ("sonnet", "unmeasured", "unmeasured")
    assert sonnet["reason"] == "no probe run measured it this week" and sonnet["on"]["runs"] == 0
    (last,) = [item for item in before["probes"] if item["model"] == "opus"]
    assert (last["status"], last["on"]["failed"], last["on"]["recurrence"]) == ("recurring", 1, 1.0)
    assert this["totals"] == {
        "probes": 2,
        "holds": 1,
        "recurring": 0,
        "not_reproduced": 0,
        "stale": 0,
        "unmeasured": 1,
        "on": {"runs": 2, "failed": 0, "recurrence": None},
        "off": {"runs": 2, "failed": 2, "recurrence": None},
    }
    assert "verify" not in str(found) and probes.TRACE_ENV not in str(found)
    assert asked(world, "other", weeks=1, week=WEEK.isoformat())["weeks"][0]["probes"] == this["probes"]
    assert world.client.get(path("probe-report")).status_code == 401
    assert (
        world.client.get(path("probe-report"), params={"weeks": 53}, headers=world.headers["owner"]).status_code == 422
    )


def test_probe_report_names_a_change_born_of_a_correction_without_a_probe_and_why(world):
    probe = measured(world)
    p, c = tables.proposals, tables.curator_changes
    ((proposal_id, run_id),) = sql(
        world.db, select(p.c.id, p.c.run_id).join(c, c.c.proposal_id == p.c.id).where(c.c.id == probe["change_id"])
    )
    corrections_cited(world, run_id)
    assert asked(world)["unprobed"] == []  # it has an active probe
    retired = world.client.post(path("probes", str(probe["id"]), "retire"), json={}, headers=world.headers["owner"])
    assert retired.status_code == 200, retired.text
    (item,) = asked(world)["unprobed"]
    assert (item["change_id"], item["proposal_id"]) == (probe["change_id"], proposal_id)
    assert "no file a probe arm can place" in item["reason"]  # tests/test_wait.py: a probe cannot measure it
    agents = [{"repo": "evo-agents", "path": "AGENTS.md"}]
    sql(world.db, update(p).where(p.c.id == proposal_id).values(paths=agents))
    (item,) = asked(world)["unprobed"]
    assert item["reason"] == f"its probe #{probe['id']} was retired"


def briefed(world, now: datetime) -> dict:
    return world.client.portal.call(partial(send_briefs, world.app.state.engine, now=now))


def notices(world, who: str = "owner") -> list[dict]:
    found = world.client.get("/v1/me/notifications", params={"kind": "notice"}, headers=world.headers[who])
    return [item for item in found.json()["notifications"] if item["notice_kind"] == runs.PROBE_REPORT_NOTICE]


def test_probe_report_goes_to_the_night_shifts_owner_on_monday_at_brief_at_once_a_week(world):
    measured(world)
    assert "probe_report" not in briefed(world, MONDAY - timedelta(minutes=1))
    assert "probe_report" not in briefed(world, MONDAY - timedelta(days=1))  # a Sunday's brief has no report
    assert notices(world) == []
    assert briefed(world, MONDAY).get("probe_report") == 1
    assert briefed(world, MONDAY + timedelta(minutes=1)).get("probe_report_sent") == 1
    (note,) = notices(world)
    assert note["title"].startswith(f"Probe report of {PROJECT}, week of {WEEK.isoformat()}: 0 recurring")
    assert (note["project"], note["link"]) == (PROJECT, f"/p/{PROJECT}/curator")
    details = note["details"]
    assert (details["week"], details["holds"], details["unmeasured"], details["probes"]) == (WEEK.isoformat(), 1, 1, 2)
    assert details["cost_usd"] == pytest.approx(0.4) and details["budget_usd"] == 3.0
    assert "unmeasured (no probe run measured it this week)" in note["body"]
    assert "verify" not in note["body"]
    assert notices(world, "other") == []
    n, d = tables.notifications, tables.notification_deliveries
    ((delivered,),) = sql(
        world.db,
        select(func.count())
        .select_from(d)
        .join(n, n.c.id == d.c.notification_id)
        .where(n.c.notice_kind == runs.PROBE_REPORT_NOTICE),
    )
    assert delivered >= 1  # on the web and every channel of the owner's, Telegram included
    assert "probe_report" not in briefed(world, MONDAY + timedelta(days=7, minutes=1)) or len(notices(world)) == 2


def test_a_charter_without_probes_sends_no_probe_report(world):
    measured(world)
    written(world.client, world.headers["owner"], curator_charter())
    found = briefed(world, MONDAY)
    assert not any(key.startswith("probe_report") for key in found) and notices(world) == []


def test_curator_probe_report_on_the_command_line_and_the_mcp_tool(world, tmp_path, monkeypatch, capsys):
    measured(world)
    with serving(create_app(world.app.state.config)) as url:
        home = write_credentials(
            tmp_path / "owner", url, "owner", world.headers["owner"]["Authorization"].removeprefix("Bearer ")
        )

        def curator(*args: str):
            return cli(monkeypatch, capsys, home, "hub", "curator", "probe", "report", *args, "--project", PROJECT)

        found = as_json(curator("--weeks", "2", "--week", WEEK.isoformat(), "--json"))
        assert_json_keys("hub curator probe report", found)
        assert set(found) == set(REPORT_KEYS)
        week = found["weeks"][0]
        assert {"week", "cost_usd", "probes"} <= set(week)
        assert {"change_id", "probe_id", "model", "on", "off", "status"} <= set(week["probes"][0])
        assert {"runs", "failed"} <= set(week["probes"][0]["on"])
        assert found == asked(world, weeks=2, week=WEEK.isoformat())
        printed = ok(curator("--week", WEEK.isoformat())).out
        assert f"week of {WEEK.isoformat()}: 1 holds" in printed and "unmeasured" in printed
    text, data = world.client.portal.call(partial(_mcp, world, {"weeks": 1, "week": WEEK.isoformat()}))
    assert f"Week of {WEEK.isoformat()}" in text and "holds" in text and "1 - pass^k" in text
    assert data["weeks"][0]["probes"] == found["weeks"][0]["probes"]


async def _mcp(world, arguments: dict):
    from starlette.requests import Request

    from evo_agents.hub.server import probe_report
    from evo_agents.hub.server.security import MACHINE, Principal

    ((owner_id,),) = sql(world.db, select(tables.users.c.id).where(tables.users.c.login == "owner"))
    user = Principal(user_id=owner_id, login="owner", admin=False, token_id=0, kind=MACHINE, token_hash="")
    request = Request({"type": "http", "app": world.app, "headers": []})
    return await probe_report.mcp_answer(request, PROJECT, user, arguments, sink=None)

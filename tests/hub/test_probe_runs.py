"""The probe runs of the Curator on the hub (plan correction-recurrence, step 2): the night shift queues one after the
night's other kinds for each active probe of a merged change and each model of the charter once an ISO week, the one
measured longest ago first, on a worker that takes probe runs, within the week's probes.budget_usd; its claim hands the
probe and a key of its own; each session's result posts with that key; a run that ends short is queued again once,
then fails; a pair the week's money does not reach is recorded unmeasured. Every name here is made up.
"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import psycopg
from sqlalchemy import func, insert, select, update

from evo_agents.hub import probes, runs, tables
from tests.hub.live import sql
from tests.hub.test_credentials_api import app_key  # noqa: F401 (a fixture)
from tests.hub.test_curator import NIGHT, fire, written
from tests.hub.test_curator_changes import (  # noqa: F401 (world is a fixture)
    accepted,
    curator_charter,
    one_change,
    path,
    world,
)
from tests.hub.test_migrate import move_to
from tests.hub.test_runs import claim, moved

MERGE = "3" * 40
BASE = "4" * 40
TIP = "5" * 40
PROBE = {
    "repo": "evo-agents",
    "commit": "abc1234",
    "task": "Wait for the fake build of widget-lab to finish, then read its log.",
    "verify": "python -m pytest -q tests/test_wait.py",
}
OPUS = {"runtime": "claude-code", "model": "opus"}
SONNET = {"runtime": "claude-code", "model": "sonnet"}
WEEK = probes.week_of(NIGHT.date())  # NIGHT is a Thursday: its week starts on Monday 2026-10-05


def probing(world, *, models=(OPUS,), budget: float = 3.0, k: int = 2) -> dict:
    """The charter with probes, an accepted change merged, and an admin's probe of it; the probe."""
    written(
        world.client,
        world.headers["owner"],
        curator_charter(probes={"k": k, "budget_usd": budget, "models": list(models)}),
    )
    accepted(world)
    change = one_change(world)
    c = tables.curator_changes
    merged = {"state": "merged", "merged_at": func.now(), "merge_sha": MERGE}
    sql(world.db, update(c).where(c.c.id == change["id"]).values(**merged))
    added = world.client.post(path("probes"), json={**PROBE, "change_id": change["id"]}, headers=world.headers["owner"])
    assert added.status_code == 201, added.text
    return added.json()


def rows(world) -> list:
    pw = tables.curator_probe_runs
    names = [column.name for column in pw.columns]
    return [
        SimpleNamespace(**dict(zip(names, row, strict=True))) for row in sql(world.db, select(pw).order_by(pw.c.id))
    ]


def session(arm: str, index: int, *, passed: bool = True, model: str = "opus") -> dict:
    found = {"arm": arm, "index": index, "passed": passed, "exit_code": 0 if passed else 1, "cost_usd": 0.1}
    found |= {
        "turns": 4,
        "duration_ms": 1200,
        "runtime": "claude-code",
        "model": model,
        "session_id": f"s-{arm}{index}",
    }
    return {"base_sha": BASE, "harness_sha": TIP, "session": found}


def report(world, run_id: int, body: dict, key: str | None):
    headers = {**world.worker["headers"], **({probes.PROBE_KEY_HEADER: key} if key is not None else {})}
    return world.client.post(f"/v1/worker/runs/{run_id}/probe", json=body, headers=headers)


def night(world, now) -> dict:
    """The night shift at ``now``, the night's review run (which a later night queues first) claimed and ended done
    first; what its last pass did."""
    found = fire(world.client, now)
    while found.get("review"):
        spec = claim(world.client, world.worker)
        assert spec["kind"] == "review", spec
        moved(world.client, world.worker, spec["id"], "running", "verifying", "done")
        found = fire(world.client, now)
    return found


def probe_run(world, now=NIGHT) -> dict:
    """The probe run the night shift queues at ``now``, claimed and running; its spec."""
    assert night(world, now).get("probe") == 1
    spec = claim(world.client, world.worker)
    assert spec["kind"] == "probe", spec
    moved(world.client, world.worker, spec["id"], "running")
    return spec


def test_probe_run_queues_after_the_nights_other_runs_and_its_claim_hands_the_probe_and_a_key(world):
    probe = probing(world)
    assert fire(world.client, NIGHT).get("probe") == 1
    spec = claim(world.client, world.worker)
    assert spec["kind"] == "probe" and spec["model"] == "opus" and spec["runtime"] == "claude-code"
    curator_spec = spec["curator"]
    assert curator_spec["role"] == "probe" and curator_spec["probe_key"]
    sent = curator_spec["probe"]
    assert (sent["probe_id"], sent["repo"], sent["k"], sent["merge_sha"]) == (probe["id"], "evo-agents", 2, MERGE)
    assert sent["verify"] == PROBE["verify"] and sent["task"] == PROBE["task"]
    assert date.fromisoformat(sent["week"]) == WEEK
    assert PROBE["task"] in spec["prompt"] and PROBE["verify"] not in spec["prompt"]  # the agent sees the task alone
    assert spec["budget"]["max_usd"] <= 3.0
    (row,) = rows(world)
    assert (row.state, row.run_id, row.k, row.week) == ("running", spec["id"], 2, WEEK)
    assert row.probe_key != curator_spec["probe_key"]  # the hub keeps its SHA-256 alone
    r = tables.runs
    ((schedule_id, plan_id),) = sql(world.db, select(r.c.schedule_id, r.c.plan_id).where(r.c.id == spec["id"]))
    assert schedule_id is not None and plan_id == one_change(world)["plan_id"]
    events = sql(world.db, select(tables.run_events.c.body).where(tables.run_events.c.run_id == spec["id"]))
    assert events and all(PROBE["verify"] not in str(event[0]) for event in events)  # no event holds the answer key
    assert fire(world.client, NIGHT).get("probe") is None  # one probe run at a time


def test_probe_run_sessions_post_with_the_runs_key_alone_and_the_last_ends_the_week(world):
    probing(world)
    spec = probe_run(world)
    run_id, key = spec["id"], spec["curator"]["probe_key"]
    assert report(world, run_id, session("on", 1), None).status_code == 403
    assert report(world, run_id, session("on", 1), "not-the-key").status_code == 403
    first = report(world, run_id, session("on", 1), key)
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "running" and len(first.json()["sessions"]) == 1
    again = report(world, run_id, session("on", 1), key)
    assert again.status_code == 409 and "already" in again.text
    past = report(world, run_id, session("on", 3), key)
    assert past.status_code == 409 and "2 session(s) of each arm" in past.text
    assert report(world, run_id, {"base_sha": BASE, "harness_sha": TIP}, key).status_code == 422
    for arm, index in (("off", 1), ("on", 2), ("off", 2)):
        last = report(world, run_id, session(arm, index, passed=arm == "on"), key)
        assert last.status_code == 200, last.text
    found = last.json()
    assert found["state"] == "done" and (found["base_sha"], found["harness_sha"]) == (BASE, TIP)
    assert [item["passed"] for item in found["sessions"]] == [True, False, True, False]
    assert {item["model"] for item in found["sessions"]} == {"opus"}
    moved(world.client, world.worker, run_id, "verifying", "done")
    (row,) = rows(world)
    assert row.state == "done"
    assert night(world, NIGHT + timedelta(days=1)).get("probe") is None  # the week has measured it
    assert night(world, NIGHT + timedelta(days=4)).get("probe") == 1  # Monday: a new week


def test_probe_run_reports_a_stale_probe_with_why(world):
    probing(world)
    spec = probe_run(world)
    stale = report(
        world, spec["id"], {"stale": "the change's diff no longer comes off main"}, spec["curator"]["probe_key"]
    )
    assert stale.status_code == 200, stale.text
    assert stale.json()["state"] == "stale" and "no longer comes off" in stale.json()["reason"]
    moved(world.client, world.worker, spec["id"], "verifying", "done")
    assert rows(world)[0].state == "stale"
    assert night(world, NIGHT + timedelta(days=1)).get("probe") is None


def test_probe_run_that_ends_short_is_queued_again_once_then_fails_with_why(world):
    probing(world)
    spec = probe_run(world)
    assert report(world, spec["id"], session("on", 1), spec["curator"]["probe_key"]).status_code == 200
    moved(world.client, world.worker, spec["id"], "failed", error="the agent crashed")
    (row,) = rows(world)
    assert (row.state, row.attempts) == ("queued", 1) and "1 of 4 sessions" in row.reason
    again = probe_run(world, NIGHT + timedelta(days=1))
    (row,) = rows(world)
    assert (row.state, row.run_id, row.sessions) == ("running", again["id"], [])
    moved(world.client, world.worker, again["id"], "verifying", "done")
    (row,) = rows(world)
    assert (row.state, row.attempts) == ("failed", 2) and "2 probe runs ended without every session" in row.reason
    assert night(world, NIGHT + timedelta(days=2)).get("probe") is None


def test_probe_run_takes_the_pair_measured_longest_ago_first(world):
    probe = probing(world, models=(OPUS, SONNET))
    pw = tables.curator_probe_runs
    c = tables.curator_changes
    ((project_id,),) = sql(world.db, select(c.c.project_id).where(c.c.id == probe["change_id"]))
    earlier = {"project_id": project_id, "probe_id": probe["id"], "change_id": probe["change_id"]}
    earlier |= {"runtime": "claude-code", "k": 2, "state": "done", "attempts": 0, "sessions": []}
    sql(world.db, insert(pw).values(**earlier, model="opus", week=WEEK - timedelta(days=7)))
    sql(world.db, insert(pw).values(**earlier, model="sonnet", week=WEEK - timedelta(days=14)))
    spec = probe_run(world)
    assert spec["model"] == "sonnet"  # measured two weeks ago, opus one


def test_probe_pairs_the_weeks_money_does_not_reach_are_recorded_unmeasured(world):
    probing(world, models=(OPUS, SONNET), budget=1.0)
    spec = probe_run(world)
    assert spec["model"] == "opus"
    r = tables.runs
    for arm, index in (("on", 1), ("off", 1), ("on", 2), ("off", 2)):
        assert report(world, spec["id"], session(arm, index), spec["curator"]["probe_key"]).status_code == 200
    sql(world.db, update(r).where(r.c.id == spec["id"]).values(usage={"total_cost_usd": 1.0}))
    moved(world.client, world.worker, spec["id"], "verifying", "done")
    assert night(world, NIGHT + timedelta(days=1)).get("probe") is None
    found = {row.model: row for row in rows(world)}
    assert found["opus"].state == "done"
    assert (
        found["sonnet"].state == "unmeasured" and "spent probes.budget_usd ($1.00 of $1.00)" in found["sonnet"].reason
    )
    assert found["sonnet"].run_id is None and claim(world.client, world.worker) is None


def test_probe_runs_go_only_to_a_worker_that_lists_probe_in_its_run_kinds(world):
    probing(world)
    body = {
        "runtimes": world.worker["runtimes"],
        "checkouts": world.worker["checkouts"],
        "free_slots": 1,
        "runs": [],
        "agent_version": "0.7.0",
        "run_kinds": [kind for kind in runs.RUN_KINDS if kind != "probe"],
    }
    response = world.client.post("/v1/worker/heartbeat", json=body, headers=world.worker["headers"])
    assert response.status_code == 200, response.text
    assert fire(world.client, NIGHT).get("probe") is None
    assert rows(world) == [] and claim(world.client, world.worker) is None


def test_probe_schema_0131_goes_down_to_0130_and_up_again(world):
    probing(world)
    spec = probe_run(world)
    db, pw, r = world.db, tables.curator_probe_runs, tables.runs
    for bad in ({"state": "paused"}, {"k": 0}, {"week": WEEK + timedelta(days=1)}, {"sessions": {}}):
        with pytest.raises(psycopg.errors.CheckViolation):
            sql(db, update(pw).values(**bad))
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(db, update(r).values(schedule_id=None).where(r.c.kind == "probe"))  # a schedule alone queues one
    (row,) = rows(world)
    again = {key: getattr(row, key) for key in ("project_id", "probe_id", "change_id", "week", "runtime", "model", "k")}
    with pytest.raises(psycopg.errors.UniqueViolation):  # once a week on each runtime and model
        sql(db, insert(pw).values(**again))
    move_to(db, "0130", down=True)
    assert sql(db, select(func.count()).select_from(r).where(r.c.kind == "probe")) == [(0,)]
    assert sql(db, select(func.count()).select_from(r).where(r.c.id == spec["id"])) == [(0,)]
    move_to(db, "0131")
    assert sql(db, select(func.count()).select_from(pw)) == [(0,)]


RULE_ARMS = {
    "on": {"AGENTS.md": "# Posts\n\nCite a source for every figure.\n"},
    "off": {"AGENTS.md": "# Posts\n"},
}


def plugin_probing(world, *, k: int = 2) -> int:
    """The charter with probes on opus and the probe of rule cite-sources of a core plugin, northwind-editorial, which
    names no proposal or change (source plugin); the probe's id."""
    written(
        world.client,
        world.headers["owner"],
        curator_charter(probes={"k": k, "budget_usd": 3.0, "models": [OPUS]}),
    )
    p, pl, pr = tables.projects, tables.plugins, tables.curator_probes
    ((project_id, label),) = sql(world.db, select(p.c.id, p.c.default_label).where(p.c.name == "evo-agents"))
    ((plugin_id,),) = sql(
        world.db, insert(pl).values(name="northwind-editorial", publisher="northwind").returning(pl.c.id)
    )
    probe = {"project_id": project_id, "repo": "evo-agents", "task": PROBE["task"], "verify": PROBE["verify"]}
    probe |= {"source": "plugin", "label": {**label, "projects": ["evo-agents"]}, "plugin_id": plugin_id}
    probe |= {"ref": "cite-sources", "arms": RULE_ARMS}
    ((probe_id,),) = sql(world.db, insert(pr).values(**probe).returning(pr.c.id))
    return probe_id


def test_probe_run_of_a_plugins_rule_has_no_change_or_plan_and_its_claim_hands_both_arms(world):
    probe_id = plugin_probing(world)
    spec = probe_run(world)
    sent = spec["curator"]["probe"]
    assert (sent["probe_id"], sent["plugin"], sent["ref"], sent["arms"]) == (
        probe_id,
        "northwind-editorial",
        "cite-sources",
        RULE_ARMS,
    )
    assert (sent["change_repo"], sent["merge_sha"]) == ("evo-agents", None)  # the files go in the probe's repo
    assert spec["curator"]["role"] == "probe" and spec["curator"].get("change_id") is None
    assert PROBE["task"] in spec["prompt"] and PROBE["verify"] not in spec["prompt"]
    r = tables.runs
    ((plan_id, title),) = sql(world.db, select(r.c.plan_id, r.c.title).where(r.c.id == spec["id"]))
    assert plan_id is None and title.startswith(f"Probe #{probe_id} of plugin northwind-editorial rule cite-sources")
    events = sql(world.db, select(tables.run_events.c.body).where(tables.run_events.c.run_id == spec["id"]))
    assert events and all(PROBE["verify"] not in str(event[0]) for event in events)  # no event holds the answer key
    key = spec["curator"]["probe_key"]
    for arm, index in (("on", 1), ("off", 1), ("on", 2), ("off", 2)):
        last = report(world, spec["id"], session(arm, index, passed=arm == "on"), key)
        assert last.status_code == 200, last.text
    assert last.json()["state"] == "done" and last.json()["change_id"] is None
    moved(world.client, world.worker, spec["id"], "verifying", "done")
    (row,) = rows(world)
    assert (row.state, row.change_id, row.probe_id) == ("done", None, probe_id)
    shown = world.client.get(path("probes"), headers=world.headers["owner"]).json()["probes"]
    assert [(item["source"], item["plugin"], item["ref"], item["change_id"]) for item in shown] == [
        ("plugin", "northwind-editorial", "cite-sources", None)
    ]
    found = world.client.get(path("probe-report"), params={"week": str(WEEK)}, headers=world.headers["owner"])
    assert found.status_code == 200, found.text
    (result,) = found.json()["weeks"][0]["probes"]
    assert (result["plugin"], result["ref"], result["change_id"], result["status"]) == (
        "northwind-editorial",
        "cite-sources",
        None,
        "holds",
    )

"""The daily release of a project on the hub: its schedule, the check at the schedule's local time, and the cycle that
check opens (``evo_agents.hub.releases``, ``evo_agents.hub.server.release_check``, ``evo_agents.hub.server.releases``).

The checks step 1 of the daily-release plan names. a1: an admin sets the schedule with its defaults (06:00
Asia/Ho_Chi_Minh, the tag nightly, v*, release.yml, the packaging paths, 200 USD) and becomes its owner; at that local
time the hub opens one cycle and queues one release run in mode cut, dispatched as the owner and pinned to no worker,
with the cycle's cap as its cost cap, only when a packaging path changed between the newest v* tag and the commit of
nightly and no cycle is unfinished; otherwise it queues nothing; a second check of the same local day opens nothing;
every check is kept and read back. a7: pause cancels the queued release run and stops new cycles, resume lets the
schedule check again, run checks now through the same gates, and the night shift's pause and the release's leave each
other alone; the commands are in the contract. GitHub is the fake of ``tests.hub.fake_github``, with the workers' App
installed on evo-agents and git's ls-refs answered for its tags.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from functools import partial
from types import SimpleNamespace

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from psycopg import errors
from sqlalchemy import func, insert, select, update

from evo_agents.hub import jobs, releases, runs, tables
from evo_agents.hub.release_cli import CHECK_KEYS, STATUS_KEYS
from evo_agents.hub.server.app import create_app, loop_tick, memory_upkeep
from evo_agents.hub.server.release_check import fire_release_schedules, release_pass
from evo_agents.hub.server.release_github import ls_refs, parse_refs
from evo_agents.hub.worker import queue
from tests.hub.contract_keys import assert_json_keys
from tests.hub.live import sql
from tests.hub.test_credentials_api import MINE, OWNER, app_key, install  # noqa: F401 (app_key is a fixture)
from tests.hub.test_credentials_api import hub_config as credentials_config
from tests.hub.test_credentials_api import members as credential_members
from tests.hub.test_credentials_api import worker_of as credential_worker
from tests.hub.test_curator import NIGHT, PROJECT, charter_body, grant, written
from tests.hub.test_curator import fire as fire_curator
from tests.hub.test_migrate import move_to
from tests.hub.test_run_cli import as_json, cli, ok, write_credentials
from tests.hub.test_run_stream import serving

REPO = "evo-agents"
READ = {"contents": "read", "metadata": "read"}
OLD, BASE, HEAD, DOCS = "a" * 40, "b" * 40, "c" * 40, "d" * 40
BASE_FILES = {
    "evo_agents/__init__.py": b'__version__ = "0.24.0"\n',
    "plugins/evo-hub/plugin.json": b'{"version": "0.24.0"}\n',
    "pyproject.toml": b'[project]\nname = "evo-ak"\n',
    "docs/hub.md": b"# The hub\n",
    "tests/test_x.py": b"def test_x():\n    pass\n",
}
HEAD_FILES = {**BASE_FILES, "evo_agents/hub/releases.py": b"RELEASE = 1\n"}
DOCS_FILES = {**BASE_FILES, "docs/hub.md": b"# The hub, again\n", "tests/test_y.py": b"def test_y():\n    pass\n"}
# Asia/Ho_Chi_Minh is UTC+7: 06:00 there on 2026-10-22 is 23:00 UTC on the 21st.
SIX = datetime(2026, 10, 21, 23, 0, tzinfo=UTC)
DAY = date(2026, 10, 22)
NEXT = SIX + timedelta(days=1)
DEFAULTS = {
    "repo": REPO,
    "github_repo": f"{MINE}/{REPO}",
    "at": "06:00",
    "timezone": "Asia/Ho_Chi_Minh",
    "target_ref": "nightly",
    "tag_pattern": "v*",
    "workflow": "release.yml",
    "paths": ["evo_agents/", "plugins/", "web/", ".claude-plugin/", "pyproject.toml", "uv.lock"],
    "cycle_budget_usd": 200.0,
    "owner": OWNER,
    "paused_at": None,
    "paused_by": None,
}
RUN_COLUMNS = (
    "kind",
    "state",
    "plan_id",
    "dispatched_by",
    "dispatched_via",
    "pinned_worker_id",
    "requested_runtime",
    "mode",
    "approval",
    "timeout_s",
    "max_attempts",
    "repos",
    "budget",
    "schedule_id",
)


def repo_at(github, nightly: str) -> None:
    """evo-agents on the fake: its three commits, its tags (v0.24.0 newest, an annotated tag on BASE) and nightly."""
    github.add_repo(MINE, REPO)
    for sha, files in ((BASE, BASE_FILES), (HEAD, HEAD_FILES), (DOCS, DOCS_FILES)):
        github.snapshot(MINE, REPO, sha, files)
    github.tag(MINE, REPO, "v0.9.0", OLD)  # lower than 0.24.0 though it sorts after it as text
    github.tag(MINE, REPO, "v0.24.0", BASE, annotated=True)
    github.tag(MINE, REPO, "v0.25.0rc1", HEAD)  # no version X.Y.Z: not a release tag
    github.tag(MINE, REPO, "nightly", nightly)
    github.set_ref(MINE, REPO, "refs/heads/main", nightly)


@pytest.fixture
def world(hub_db, tmp_path, github, app_key):  # noqa: F811
    """test_credentials_api's project (evo-agents and lumen-identity on GitHub, two repos elsewhere) on a hub holding
    the workers' App, installed on evo-agents with contents read; owner an admin of the project; evo-agents at v0.24.0
    with nightly one packaging change ahead."""
    config = credentials_config(hub_db, tmp_path, github, app_key)
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        headers = credential_members(client, github)
        grant(client, headers, OWNER, "admin")
        install(github, REPO, permissions=READ)
        repo_at(github, HEAD)
        yield SimpleNamespace(client=client, headers=headers, github=github, db=hub_db, app=client.app, config=config)


def path(*parts: str) -> str:
    return "/".join((f"/v1/projects/{PROJECT}/releases", *parts))


def status_of(world, who: str = "owner") -> dict:
    response = world.client.get(path(), headers=world.headers[who])
    assert response.status_code == 200, response.text
    return response.json()


def set_schedule(world, who: str = "owner", **body) -> dict:
    response = world.client.put(path("schedule"), json=body, headers=world.headers[who])
    assert response.status_code == 200, response.text
    return response.json()


def post(world, action: str, who: str = "owner"):
    return world.client.post(path(action), headers=world.headers[who])


def fire(world, now: datetime) -> dict:
    state = world.app.state
    return world.client.portal.call(partial(fire_release_schedules, state.engine, state.github_app, now=now))


def release_runs(db) -> list[dict]:
    r = tables.runs
    rows = sql(db, select(r.c.id, *(r.c[name] for name in RUN_COLUMNS)).where(r.c.kind == "release").order_by(r.c.id))
    return [dict(zip(("id", *RUN_COLUMNS), row, strict=True)) for row in rows]


def cycles(db) -> list[tuple]:
    c = tables.release_cycles
    columns = (c.c.id, c.c.state, c.c.start, c.c.local_day, c.c.base_tag, c.c.base_commit, c.c.target_commit)
    return sql(db, select(*columns, c.c.changed_paths, c.c.budget_usd).order_by(c.c.id))


def checks(db) -> list[tuple]:
    k = tables.release_checks
    return sql(db, select(k.c.trigger, k.c.local_day, k.c.outcome).order_by(k.c.id))


def user_id(db, login: str) -> int:
    return sql(db, select(tables.users.c.id).where(tables.users.c.login == login))[0][0]


def count(db, table) -> int:
    return sql(db, select(func.count()).select_from(table))[0][0]


# The model


def test_the_newest_tag_is_the_highest_version_after_the_prefix():
    tags = {"v0.9.0": OLD, "v0.24.0": BASE, "v0.25.0rc1": HEAD, "vnext": DOCS, "release-1.0.0": DOCS}
    assert releases.latest_tag(tags, "v*") == ("v0.24.0", BASE)
    assert releases.latest_tag(tags, "release-*") == ("release-1.0.0", DOCS)
    assert releases.latest_tag({"nightly": HEAD}, "v*") is None
    assert releases.tag_version("v1.2.3", "v*") == (1, 2, 3) and releases.tag_version("v01.2.3", "v*") is None


def test_a_packaging_path_changes_when_a_file_under_it_is_added_removed_or_changed():
    entries = ["evo_agents/", "pyproject.toml", "web/"]
    base = {"evo_agents/a.py": "1", "pyproject.toml": "2", "docs/x.md": "3", "web/app/page.tsx": "4"}
    assert releases.changed_paths(base, dict(base), entries) == []
    assert releases.changed_paths(base, {**base, "docs/x.md": "9"}, entries) == []
    assert releases.changed_paths(base, {**base, "evo_agents/b.py": "5"}, entries) == ["evo_agents/"]
    removed = {key: value for key, value in base.items() if key != "web/app/page.tsx"}
    assert releases.changed_paths(base, {**removed, "pyproject.toml": "7"}, entries) == ["pyproject.toml", "web/"]
    assert releases.under("evo_agents_extra/x.py", "evo_agents/") is False
    assert releases.under("uv.lock", "uv.lock") and releases.under("plugins/a/b.json", "plugins")


def test_the_check_of_a_day_is_due_from_its_time_for_the_catch_up_window():
    six = datetime(2026, 10, 22, 6, 0)
    assert releases.due_day(six, "06:00") == date(2026, 10, 22)
    assert releases.due_day(six + timedelta(minutes=59), "06:00") == date(2026, 10, 22)
    assert releases.due_day(six + timedelta(minutes=60), "06:00") is None
    assert releases.due_day(six - timedelta(minutes=1), "06:00") is None
    assert releases.due_day(datetime(2026, 10, 23, 0, 10), "23:30") == date(2026, 10, 22)  # past midnight


def test_packaging_paths_target_refs_and_patterns_are_checked():
    assert releases.paths_problem(list(releases.DEFAULT_PATHS)) is None
    for bad in ([], ["/etc"], ["a/../b"], ["a", "a"], ["web/*"], [" web/"], ["x" * 201]):
        assert releases.paths_problem(bad) is not None, bad
    assert releases.ref_problem("nightly") is None and releases.ref_problem("refs/heads/main") is None
    for bad in ("a..b", "/main", "main/", "x.lock", "a b"):
        assert releases.ref_problem(bad) is not None, bad
    assert releases.target_refs("nightly") == ("refs/tags/nightly", "refs/heads/nightly")
    assert releases.target_refs("refs/heads/main") == ("refs/heads/main",)


def test_ls_refs_asks_for_the_prefixes_and_reads_what_an_annotated_tag_peels_to():
    body = ls_refs(["refs/tags/v", "refs/tags/nightly"])
    assert b"command=ls-refs" in body and b"ref-prefix refs/tags/v\n" in body and body.endswith(b"0000")
    lines = (f"{'e' * 40} refs/tags/v0.24.0 peeled:{BASE}\n", f"{HEAD} refs/tags/nightly\n", "not-a-sha refs/x\n")
    answer = "".join(f"{len(line) + 4:04x}{line}" for line in lines).encode() + b"0000"
    assert parse_refs(answer) == {"refs/tags/v0.24.0": BASE, "refs/tags/nightly": HEAD}


def test_release_is_a_run_kind_that_the_release_schedule_alone_dispatches():
    assert runs.RUN_KINDS[-3] == releases.RELEASE_KIND == "release"  # question (0100) and browser (0118) come after it
    assert "release" in runs.SHOWN_KINDS


# Setting the schedule


def test_an_admin_sets_the_schedule_with_its_defaults_and_becomes_its_owner(world):
    assert status_of(world, "other") == {
        "project": PROJECT,
        "schedule": None,
        "paused": False,
        "next_check_at": None,
        "last_check": None,
        "checks": [],
        "cycle": None,
    }
    refused = world.client.put(path("schedule"), json={"repo": REPO}, headers=world.headers["other"])
    assert refused.status_code == 403 and "needs the admin role" in refused.text
    ambiguous = world.client.put(path("schedule"), json={}, headers=world.headers["owner"])
    assert ambiguous.status_code == 422 and "evo-agents, lumen-identity" in ambiguous.text
    for body, word in (
        ({"repo": "lumen-kb-docs"}, "origin on GitHub"),
        ({"repo": REPO, "timezone": "Mars/Olympus"}, "not a time zone"),
        ({"repo": REPO, "at": "6:00"}, "pattern"),
        ({"repo": REPO, "paths": ["/abs"]}, "not relative"),
        ({"repo": REPO, "tag_pattern": "v"}, "pattern"),
        ({"repo": REPO, "cycle_budget_usd": 0.5}, "greater than or equal"),
        ({"repo": REPO, "workers": 2}, "Extra inputs"),
    ):
        response = world.client.put(path("schedule"), json=body, headers=world.headers["owner"])
        assert response.status_code == 422 and word in response.text, (body, response.text)
    found = set_schedule(world, repo=REPO)
    schedule = found["schedule"]
    assert {key: schedule[key] for key in DEFAULTS} == DEFAULTS
    assert schedule["updated_by"] == OWNER and found["paused"] is False
    next_check = datetime.fromisoformat(found["next_check_at"])
    assert next_check.astimezone(UTC).time().isoformat() == "23:00:00"  # 06:00 in Ho Chi Minh City
    assert_json_keys("hub release status", found)
    again = set_schedule(world, at="05:30", paths=["evo_agents/", "uv.lock"], cycle_budget_usd=50)["schedule"]
    assert (again["repo"], again["at"], again["paths"], again["cycle_budget_usd"]) == (
        REPO,
        "05:30",
        ["evo_agents/", "uv.lock"],
        50.0,
    )
    assert again["timezone"] == "Asia/Ho_Chi_Minh" and again["target_ref"] == "nightly"  # kept
    a = tables.audit
    targets = sql(world.db, select(a.c.target).where(a.c.action == "release.set").order_by(a.c.id))
    assert [row[0] for row in targets] == [
        f"{PROJECT} repo={REPO} at=06:00 Asia/Ho_Chi_Minh",
        f"{PROJECT} repo={REPO} at=05:30 Asia/Ho_Chi_Minh",
    ]


# The daily check


def test_the_check_at_six_opens_one_cycle_and_queues_its_cut_run_as_the_owner(world):
    set_schedule(world, repo=REPO)
    assert fire(world, SIX - timedelta(minutes=1)) == {}  # 05:59 local: not yet
    assert fire(world, SIX) == {"opened": 1}
    owner = user_id(world.db, OWNER)
    [run] = release_runs(world.db)
    assert {key: value for key, value in run.items() if key != "id"} == {
        "kind": "release",
        "state": "queued",
        "plan_id": None,
        "dispatched_by": owner,
        "dispatched_via": "release",
        "pinned_worker_id": None,
        "requested_runtime": "claude-code",
        "mode": "headless",
        "approval": "auto",
        "timeout_s": releases.RUN_MINUTES * 60 + releases.GRACE_SECONDS,
        "max_attempts": 1,
        "repos": [{"repo": REPO}],
        "budget": {"max_usd": 200.0, "max_seconds": releases.RUN_MINUTES * 60},
        "schedule_id": None,
    }
    [cycle] = cycles(world.db)
    assert cycle[1:] == ("open", "cut", DAY, "v0.24.0", BASE, HEAD, ["evo_agents/"], 200.0)
    rr = tables.release_runs
    assert sql(world.db, select(rr.c.run_id, rr.c.cycle_id, rr.c.mode)) == [(run["id"], cycle[0], "cut")]
    assert checks(world.db) == [("schedule", DAY, "opened")]
    found = status_of(world, "other")
    last = found["last_check"]
    assert (last["outcome"], last["cycle_id"], last["run_id"], last["changed_paths"]) == (
        "opened",
        cycle[0],
        run["id"],
        ["evo_agents/"],
    )
    assert (last["base_tag"], last["base_commit"], last["target_commit"]) == ("v0.24.0", BASE, HEAD)
    landed = ("version", "branch", "decision", "pull_url", "merge_commit", "tagged", "tagged_at", "land_state")
    assert found["cycle"]["id"] == cycle[0] and found["cycle"]["runs"] == [
        {
            "run_id": run["id"],
            "mode": "cut",
            "tag": None,
            "state": "queued",
            "cost_usd": 0.0,
            **dict.fromkeys((*landed, "land_reason")),  # nothing pushed or landed yet
        }
    ]
    assert (found["cycle"]["cost_usd"], found["cycle"]["budget_usd"]) == (0.0, 200.0)
    a = tables.audit
    dispatched = sql(world.db, select(a.c.actor_id, a.c.token_id, a.c.target).where(a.c.action == "release.dispatch"))
    assert dispatched == [(owner, None, f"{PROJECT} cycle:{cycle[0]} run:{run['id']} mode=cut")]
    e = tables.run_events
    [event] = sql(world.db, select(e.c.body).where(e.c.run_id == run["id"], e.c.kind == "system"))
    assert event[0]["cycle_id"] == cycle[0] and event[0]["max_usd"] == 200.0
    shown = world.client.get(f"/v1/projects/{PROJECT}/runs/{run['id']}", headers=world.headers["owner"])
    assert shown.status_code == 200, shown.text
    assert (shown.json()["kind"], shown.json()["dispatched_via"], shown.json()["dispatched_by"]) == (
        "release",
        "release",
        OWNER,
    )
    assert world.client.get("/v1/me/overview", headers=world.headers["owner"]).status_code == 200


def test_a_second_check_the_same_local_day_opens_no_second_cycle(world):
    set_schedule(world, repo=REPO)
    world.github.tag(MINE, REPO, "nightly", DOCS)  # only docs and tests changed since v0.24.0
    assert fire(world, SIX) == {"unchanged": 1}
    world.github.tag(MINE, REPO, "nightly", HEAD)  # a packaging change lands at 06:10
    assert fire(world, SIX + timedelta(minutes=10)) == {"done": 1}
    assert fire(world, SIX + timedelta(minutes=30)) == {"done": 1}
    # a hub that restarted finds the day's check in the database
    with TestClient(create_app(world.config), base_url="https://hub.test") as again:
        state = again.app.state
        found = again.portal.call(partial(fire_release_schedules, state.engine, state.github_app, now=SIX))
    assert found == {"done": 1}
    assert release_runs(world.db) == [] and cycles(world.db) == []
    assert fire(world, NEXT) == {"opened": 1}  # the next day sees the change
    assert fire(world, NEXT + timedelta(minutes=1)) == {"done": 1}
    assert len(release_runs(world.db)) == 1 and len(cycles(world.db)) == 1
    assert checks(world.db) == [("schedule", DAY, "unchanged"), ("schedule", DAY + timedelta(days=1), "opened")]
    shown = status_of(world)["checks"]
    assert [item["outcome"] for item in shown] == ["opened", "unchanged"]
    assert "changes no packaging path since v0.24.0" in shown[1]["reason"]


def test_nightly_on_the_newest_tag_queues_nothing(world):
    set_schedule(world, repo=REPO)
    world.github.tag(MINE, REPO, "nightly", BASE)
    assert fire(world, SIX) == {"unchanged": 1}
    assert release_runs(world.db) == [] and cycles(world.db) == []
    assert status_of(world)["last_check"]["changed_paths"] == []


def test_an_unfinished_cycle_keeps_the_next_days_check_busy(world):
    set_schedule(world, repo=REPO)
    assert fire(world, SIX) == {"opened": 1}
    c = tables.release_cycles
    for state in ("open", "stopped"):
        sql(world.db, update(c).values(state=state))
        assert fire(world, NEXT + timedelta(days=["open", "stopped"].index(state))) == {"busy": 1}
    assert len(release_runs(world.db)) == 1 and len(cycles(world.db)) == 1
    reason = status_of(world)["last_check"]["reason"]
    assert reason.startswith("cycle #") and "is stopped" in reason
    sql(world.db, update(c).values(state="published", ended_at=func.now()))
    r = tables.runs
    sql(world.db, update(r).values(state="cancelled", finished_at=func.now()).where(r.c.kind == "release"))
    assert fire(world, NEXT + timedelta(days=2)) == {"opened": 1}  # once it ended, the next change opens a cycle
    assert len(cycles(world.db)) == 2


def test_a_check_that_cannot_read_github_is_kept_and_tried_again_three_times_a_day(world):
    set_schedule(world, repo=REPO)
    world.github.repos[(MINE.lower(), REPO)].refs.clear()  # no tag, no nightly
    for minute in range(5):
        fire(world, SIX + timedelta(minutes=minute))
    assert checks(world.db) == [("schedule", DAY, "error")] * releases.ERROR_TRIES
    assert "no tag of maycuatroi1/evo-agents matches v*" in status_of(world)["last_check"]["reason"]
    repo_at(world.github, HEAD)
    assert fire(world, NEXT) == {"opened": 1}


def test_a_hub_without_the_app_installed_records_why(world):
    set_schedule(world, repo=REPO)
    world.github.installations.clear()
    assert fire(world, SIX) == {"error": 1}
    reason = status_of(world)["last_check"]["reason"]
    assert "not installed" in reason or "App" in reason, reason
    assert release_runs(world.db) == []


def test_an_owner_who_lost_the_admin_role_queues_nothing(world):
    set_schedule(world, repo=REPO)
    grant(world.client, world.headers, OWNER, "writer")
    assert fire(world, SIX) == {"owner": 1}
    assert release_runs(world.db) == []
    assert "holds no admin role" in status_of(world, "other")["last_check"]["reason"]


# Pause, resume and run now


def test_pause_cancels_the_queued_release_run_and_resume_lets_it_check_again(world):
    assert post(world, "pause").status_code == 409  # no schedule yet
    set_schedule(world, repo=REPO)
    assert fire(world, SIX) == {"opened": 1}
    [run] = release_runs(world.db)
    refused = post(world, "pause", "other")
    assert refused.status_code == 403 and "needs the admin role" in refused.text
    paused = post(world, "pause")
    assert paused.status_code == 200, paused.text
    found = paused.json()
    assert found["paused"] is True and found["schedule"]["paused_by"] == OWNER and found["cycle"] is None
    assert [item["state"] for item in release_runs(world.db)] == ["cancelled"]
    c = tables.release_cycles
    [(state, reason, ended)] = sql(world.db, select(c.c.state, c.c.stop_reason, c.c.ended_at))
    assert state == "closed" and "paused by owner" in reason and ended is not None
    first = found["schedule"]["paused_at"]
    assert post(world, "pause").json()["schedule"]["paused_at"] == first  # a second pause keeps the first time
    assert fire(world, NEXT) == {"paused": 1}
    assert len(release_runs(world.db)) == 1
    resumed = post(world, "resume")
    assert resumed.status_code == 200 and resumed.json()["paused"] is False
    assert fire(world, NEXT + timedelta(days=1)) == {"opened": 1}
    assert [item["state"] for item in release_runs(world.db)] == ["cancelled", "queued"]
    a = tables.audit
    actions = sql(world.db, select(a.c.action).where(a.c.action.in_(("release.pause", "release.resume"))))
    assert [row[0] for row in actions] == ["release.pause", "release.pause", "release.resume"]


def test_run_now_checks_through_the_same_gates_as_the_daily_check(world):
    assert post(world, "run").status_code == 409
    set_schedule(world, repo=REPO)
    refused = post(world, "run", "other")
    assert refused.status_code == 403
    world.github.tag(MINE, REPO, "nightly", DOCS)
    first = post(world, "run")
    assert first.status_code == 200, first.text
    assert (first.json()["outcome"], first.json()["trigger"], first.json()["requested_by"]) == (
        "unchanged",
        "manual",
        OWNER,
    )
    assert_json_keys("hub release run", first.json())
    world.github.tag(MINE, REPO, "nightly", HEAD)
    opened = post(world, "run").json()
    assert opened["outcome"] == "opened" and opened["run_id"] == release_runs(world.db)[0]["id"]
    assert post(world, "run").json()["outcome"] == "busy"
    assert fire(world, SIX) == {"busy": 1}  # the day's scheduled check still happens, and finds the cycle
    assert post(world, "pause").status_code == 200
    assert post(world, "run").json()["outcome"] == "paused"
    assert [row[0] for row in checks(world.db)] == ["manual", "manual", "manual", "schedule", "manual"]
    a = tables.audit
    assert len(sql(world.db, select(a.c.id).where(a.c.action == "release.run"))) == 4


def test_the_night_shifts_pause_and_the_releases_leave_each_other_alone(world):
    """Pausing either leaves the other's schedule as it was and its queued run queued (invariant a8 on its side)."""
    worker = credential_worker(world.client, world.headers["owner"], "mac-mini")
    beat = {"runtimes": worker["runtimes"], "checkouts": worker["checkouts"], "free_slots": 1, "runs": []}
    beat |= {"agent_version": "0.24.0", "run_kinds": list(runs.RUN_KINDS)}
    assert world.client.post("/v1/worker/heartbeat", json=beat, headers=worker["headers"]).status_code == 200
    written(world.client, world.headers["owner"], charter_body(worker="mac-mini", night_plans=["credentials-smoke"]))
    set_schedule(world, repo=REPO)
    assert fire(world, SIX) == {"opened": 1}
    assert sum(fire_curator(world.client, NIGHT).values()) == 1
    r, s, rs = tables.runs, tables.schedules, tables.release_schedules
    [(night_run, state)] = sql(world.db, select(r.c.id, r.c.state).where(r.c.schedule_id.is_not(None)))
    assert state == "queued"
    # the release's pause: the night shift's schedule and its queued run are left as they were
    assert post(world, "pause").status_code == 200
    assert sql(world.db, select(s.c.paused_at, s.c.pause_reason)) == [(None, None)]
    assert sql(world.db, select(r.c.state).where(r.c.id == night_run)) == [("queued",)]
    assert world.client.get(f"/v1/projects/{PROJECT}/curator", headers=world.headers["owner"]).json()["paused"] is False
    assert post(world, "resume").status_code == 200
    assert fire(world, NEXT) == {"opened": 1}
    # the night shift's pause: the release schedule and its queued run are left as they were
    curator_paused = world.client.post(f"/v1/projects/{PROJECT}/curator/pause", headers=world.headers["owner"])
    assert curator_paused.status_code == 200, curator_paused.text
    assert sql(world.db, select(r.c.state).where(r.c.id == night_run)) == [("cancelled",)]  # its own run
    assert sql(world.db, select(rs.c.paused_at, rs.c.paused_by)) == [(None, None)]
    assert [item["state"] for item in release_runs(world.db)] == ["cancelled", "queued"]
    assert status_of(world)["paused"] is False
    assert fire(world, NEXT + timedelta(days=1)) == {"busy": 1}  # the release still checks, and finds its cycle


def test_the_release_schedules_job_runs_every_minute():
    periodic = {p.task.name: p for p in queue.periodic_registry.periodic_tasks.values()}
    assert periodic[jobs.RELEASE_SCHEDULES].cron == "* * * * *"
    assert queue.tasks[jobs.RELEASE_SCHEDULES].queueing_lock == jobs.RELEASE_SCHEDULES == "hub.release_schedules"


def test_the_release_schedules_job_takes_the_hubs_engine_and_app(world):
    later = (datetime.now(UTC) + timedelta(hours=3)).strftime("%H:%M")  # the job reads the database's clock
    set_schedule(world, repo=REPO, at=later, timezone="UTC")
    state = world.app.state
    found = SimpleNamespace(engine=state.engine, github_app=state.github_app, release_pass=release_pass)
    context = SimpleNamespace(additional_context={"hub": found})
    assert world.client.portal.call(queue.tasks[jobs.RELEASE_SCHEDULES].func, context) == {}


def test_a_worker_handed_no_release_pass_checks_no_schedule_and_says_so(world, caplog):
    set_schedule(world, repo=REPO, at="06:00", timezone="Asia/Ho_Chi_Minh")
    found = SimpleNamespace(engine=world.app.state.engine, github_app=world.app.state.github_app)
    context = SimpleNamespace(additional_context={"hub": found})
    with caplog.at_level("WARNING", logger="evo_agents.hub.worker"):
        assert world.client.portal.call(queue.tasks[jobs.RELEASE_SCHEDULES].func, context) == {"no_release_pass": 1}
    assert "no release pass was handed to this worker" in caplog.text
    assert sql(world.db, select(func.count()).select_from(tables.release_checks)) == [(0,)]


def test_the_hub_worker_command_hands_the_worker_the_servers_release_pass(monkeypatch, tmp_path):
    import argparse

    from evo_agents.hub import cli as hub_cli
    from evo_agents.hub import worker
    from evo_agents.hub.config import S3_VARIABLES

    for name, value in zip(S3_VARIABLES, ("https://s3.northwind.test", "northwind-hub", "id", "secret"), strict=True):
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("EVO_HUB_SENTRY_DSN", raising=False)
    handed = []
    monkeypatch.setattr(worker, "main", lambda config, concurrency, **kwargs: handed.append(kwargs) or 0)
    args = argparse.Namespace(
        dsn="postgresql://hub@127.0.0.1:1/hub", data_dir=str(tmp_path), concurrency=1, hub_command="worker"
    )
    assert hub_cli.cmd_worker.__wrapped__(args) == 0
    assert handed == [{"release_pass": release_pass, "loop_tick": loop_tick, "memory_upkeep": memory_upkeep}]


# From the command line


@pytest.fixture
def served(hub_db, tmp_path, github, app_key):  # noqa: F811
    import httpx

    app = create_app(credentials_config(hub_db, tmp_path, github, app_key))
    with serving(app) as url, httpx.Client(base_url=url, timeout=10) as client:
        headers = credential_members(client, github)
        grant(client, headers, OWNER, "admin")
        install(github, REPO, permissions=READ)
        repo_at(github, HEAD)
        homes = {
            who: write_credentials(tmp_path / who, url, login, headers[who]["Authorization"].removeprefix("Bearer "))
            for who, login in (("owner", OWNER), ("other", "someone-else"))
        }
        yield SimpleNamespace(url=url, homes=homes)


def release_cli(served, monkeypatch, capsys, who: str, *args: str):
    return cli(monkeypatch, capsys, served.homes[who], "hub", "release", *args, "--project", PROJECT)


def test_release_status_set_pause_resume_and_run_from_the_command_line(served, monkeypatch, capsys):
    text = ok(release_cli(served, monkeypatch, capsys, "other", "status")).out
    assert "has no release schedule" in text
    refused = release_cli(served, monkeypatch, capsys, "other", "set", "--repo", REPO)
    assert refused.code == 1 and "needs the admin role" in refused.err
    args = ("set", "--repo", REPO, "--at", "06:30", "--path", "evo_agents/", "--path", "uv.lock", "--budget", "150")
    printed = as_json(release_cli(served, monkeypatch, capsys, "owner", *args, "--json"))
    assert_json_keys("hub release set", printed)
    assert (printed["schedule"]["at"], printed["schedule"]["paths"], printed["schedule"]["cycle_budget_usd"]) == (
        "06:30",
        ["evo_agents/", "uv.lock"],
        150.0,
    )
    shown = ok(release_cli(served, monkeypatch, capsys, "other", "status")).out
    assert "06:30 Asia/Ho_Chi_Minh" in shown and "evo_agents/, uv.lock" in shown and "$150.00" in shown
    checked = as_json(release_cli(served, monkeypatch, capsys, "owner", "run", "--json"))
    assert_json_keys("hub release run", checked)
    assert checked["outcome"] == "opened" and checked["changed_paths"] == ["evo_agents/"]
    status = as_json(release_cli(served, monkeypatch, capsys, "other", "status", "--json"))
    assert_json_keys("hub release status", status)
    assert status["cycle"]["state"] == "open" and status["cycle"]["runs"][0]["mode"] == "cut"
    paused = ok(release_cli(served, monkeypatch, capsys, "owner", "pause")).out
    assert "Paused the release of evo-agents" in paused
    assert_json_keys(
        "hub release resume", as_json(release_cli(served, monkeypatch, capsys, "owner", "resume", "--json"))
    )
    again = ok(release_cli(served, monkeypatch, capsys, "owner", "run")).out
    assert again.startswith("Checked: ") and "opened" in again
    assert set(STATUS_KEYS) == set(status) and set(CHECK_KEYS) == set(checked)


# Schema 0067


def refused(db, error, statement) -> None:
    with pytest.raises(error):
        sql(db, statement)


def test_the_rows_of_0067_hold_together_and_it_goes_down_to_0066_and_up_again(world):
    set_schedule(world, repo=REPO)
    assert fire(world, SIX) == {"opened": 1}
    db = world.db
    s, c, rr, k, r = (
        tables.release_schedules,
        tables.release_cycles,
        tables.release_runs,
        tables.release_checks,
        tables.runs,
    )
    for values in (
        {"check_at": "25:00"},
        {"paths": []},
        {"paths": [1]},
        {"paths": {"a": 1}},
        {"cycle_budget_usd": 0.5},
        {"paused_at": func.now()},  # a pause names who paused
        {"tag_pattern": "v"},
        {"workflow": "release.sh"},
        {"target_ref": "a b"},
    ):
        refused(db, errors.CheckViolation, update(s).values(**values))
    for values in ({"state": "lost"}, {"start": "patch"}, {"ended_at": func.now()}, {"base_commit": "abc"}):
        refused(db, errors.CheckViolation, update(c).values(**values))
    refused(db, errors.CheckViolation, update(rr).values(mode="patch"))
    for values in ({"outcome": "maybe"}, {"trigger": "manual"}, {"reason": ""}, {"changed_paths": "evo_agents/"}):
        refused(db, errors.CheckViolation, update(k).values(**values))
    with live.engine(db).begin() as conn:
        cycle = conn.execute(select(c)).mappings().one()
        check = conn.execute(select(k)).mappings().one()
        run = conn.execute(select(r).where(r.c.kind == "release")).mappings().one()
    refused(
        db,
        errors.UniqueViolation,
        insert(c).values(**{key: v for key, v in cycle.items() if key != "id" and v is not None}),
    )
    refused(
        db,
        errors.UniqueViolation,
        insert(k).values(**{key: v for key, v in check.items() if key != "id" and v is not None}),
    )
    copy = {key: value for key, value in run.items() if key != "id" and value is not None}  # SQL NULL, not JSON null
    refused(db, errors.UniqueViolation, insert(r).values(**copy))  # one active release run in a project
    refused(db, errors.CheckViolation, insert(r).values(**{**copy, "state": "cancelled", "dispatched_via": "machine"}))
    sql(db, insert(r).values(**{**copy, "state": "cancelled", "finished_at": func.now()}))  # an ended one may be
    move_to(db, "0066", down=True)
    gone = select(*(func.to_regclass(f"public.{name}") for name in pg.RELEASE_TABLES))
    assert sql(db, gone) == [(None,) * len(pg.RELEASE_TABLES)]
    assert sql(db, select(func.count()).select_from(r).where(r.c.kind == "release")) == [(0,)]
    at_0066 = {
        key: value
        for key, value in copy.items()
        if key not in ("principal_id", "autonomy", "browser", "browser_identities", "selector", "tolerations")
    }  # of 0113, 0118, 0119 and 0149
    refused(db, errors.CheckViolation, insert(r).values(**{**at_0066, "state": "cancelled", "finished_at": func.now()}))
    move_to(db, "0067")
    assert sql(db, select(func.count()).select_from(s)) == [(0,)]
    move_to(db, "head")  # not 0067: the schedule's run writes the columns of every later revision
    set_schedule(world, repo=REPO)
    assert fire(world, NEXT) == {"opened": 1}

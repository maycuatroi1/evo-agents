"""K6 at the release the schedule cuts (``evo_agents.hub.server.release_land``), as criterion a6 of plan autonomy-levels
names it: before the hub merges a release branch and tags it, it reads K6 of the release run's principal. deny leaves it
unlanded and stops the cycle with a reason that names the class, the mode and the source; ask waits for the
principal's answer and lands once they approve. The world is ``tests.hub.test_release_runs``'s; the projects and people
are fictional."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import release_runs, runs, tables
from tests.hub.live import sql
from tests.hub.test_credentials_api import MINE, OWNER, audit_rows
from tests.hub.test_curator import PROJECT
from tests.hub.test_release_runs import (  # noqa: F401 (app_key and world are fixtures)
    app_key,
    cycle_of,
    done,
    land,
    landed_row,
    pushed,
    tag_of,
    world,
)
from tests.hub.test_release_schedule import REPO, SIX, fire, set_schedule
from tests.hub.test_runs import claim, moved

VERSION = "0.25.0"


def pref(world, **classes) -> None:
    body = {"level": "A5", "classes": classes}
    response = world.client.put("/v1/autonomy/prefs", json=body, headers=world.headers["owner"])
    assert response.status_code == 200, response.text


def cut_by_autonomy(world) -> int:
    """The schedule's cut run, claimed by a worker whose daemon runs release runs and says autonomy, its branch pushed
    and the run done; its id."""
    set_schedule(world, repo=REPO)
    assert fire(world, SIX) == {"opened": 1}
    worker = world.worker
    body = {
        "runtimes": worker["runtimes"],
        "checkouts": worker["checkouts"],
        "free_slots": 1,
        "runs": [],
        "agent_version": VERSION,
        "run_kinds": list(runs.RUN_KINDS),
        "features": list(runs.WORKER_FEATURES),
    }
    assert world.client.post("/v1/worker/heartbeat", json=body, headers=worker["headers"]).status_code == 200
    spec = claim(world.client, worker)
    assert spec is not None and spec["kind"] == "release"
    moved(world.client, worker, spec["id"], "running")
    assert pushed(world, spec["id"], VERSION).status_code == 201
    done(world, spec["id"])
    return spec["id"]


def test_autonomy_level_deploy_a_release_under_deny_is_not_landed_and_its_cycle_stops(world):
    pref(world, K6="deny")
    run_id = cut_by_autonomy(world)
    assert land(world) == {"refused": 1}
    assert world.github.merges == [] and tag_of(world.github, "v0.25.0") is None
    assert world.github.pull_of(MINE, REPO, release_runs.branch_of(VERSION)) is None
    row = landed_row(world.db, run_id)
    assert row["land_state"] == "refused" and "K6 (deploy, publish, tag a release) is deny" in row["land_reason"]
    assert f"set by pref:{OWNER}" in row["land_reason"]
    state, reason, _ = cycle_of(world.db)
    assert state == "stopped" and "K6 (" in reason
    ((target, *_),) = audit_rows(world.db, "autonomy.refused")
    assert target == f"run:{run_id} K6 release v0.25.0 mode=deny"


def test_autonomy_level_deploy_a_release_under_ask_lands_once_the_principal_approves(world):
    pref(world, K6="ask")
    run_id = cut_by_autonomy(world)
    assert land(world) == {"waiting": 1}
    assert world.github.merges == [] and tag_of(world.github, "v0.25.0") is None
    d = tables.decisions
    ((decision_id, key),) = sql(
        world.db,
        select(d.c.id, d.c.step_key).where(d.c.run_id == run_id, d.c.category == runs.AUTONOMY_DECISION_CATEGORY),
    )
    assert key == "release v0.25.0"
    assert f"decision #{decision_id}" in landed_row(world.db, run_id)["land_reason"]
    assert land(world) == {"waiting": 1}  # asked once
    path = f"/v1/projects/{PROJECT}/decisions/{decision_id}/answer"
    answered = world.client.post(path, json={"option": "approve"}, headers=world.headers["owner"])
    assert answered.status_code == 200, answered.text
    assert land(world) == {"tagged": 1}
    assert tag_of(world.github, "v0.25.0") is not None
